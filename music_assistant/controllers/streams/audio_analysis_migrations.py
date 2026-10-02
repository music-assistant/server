"""
Database schema migration logic for the audio analysis database.

Holds the versioned, step-by-step migrations that bring an attached ``audio_analysis.db``
up to the current ``AA_DB_SCHEMA_VERSION``, mirroring the music library's migrations
module. The analysis database keeps its own version counter so its steps never share
numbering with the library schema (which diverges between dev and stable).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from music_assistant_models.errors import ProviderUnavailableError

from music_assistant.constants import (
    DB_TABLE_AUDIO_ANALYSIS,
    DB_TABLE_AUDIO_ANALYSIS_FAILURES,
)
from music_assistant.controllers.streams import constants

if TYPE_CHECKING:
    import logging

    from music_assistant.helpers.database import DatabaseConnection

LEGACY_TABLES: Final[tuple[str, ...]] = (
    DB_TABLE_AUDIO_ANALYSIS,
    DB_TABLE_AUDIO_ANALYSIS_FAILURES,
)
_ANALYSIS_COLUMNS: Final[tuple[str, ...]] = (
    "media_type",
    "item_id",
    "provider",
    "aa_provider_domain",
    "analysis_data",
    "analysis_version",
    "timestamp_created",
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
        # analysis used to live in library.db; move both tables into the attached file
        moved += await _relocate_legacy_table(
            database, logger, DB_TABLE_AUDIO_ANALYSIS, _ANALYSIS_COLUMNS
        )
        moved += await _relocate_legacy_table(
            database, logger, DB_TABLE_AUDIO_ANALYSIS_FAILURES, _FAILURE_COLUMNS
        )

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


async def _relocate_legacy_table(
    database: DatabaseConnection,
    logger: logging.Logger,
    table: str,
    columns: tuple[str, ...],
) -> int:
    """
    Copy a legacy main.<table> into the attached db in id batches, then drop it.

    Conflicts on the natural key keep the newer row, so a retry never overwrites a newer
    destination row and rows a downgraded build wrote win over older copies.

    :param database: The music library connection the analysis database is attached to.
    :param logger: Logger to report progress on.
    :param table: Name of the legacy table in library.db (main schema) to relocate.
    :param columns: Column names (excluding id) shared by main.<table> and aa.<table>.
    :returns: Number of rows in the dropped source (0 if absent).
    """
    exists = await database.get_rows_from_query(
        "SELECT 1 FROM main.sqlite_master WHERE type = 'table' AND name = :name",
        {"name": table},
        limit=1,
    )
    if not exists:
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
    cols = ", ".join(columns)
    updates = ", ".join(f"{column} = excluded.{column}" for column in columns)
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
    # verify by natural key, not row count: a live write can consume aa's own
    # AUTOINCREMENT sequence, so aa's count alone can't prove every legacy row landed
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
