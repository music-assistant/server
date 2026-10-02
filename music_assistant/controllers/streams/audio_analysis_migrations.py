"""
Database schema migration logic for the audio analysis database.

Holds the versioned, step-by-step migrations that bring an attached ``audio_analysis.db``
up to the current ``AA_DB_SCHEMA_VERSION``, mirroring the music library's migrations
module. The analysis database keeps its own version counter so its steps never share
numbering with the library schema (which diverges between dev and stable).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Final

from music_assistant_models.errors import ProviderUnavailableError

from music_assistant.constants import (
    DB_TABLE_AUDIO_ANALYSIS,
    DB_TABLE_AUDIO_ANALYSIS_FAILURES,
)
from music_assistant.controllers.streams import constants
from music_assistant.controllers.streams.audio_analysis_codec import encode
from music_assistant.helpers.json import json_loads
from music_assistant.models.audio_analysis import AudioAnalysisData

if TYPE_CHECKING:
    import logging
    from collections.abc import Mapping

    from music_assistant.helpers.database import DatabaseConnection

LEGACY_TABLES: Final[tuple[str, ...]] = (
    DB_TABLE_AUDIO_ANALYSIS,
    DB_TABLE_AUDIO_ANALYSIS_FAILURES,
)
_FAILURE_COLUMNS: Final[tuple[str, ...]] = (
    "media_type",
    "item_id",
    "provider",
    "aa_provider_domain",
    "reason",
    "analysis_version",
    "next_retry",
    "timestamp_created",
)


async def migrate_analysis_database(
    database: DatabaseConnection,
    logger: logging.Logger,
    prev_version: int,
) -> int:
    """
    Migrate the attached analysis database from a previous schema version to the current one.

    A step that cannot complete raises and keeps its source data, so the caller leaves the
    stored version untouched and the step runs again on the next start.

    :param database: The music library connection the analysis database is attached to.
    :param logger: Logger to report progress on.
    :param prev_version: The schema version currently stored in the analysis database.
    :returns: Number of rows moved out of library.db, so the caller can compact it.
    """
    logger.info(
        "Migrating %s from version %s to %s",
        constants.AA_DB_FILENAME,
        prev_version,
        constants.AA_DB_SCHEMA_VERSION,
    )
    moved = 0

    if prev_version < 1:
        # analysis used to live in library.db as JSON; pack it into the attached file
        moved += await _convert_legacy_analysis(database, logger)
        moved += await _relocate_legacy_failures(database, logger)

    return moved


async def has_legacy_tables(database: DatabaseConnection) -> bool:
    """
    Return whether library.db still holds an analysis table from before the split.

    :param database: The music library connection.
    """
    rows = await database.get_rows_from_query(
        "SELECT 1 FROM main.sqlite_master WHERE type = 'table' AND name IN "
        f"({', '.join(f"'{table}'" for table in LEGACY_TABLES)})",
        limit=1,
    )
    return bool(rows)


async def _table_exists(database: DatabaseConnection, table: str) -> bool:
    """
    Return whether a table exists in library.db (the main schema).

    :param database: The music library connection.
    :param table: Unqualified table name.
    """
    rows = await database.get_rows_from_query(
        "SELECT 1 FROM main.sqlite_master WHERE type = 'table' AND name = :name",
        {"name": table},
        limit=1,
    )
    return bool(rows)


async def _convert_legacy_analysis(database: DatabaseConnection, logger: logging.Logger) -> int:
    """
    Pack the JSON rows of the legacy main.audio_analysis into the attached db, then drop it.

    :param database: The music library connection the analysis database is attached to.
    :param logger: Logger to report progress on.
    :returns: Number of rows removed from library.db (0 if the table is absent).
    """
    source = f"main.{DB_TABLE_AUDIO_ANALYSIS}"
    if not await _table_exists(database, DB_TABLE_AUDIO_ANALYSIS):
        return 0
    total = await database.get_count_from_query(f"SELECT id FROM {source}")
    logger.info("Converting %s audio analysis rows from library.db to the packed format", total)
    converted = 0
    unreadable = 0
    last_id = 0
    while True:
        rows = await database.get_rows_from_query(
            "SELECT id, media_type, item_id, provider, aa_provider_domain, "
            "CAST(analysis_data AS BLOB) AS analysis_data, analysis_version, "
            f"timestamp_created FROM {source} WHERE id > :last ORDER BY id",
            {"last": last_id},
            limit=constants.MIGRATE_BATCH_SIZE,
        )
        if not rows:
            break
        packed, bad = await asyncio.to_thread(_pack_rows, rows, logger)
        unreadable += bad
        for values in packed:
            # conflicts keep the newer row, so a retry never overwrites a newer destination
            # row and rows a downgraded build wrote win over older copies
            await database.execute(
                f"INSERT INTO {constants.AA_TABLE_ANALYSIS} (media_type, item_id, "
                "provider, aa_provider_domain, analysis_version, timestamp_created, "
                "header, payload) VALUES (:media_type, :item_id, :provider, "
                ":aa_provider_domain, :analysis_version, :timestamp_created, "
                ":header, :payload) "
                "ON CONFLICT(item_id, provider, aa_provider_domain, media_type) "
                "DO UPDATE SET analysis_version = excluded.analysis_version, "
                "timestamp_created = excluded.timestamp_created, header = excluded.header, "
                "payload = excluded.payload "
                "WHERE excluded.timestamp_created > "
                f"{DB_TABLE_AUDIO_ANALYSIS}.timestamp_created",
                values,
            )
        await database.commit()
        converted += len(packed)
        last_id = int(rows[-1]["id"])
        if (converted + unreadable) % constants.MIGRATE_PROGRESS_ROWS < len(rows):
            logger.info("Converted %s/%s audio analysis rows", converted + unreadable, total)
    # verify by natural key, not row count: a live write can consume the packed
    # table's own AUTOINCREMENT sequence, so its count alone proves nothing
    missing = await database.get_count_from_query(
        f"SELECT s.id FROM {source} s WHERE NOT EXISTS ("
        f"SELECT 1 FROM {constants.AA_TABLE_ANALYSIS} a WHERE a.item_id = s.item_id "
        "AND a.provider = s.provider AND a.aa_provider_domain = s.aa_provider_domain "
        "AND a.media_type = s.media_type)"
    )
    if missing > unreadable:
        raise ProviderUnavailableError(
            f"Conversion of {source} incomplete ({missing} rows missing, {unreadable} unreadable)"
        )
    await database.execute(f"DROP TABLE {source}")
    await database.commit()
    if unreadable:
        logger.warning("%s unreadable audio analysis rows in library.db were dropped", unreadable)
    logger.info("Converted %s audio analysis rows into the packed format", converted)
    return total


async def _relocate_legacy_failures(database: DatabaseConnection, logger: logging.Logger) -> int:
    """
    Copy the legacy main.audio_analysis_failures into the attached db in id batches, then drop it.

    :param database: The music library connection the analysis database is attached to.
    :param logger: Logger to report progress on.
    :returns: Number of rows in the dropped source (0 if absent).
    """
    table = DB_TABLE_AUDIO_ANALYSIS_FAILURES
    if not await _table_exists(database, table):
        return 0
    schema = constants.AA_DB_SCHEMA
    total = await database.get_count_from_query(f"SELECT id FROM main.{table}")
    max_id = 0
    if total:
        row = await database.get_rows_from_query(
            f"SELECT MAX(id) AS max_id FROM main.{table}", limit=1
        )
        max_id = int(row[0]["max_id"])
    logger.info(
        "Moving %s rows from library.db table %s to %s", total, table, constants.AA_DB_FILENAME
    )
    cols = ", ".join(_FAILURE_COLUMNS)
    updates = ", ".join(f"{column} = excluded.{column}" for column in _FAILURE_COLUMNS)
    copied = 0
    last_id = 0
    while last_id < max_id:
        cursor = await database.execute(
            f"INSERT INTO {schema}.{table} ({cols}) "
            f"SELECT {cols} FROM main.{table} "
            f"WHERE id > :last_id AND id <= :upper ORDER BY id "
            "ON CONFLICT(item_id, provider, aa_provider_domain, media_type) "
            f"DO UPDATE SET {updates} "
            f"WHERE excluded.timestamp_created > {table}.timestamp_created",
            {"last_id": last_id, "upper": last_id + constants.RELOCATE_BATCH_SIZE},
        )
        await database.commit()
        copied += cursor.rowcount
        last_id += constants.RELOCATE_BATCH_SIZE
        logger.debug("Moved %s/%s rows of %s", min(copied, total), total, table)
    missing = await database.get_count_from_query(
        f"SELECT m.id FROM main.{table} m WHERE NOT EXISTS ("
        f"SELECT 1 FROM {schema}.{table} a "
        f"WHERE a.item_id = m.item_id AND a.provider = m.provider "
        f"AND a.aa_provider_domain = m.aa_provider_domain AND a.media_type = m.media_type)"
    )
    if missing:
        raise ProviderUnavailableError(
            f"Relocation of {table} incomplete ({missing} of {total} rows still unmigrated)"
        )
    await database.execute(f"DROP TABLE main.{table}")
    await database.commit()
    logger.info("Moved %s of %s rows of %s into %s", copied, total, table, constants.AA_DB_FILENAME)
    return total


def _pack_rows(
    rows: list[Mapping[str, Any]], logger: logging.Logger
) -> tuple[list[dict[str, Any]], int]:
    """
    Decode JSON rows through the model and pack them.

    :param rows: Legacy rows carrying an ``analysis_data`` JSON column.
    :param logger: Logger to report skipped rows on.
    :returns: The packed rows and the number of rows that could not be read or packed.
    """
    packed: list[dict[str, Any]] = []
    unreadable = 0
    for row in rows:
        try:
            analysis = AudioAnalysisData.from_dict(json_loads(row["analysis_data"]))
            header, payload = encode(analysis)
        except (IndexError, KeyError, TypeError, ValueError) as err:
            # the error itself may embed the full (huge) field value, so log only the error
            # type plus the offending field name; one bad row must not stall the conversion
            error_detail = type(err).__name__
            if field_name := getattr(err, "field_name", None):
                error_detail = f"{error_detail} in field {field_name}"
            logger.warning(
                "Skipping unreadable audio_analysis row (id=%s, domain=%s, error=%s)",
                row["id"],
                row["aa_provider_domain"],
                error_detail,
            )
            unreadable += 1
            continue
        packed.append(
            {
                "media_type": row["media_type"],
                "item_id": row["item_id"],
                "provider": row["provider"],
                "aa_provider_domain": row["aa_provider_domain"],
                "analysis_version": row["analysis_version"],
                "timestamp_created": row["timestamp_created"],
                "header": header,
                "payload": payload,
            }
        )
    return packed, unreadable
