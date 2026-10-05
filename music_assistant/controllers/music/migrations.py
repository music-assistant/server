"""
Database schema migration logic for the music library database.

Holds the versioned, step-by-step migrations that bring an existing library
database up to the current ``DB_SCHEMA_VERSION``. Kept separate from the
controller/connection setup so this (large) migration code stays isolated and
individually testable.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sqlite3
from contextlib import suppress
from datetime import datetime
from typing import TYPE_CHECKING, cast

from music_assistant_models.enums import MediaType
from music_assistant_models.errors import MusicAssistantError, ProviderUnavailableError
from music_assistant_models.helpers import create_safe_string

from music_assistant.constants import (
    DB_TABLE_ALBUMS,
    DB_TABLE_ARTISTS,
    DB_TABLE_AUDIO_ANALYSIS,
    DB_TABLE_AUDIO_ANALYSIS_FAILURES,
    DB_TABLE_AUDIOBOOKS,
    DB_TABLE_EXTERNAL_ID_LOOKUP,
    DB_TABLE_FAVORITES,
    DB_TABLE_GENRE_MEDIA_ITEM_EXCLUSION,
    DB_TABLE_GENRE_MEDIA_ITEM_MAPPING,
    DB_TABLE_GENRES,
    DB_TABLE_LOUDNESS_MEASUREMENTS,
    DB_TABLE_PLAYLISTS,
    DB_TABLE_PLAYLOG,
    DB_TABLE_PODCASTS,
    DB_TABLE_PROVIDER_MAPPINGS,
    DB_TABLE_RADIOS,
    DB_TABLE_TRACKS,
    DEFAULT_GENRE_MAPPING,
    GENRE_ICONS_DIR_NAME,
    LOUDNESS_MEASUREMENT_MIN_LUFS,
    MEDIA_ITEM_DB_TABLES,
)
from music_assistant.controllers.music.constants import (
    AUDIO_ANALYSIS_MOVE_BATCH_SIZE,
    DB_SCHEMA_VERSION,
)
from music_assistant.controllers.music.favorites import PENDING_USER_ID
from music_assistant.controllers.music.media.genres import GenreController
from music_assistant.controllers.streams.audio_analysis_database import create_analysis_tables
from music_assistant.controllers.streams.constants import AA_DB_FILENAME
from music_assistant.helpers.json import json_dumps, json_loads, serialize_to_json
from music_assistant.helpers.lyrics import normalize_lrc_lyrics

if TYPE_CHECKING:
    import logging
    from collections.abc import Awaitable, Callable

    from music_assistant import MusicAssistant
    from music_assistant.helpers.database import DatabaseConnection


# schema name the analysis database is attached under while legacy rows are moved into it
_AUDIO_ANALYSIS_SCHEMA = "aa"
_ANALYSIS_COLUMNS = (
    "media_type",
    "item_id",
    "provider",
    "aa_provider_domain",
    "analysis_data",
    "analysis_version",
    "timestamp_created",
)
_FAILURE_COLUMNS = (
    "media_type",
    "item_id",
    "provider",
    "aa_provider_domain",
    "reason",
    "analysis_version",
    "next_retry",
    "timestamp_created",
)


async def ensure_legacy_audio_analysis_table(database: DatabaseConnection) -> None:
    """
    Create the pre-2.9 audio_analysis table in library.db if it is missing.

    Only the v38 loudness migration writes to it; the AudioAnalysisController relocates the
    rows into audio_analysis.db right after the music database has been set up.
    """
    await database.execute(
        f"""CREATE TABLE IF NOT EXISTS {DB_TABLE_AUDIO_ANALYSIS}(
                [id] INTEGER PRIMARY KEY AUTOINCREMENT,
                [media_type] TEXT NOT NULL,
                [item_id] TEXT NOT NULL,
                [provider] TEXT NOT NULL,
                [aa_provider_domain] TEXT NOT NULL,
                [analysis_data] json NOT NULL,
                [analysis_version] INTEGER DEFAULT 1,
                [timestamp_created] INTEGER DEFAULT (cast(strftime('%s','now') as int)),
                UNIQUE(item_id,provider,aa_provider_domain,media_type));"""
    )


async def migrate_database(  # noqa: PLR0915
    mass: MusicAssistant,
    database: DatabaseConnection,
    logger: logging.Logger,
    prev_version: int,
    create_tables: Callable[[], Awaitable[None]],
) -> None:
    """
    Migrate the library database from a previous schema version to the current one.

    :param prev_version: the schema version currently stored in the database.
    :param create_tables: callback that (re)creates the current table schema, used by
        the migration steps that rebuild a table from scratch.
    """
    logger.info("Migrating database from version %s to %s", prev_version, DB_SCHEMA_VERSION)

    if prev_version < 15:
        raise MusicAssistantError("Database schema version too old to migrate")

    if prev_version <= 15:
        # add search_name and search_sort_name columns to all tables
        # and populate them with the name and sort_name values
        # this is to allow for local/case independent searches
        for table in (
            DB_TABLE_TRACKS,
            DB_TABLE_ALBUMS,
            DB_TABLE_ARTISTS,
            DB_TABLE_RADIOS,
            DB_TABLE_PLAYLISTS,
            DB_TABLE_AUDIOBOOKS,
            DB_TABLE_PODCASTS,
        ):
            try:
                await database.execute(
                    f"ALTER TABLE {table} ADD COLUMN search_name TEXT DEFAULT '' NOT NULL"
                )
                await database.execute(
                    f"ALTER TABLE {table} ADD COLUMN search_sort_name TEXT DEFAULT '' NOT NULL"
                )
            except Exception as err:
                if "duplicate column" not in str(err):
                    raise
            # migrate all existing values
            async for db_row in database.iter_items(table):
                await database.update(
                    table,
                    {"item_id": db_row["item_id"]},
                    {
                        "search_name": create_safe_string(db_row["name"], True, True),
                        "search_sort_name": create_safe_string(db_row["sort_name"], True, True),
                    },
                )

    if prev_version <= 16:
        # cleanup invalid release_date field in metadata
        for table in (
            DB_TABLE_TRACKS,
            DB_TABLE_ALBUMS,
            DB_TABLE_AUDIOBOOKS,
            DB_TABLE_PODCASTS,
        ):
            async for db_row in database.iter_items(table):
                if '"release_date":null' in db_row["metadata"]:
                    continue
                metadata = json_loads(db_row["metadata"])
                try:
                    datetime.fromisoformat(metadata["release_date"])
                except KeyError, ValueError:
                    # this is not a valid date, so we set it to None
                    metadata["release_date"] = None
                    await database.update(
                        table,
                        {"item_id": db_row["item_id"]},
                        {
                            "metadata": serialize_to_json(metadata),
                        },
                    )

    if prev_version <= 17:
        # migrate triggers to auto update timestamps
        # it had an error in the previous version where it was not created
        for db_table in (
            "artists",
            "albums",
            "tracks",
            "playlists",
            "radios",
            "audiobooks",
            "podcasts",
        ):
            await database.execute(f"DROP TRIGGER IF EXISTS update_{db_table}_timestamp;")

    if prev_version <= 18:
        # add in_library column to provider_mappings table
        await database.execute(
            f"ALTER TABLE {DB_TABLE_PROVIDER_MAPPINGS} ADD COLUMN in_library "
            "BOOLEAN NOT NULL DEFAULT 0;"
        )
        # migrate existing entries in provider_mappings which are filesystem
        await database.execute(
            f"UPDATE {DB_TABLE_PROVIDER_MAPPINGS} SET in_library = 1 "
            "WHERE provider_domain in ('filesystem_local', 'filesystem_smb');"
        )

    if prev_version <= 20:
        # drop column cache_checksum from playlists table
        # this is no longer used and is a leftover from previous designs
        try:
            await database.execute(f"ALTER TABLE {DB_TABLE_PLAYLISTS} DROP COLUMN cache_checksum")
        except Exception as err:
            if "no such column" not in str(err):
                raise

    if prev_version <= 21:
        # drop table for smart fades analysis - it will be recreated with needed columns
        await database.execute("DROP TABLE IF EXISTS smart_fades_analysis")
        await create_tables()

    if prev_version <= 22:
        # add userid column to playlog table
        try:
            await database.execute(f"ALTER TABLE {DB_TABLE_PLAYLOG} ADD COLUMN userid TEXT")
        except Exception as err:
            if "duplicate column" not in str(err):
                raise
        # Note: SQLite doesn't support modifying constraints directly
        # The UNIQUE constraint will be updated when the table is recreated
        # For now, we'll keep the old constraint and add a new one via unique index
        try:
            await database.execute(f"DROP INDEX IF EXISTS {DB_TABLE_PLAYLOG}_unique_idx")
            await database.execute(
                f"CREATE UNIQUE INDEX {DB_TABLE_PLAYLOG}_unique_idx "
                f"ON {DB_TABLE_PLAYLOG}(item_id,provider,media_type,userid)"
            )
        except Exception as err:
            # If we can't create the index due to duplicate entries, log and continue
            logger.warning("Could not create unique index on playlog: %s", err)

    if prev_version <= 23:
        # add is_unique column to provider_mappings table
        try:
            await database.execute(
                f"ALTER TABLE {DB_TABLE_PROVIDER_MAPPINGS} ADD COLUMN is_unique BOOLEAN"
            )
        except Exception as err:
            if "duplicate column" not in str(err):
                raise

    if prev_version <= 24:
        # add queue_id and user_initiated columns to playlog table
        try:
            await database.execute(f"ALTER TABLE {DB_TABLE_PLAYLOG} ADD COLUMN queue_id TEXT")
        except Exception as err:
            if "duplicate column" not in str(err):
                raise
        try:
            await database.execute(
                f"ALTER TABLE {DB_TABLE_PLAYLOG} "
                "ADD COLUMN user_initiated BOOLEAN NOT NULL DEFAULT 1"
            )
        except Exception as err:
            if "duplicate column" not in str(err):
                raise

    if prev_version <= 26:
        # force in_library=True for provider mappings from non-streaming providers
        # streaming providers will be automatically added to library when synced
        await database.execute(
            f"UPDATE {DB_TABLE_PROVIDER_MAPPINGS} SET in_library = 1 "
            "WHERE provider_domain NOT IN "
            "('spotify', 'deezer', 'tidal', 'qobuz', 'apple_music', 'ytmusic');"
        )
        # also set in_library=True for all radio items
        await database.execute(
            f"UPDATE {DB_TABLE_PROVIDER_MAPPINGS} SET in_library = 1 WHERE media_type = 'radio';"
        )
        # remove invalid playlist provider mappings for playlists which are not in library
        await database.execute(
            f"DELETE FROM {DB_TABLE_PROVIDER_MAPPINGS} "
            "WHERE media_type = 'playlist' AND in_library = 0;"
        )

    if prev_version <= 27:
        # set streaming provider mappings to in_library=True, but only for items
        # that do not already have any mapping with in_library=True
        # (to avoid overwriting explicit values in multi-instance setups)
        await database.execute(
            f"UPDATE {DB_TABLE_PROVIDER_MAPPINGS} SET in_library = 1 "
            "WHERE provider_domain NOT IN "
            "('filesystem_local', 'builtin', 'test', 'jellyfin', 'emby', "
            "'plex', 'opensubsonic', 'audiobookshelf', 'gpodder', 'podcastfeed') "
            "AND NOT EXISTS ("
            f"SELECT 1 FROM {DB_TABLE_PROVIDER_MAPPINGS} AS pm2 "
            f"WHERE pm2.media_type = {DB_TABLE_PROVIDER_MAPPINGS}.media_type "
            f"AND pm2.item_id = {DB_TABLE_PROVIDER_MAPPINGS}.item_id "
            "AND pm2.in_library = 1)"
        )

    if prev_version <= 28:
        # create genre/alias tables
        await create_tables()

        # Use raw aiosqlite connection for bulk operations.
        db = database._db

        empty_metadata = serialize_to_json({})

        def _normalize_name(raw_name: str) -> tuple[str, str, str, str]:
            name = raw_name.strip()
            sort_name = name
            search_name = create_safe_string(name, True, True)
            search_sort_name = create_safe_string(sort_name or "", True, True)
            return name, sort_name, search_name, search_sort_name

        genre_cache: dict[str, int] = {}

        genre_insert_sql = (
            f"INSERT OR IGNORE INTO {DB_TABLE_GENRES}"
            "(name, sort_name, translation_key, description, "
            "metadata, genre_aliases, play_count, last_played, "
            "search_name, search_sort_name) "
            "VALUES (?, ?, ?, NULL, ?, ?, 0, 0, ?, ?)"
        )
        genre_select_sql = f"SELECT item_id FROM {DB_TABLE_GENRES} WHERE search_name = ?"

        async def _get_or_create_genre(
            raw_name: str,
            aliases: list[str] | None = None,
            translation_key: str | None = None,
        ) -> int:
            name, sort_name, search_name, search_sort_name = _normalize_name(raw_name)
            if not search_name:
                return 0
            if search_name in genre_cache:
                return genre_cache[search_name]
            aliases_json = serialize_to_json(aliases or [name])
            icon_metadata = GenreController._get_genre_icon_metadata(translation_key)
            metadata_json = (
                serialize_to_json(icon_metadata.to_dict()) if icon_metadata else empty_metadata
            )
            row_id = await db.execute_insert(
                genre_insert_sql,
                (
                    name,
                    sort_name,
                    translation_key,
                    metadata_json,
                    aliases_json,
                    search_name,
                    search_sort_name,
                ),
            )
            if row_id and row_id[0]:
                genre_cache[search_name] = row_id[0]
                return cast("int", row_id[0])
            async with db.execute(genre_select_sql, (search_name,)) as cursor:
                row = await cursor.fetchone()
                if row:
                    genre_cache[search_name] = row[0]
                    return cast("int", row[0])
            return 0

        # Phase 1: Seed DEFAULT_GENRE_MAPPING — create genres with aliases.
        # Build n:n lookup: normalized alias name -> list of genre_ids.
        # One alias can belong to multiple genres (e.g. "funk" is both
        # a standalone genre and an alias of Soul/R&B).
        alias_to_genre: dict[str, list[int]] = {}
        for entry in DEFAULT_GENRE_MAPPING:
            genre_name = entry.get("genre")
            if not genre_name:
                continue
            all_aliases = [genre_name, *entry.get("aliases", [])]
            genre_id = await _get_or_create_genre(
                genre_name,
                aliases=all_aliases,
                translation_key=entry.get("translation_key"),
            )
            if not genre_id:
                continue
            for alias in all_aliases:
                norm = create_safe_string(alias.strip(), True, True)
                if norm:
                    alias_to_genre.setdefault(norm, [])
                    if genre_id not in alias_to_genre[norm]:
                        alias_to_genre[norm].append(genre_id)
        await db.commit()

        # Phase 2: Discover unique genre names from all media items,
        # create genres for unknown names, then bulk-insert mappings.
        media_tables = (
            (DB_TABLE_TRACKS, MediaType.TRACK),
            (DB_TABLE_ALBUMS, MediaType.ALBUM),
            (DB_TABLE_ARTISTS, MediaType.ARTIST),
            (DB_TABLE_PLAYLISTS, MediaType.PLAYLIST),
            (DB_TABLE_RADIOS, MediaType.RADIO),
            (DB_TABLE_AUDIOBOOKS, MediaType.AUDIOBOOK),
            (DB_TABLE_PODCASTS, MediaType.PODCAST),
        )

        # 2a: Extract all unique raw genre names from metadata
        union_parts = [
            f"SELECT DISTINCT TRIM(g.value) AS raw_name "
            f"FROM {table}, json_each(json_extract({table}.metadata, '$.genres')) AS g "
            f"WHERE json_extract({table}.metadata, '$.genres') IS NOT NULL "
            f"AND json_extract({table}.metadata, '$.genres') != '[]'"
            for table, _ in media_tables
        ]
        unique_names_sql = " UNION ".join(union_parts)
        logger.debug("Genre migration - unique names query:\n%s", unique_names_sql)
        async with db.execute(unique_names_sql) as cursor:
            unique_raw_names = [row[0] for row in await cursor.fetchall() if row[0]]
        logger.info("Genre migration - discovered %d unique genre names", len(unique_raw_names))

        # 2b: Ensure genres exist for all discovered names.
        # Names already covered by Phase 1 aliases just reuse those genre(s).
        # New names get their own genre. One alias can map to multiple genres (n:n).
        raw_name_to_genres: dict[str, list[int]] = {}
        for raw_name in unique_raw_names:
            norm = create_safe_string(raw_name.strip(), True, True)
            if not norm:
                continue
            if norm in alias_to_genre:
                raw_name_to_genres[raw_name] = list(alias_to_genre[norm])
                logger.debug(
                    "Genre migration - resolved %r -> genre_ids %s (alias match)",
                    raw_name,
                    alias_to_genre[norm],
                )
            else:
                genre_id = await _get_or_create_genre(raw_name)
                if genre_id:
                    raw_name_to_genres[raw_name] = [genre_id]
                    alias_to_genre[norm] = [genre_id]
                    logger.debug(
                        "Genre migration - resolved %r -> genre_id %d (new genre)",
                        raw_name,
                        genre_id,
                    )
        await db.commit()
        logger.info("Genre migration - resolved %d unique genre names", len(raw_name_to_genres))

        # 2c: Add discovered raw names as aliases to their resolved genres
        # so that frontend searches by raw name find the parent genre.
        genre_new_aliases: dict[int, list[str]] = {}
        for raw_name, gids in raw_name_to_genres.items():
            for gid in gids:
                genre_new_aliases.setdefault(gid, []).append(raw_name)
        for gid, new_aliases in genre_new_aliases.items():
            async with db.execute(
                f"SELECT genre_aliases FROM {DB_TABLE_GENRES} WHERE item_id = :gid",
                {"gid": gid},
            ) as cursor:
                row = await cursor.fetchone()
            if not row:
                continue
            existing = json_loads(row[0]) if row[0] else []
            existing_norms = {create_safe_string(a, True, True) for a in existing}
            to_add = [
                a for a in new_aliases if create_safe_string(a, True, True) not in existing_norms
            ]
            if to_add:
                merged = existing + to_add
                await db.execute(
                    f"UPDATE {DB_TABLE_GENRES} SET genre_aliases = :aliases WHERE item_id = :gid",
                    {"aliases": json_dumps(merged), "gid": gid},
                )
        await db.commit()

        # 2d: Build CTE with (raw_name, genre_id) and do one INSERT per
        # media type using json_each to map media items directly to genres.
        # One raw_name can map to multiple genre_ids (n:n).
        if raw_name_to_genres:
            cte_values = ", ".join(
                f"(LOWER('{name.replace(chr(39), chr(39) + chr(39))}'), {gid})"
                for name, gids in raw_name_to_genres.items()
                for gid in gids
            )
            cte = f"WITH genre_lookup(raw_name, genre_id) AS (VALUES {cte_values})"

            for table, media_type in media_tables:
                full_query = (
                    f"{cte} INSERT OR REPLACE INTO {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING}"
                    f"(genre_id, media_id, media_type, alias) "
                    f"SELECT gl.genre_id, {table}.item_id, "
                    f"'{media_type.value}', TRIM(g.value) "
                    f"FROM {table}, "
                    f"json_each(json_extract({table}.metadata, '$.genres')) AS g "
                    f"JOIN genre_lookup gl ON gl.raw_name = LOWER(TRIM(g.value)) "
                    f"WHERE json_extract({table}.metadata, '$.genres') IS NOT NULL "
                    f"AND json_extract({table}.metadata, '$.genres') != '[]'"
                )
                logger.debug("Genre migration - %s query:\n%s", media_type.value, full_query)
                await db.execute(full_query)
                await db.commit()

    if prev_version <= 29:
        # Smart fades analyses were previously computed on silence-stripped audio,
        # so beat timestamps are misaligned with the unstripped buffers now passed
        # to the crossfade mixer. Truncate the table so all analyses are re-computed.
        with suppress(Exception):
            await database.execute("DELETE FROM smart_fades_analysis")

    if prev_version <= 30:
        # add supported_mediatypes column to playlist table, and make {MediaType.TRACK},
        # i.e. ["track"] the default, as this was the only media type supported.
        try:
            await database.execute(
                f"ALTER TABLE {DB_TABLE_PLAYLISTS} ADD COLUMN supported_mediatypes"
                " json DEFAULT '[\"track\"]' NOT NULL"
            )
        except Exception as err:
            if "duplicate column" not in str(err):
                raise

    if prev_version <= 31:
        # create the genre_media_item_exclusion table (new in schema 31)
        await database.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {DB_TABLE_GENRE_MEDIA_ITEM_EXCLUSION}(
            [genre_id] INTEGER NOT NULL,
            [media_id] INTEGER NOT NULL,
            [media_type] TEXT NOT NULL,
            FOREIGN KEY([genre_id]) REFERENCES [genres]([item_id]),
            UNIQUE(genre_id, media_id, media_type)
            );"""
        )
        await database.execute(
            f"CREATE INDEX IF NOT EXISTS {DB_TABLE_GENRE_MEDIA_ITEM_EXCLUSION}_media_idx "
            f"on {DB_TABLE_GENRE_MEDIA_ITEM_EXCLUSION}(media_id,media_type);"
        )
        await database.execute(
            f"CREATE INDEX IF NOT EXISTS {DB_TABLE_GENRE_MEDIA_ITEM_EXCLUSION}_genre_idx "
            f"on {DB_TABLE_GENRE_MEDIA_ITEM_EXCLUSION}(genre_id);"
        )

    if prev_version <= 32:
        # recreate genre_media_item_mapping with nullable alias and is_derived column
        # (new in schema 33 to support propagated genre mappings from tracks)
        await database.execute(
            f"ALTER TABLE {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING} "
            f"RENAME TO {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING}_old;"
        )
        await database.execute(
            f"""
            CREATE TABLE {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING}(
            [genre_id] INTEGER NOT NULL,
            [media_id] INTEGER NOT NULL,
            [media_type] TEXT NOT NULL,
            [alias] TEXT,
            [is_derived] BOOLEAN NOT NULL DEFAULT 0,
            FOREIGN KEY([genre_id]) REFERENCES [genres]([item_id]),
            UNIQUE(genre_id, media_id, media_type)
            );"""
        )
        await database.execute(
            f"INSERT INTO {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING} "
            f"(genre_id, media_id, media_type, alias) "
            f"SELECT genre_id, media_id, media_type, alias "
            f"FROM {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING}_old;"
        )
        await database.execute(f"DROP TABLE {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING}_old;")

    if prev_version <= 33:
        # add is_excluded column to genres table (new in schema 34)
        try:
            await database.execute(
                f"ALTER TABLE {DB_TABLE_GENRES} "
                "ADD COLUMN [is_excluded] BOOLEAN NOT NULL DEFAULT 0;"
            )
        except Exception as err:
            if "duplicate column" not in str(err):
                raise
        # drop the old genre_global_exclusion table (replaced by is_excluded column)
        await database.execute("DROP TABLE IF EXISTS genre_global_exclusion;")
        # add is_default column to genres table (new in schema 34)
        try:
            await database.execute(
                f"ALTER TABLE {DB_TABLE_GENRES} ADD COLUMN [is_default] BOOLEAN NOT NULL DEFAULT 0;"
            )
        except Exception as err:
            if "duplicate column" not in str(err):
                raise
        # mark all existing genres with a translation_key as default
        await database.execute(
            f"UPDATE {DB_TABLE_GENRES} SET is_default = 1 WHERE translation_key IS NOT NULL;"
        )
    if prev_version <= 34:
        # fix filesystem playlists missing in_library flag
        await database.execute(
            f"UPDATE {DB_TABLE_PROVIDER_MAPPINGS} SET in_library = 1 "
            "WHERE media_type = 'playlist' "
            "AND provider_domain IN ('filesystem_local', 'filesystem_smb', 'filesystem_nfs');"
        )

    if prev_version <= 35:
        # add is_dynamic column to playlist table
        try:
            await database.execute(
                f"ALTER TABLE {DB_TABLE_PLAYLISTS} ADD COLUMN is_dynamic BOOLEAN NOT NULL DEFAULT 0"
            )
        except Exception as err:
            if "duplicate column" not in str(err):
                raise
        # backfill is_dynamic for existing Apple Music station playlists
        await database.execute(
            f"UPDATE {DB_TABLE_PLAYLISTS} SET is_dynamic = 1 "
            f"WHERE item_id IN ("
            f"  SELECT item_id FROM {DB_TABLE_PROVIDER_MAPPINGS} "
            f"  WHERE media_type = 'playlist' "
            f"  AND provider_domain = 'apple_music' "
            f"  AND provider_item_id LIKE 'ra.%'"
            f")"
        )

    if prev_version <= 36:
        # drop legacy smart_fades_analysis table — analysis is now handled by
        # audio analysis providers and stored in the audio_analysis table.
        await database.execute("DROP TABLE IF EXISTS smart_fades_analysis")

    if prev_version <= 37:
        # purge unreliable loudness measurements persisted by earlier versions
        # (ebur128 reports ~-70 LUFS on near-silence / early-cancelled streams,
        # which caused huge gain corrections on subsequent plays)
        await database.execute(
            f"DELETE FROM {DB_TABLE_LOUDNESS_MEASUREMENTS} "
            f"WHERE loudness <= {LOUDNESS_MEASUREMENT_MIN_LUFS}"
        )
        await database.execute(
            f"UPDATE {DB_TABLE_LOUDNESS_MEASUREMENTS} "
            f"SET loudness_album = NULL "
            f"WHERE loudness_album <= {LOUDNESS_MEASUREMENT_MIN_LUFS}"
        )

    if prev_version <= 38:
        # stable 2.8.9 shipped schema v38 without the smart_fades_analysis drop
        # (that drop is gated at <= 36, which v38 users leapfrog). re-run it here
        # so stable->2.9.0 upgraders also lose the legacy table. idempotent: a
        # no-op for beta users who already dropped it at v36.
        await database.execute("DROP TABLE IF EXISTS smart_fades_analysis")
        # migrate loudness measurements to the unified audio_analysis table
        # under the new builtin loudness_analysis provider, then drop the
        # legacy table. album loudness rides along when present.
        await ensure_legacy_audio_analysis_table(database)
        await database.execute(
            f"INSERT OR IGNORE INTO {DB_TABLE_AUDIO_ANALYSIS} "
            f"(media_type, item_id, provider, aa_provider_domain, "
            f" analysis_data, analysis_version) "
            f"SELECT media_type, item_id, provider, 'loudness_analysis', "
            f"       json_object("
            f"           'loudness_integrated', loudness, "
            f"           'loudness_album', loudness_album"
            f"       ), 1 "
            f"FROM {DB_TABLE_LOUDNESS_MEASUREMENTS} "
            f"WHERE loudness IS NOT NULL "
            f"  AND loudness > {LOUDNESS_MEASUREMENT_MIN_LUFS}"
        )
        await database.execute(f"DROP TABLE IF EXISTS {DB_TABLE_LOUDNESS_MEASUREMENTS}")

    if prev_version <= 39:
        # add is_manual column to genre_media_item_mapping
        try:
            await database.execute(
                f"ALTER TABLE {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING} "
                "ADD COLUMN [is_manual] BOOLEAN NOT NULL DEFAULT 0;"
            )
        except Exception as err:
            if "duplicate column" not in str(err):
                raise

    if prev_version <= 40:
        # genre icons were previously stored with an absolute filesystem path to
        # the builtin SVG, which is install-location dependent. after a runtime
        # upgrade or relocation (e.g. the python3.13 -> python3.14 site-packages
        # move) that path no longer existed, so genre icons 404'd via imageproxy.
        # rewrite them to the install-independent "<GENRE_ICONS_DIR_NAME>/<file>"
        # form; the builtin provider resolves that against RESOURCES_DIR at serve
        # time.
        genre_dir_marker = f"/resources/{GENRE_ICONS_DIR_NAME}/"
        async for db_row in database.iter_items(DB_TABLE_GENRES):
            raw_metadata = db_row["metadata"]
            if not raw_metadata:
                continue
            metadata = json_loads(raw_metadata)
            images = metadata.get("images")
            if not images:
                continue
            changed = False
            for image in images:
                path = image.get("path")
                if not (image.get("provider") == "builtin" and isinstance(path, str)):
                    continue
                norm = path.replace("\\", "/")
                if genre_dir_marker in norm and norm.endswith(".svg"):
                    image["path"] = f"{GENRE_ICONS_DIR_NAME}/{norm.rsplit('/', 1)[-1]}"
                    changed = True
            if changed:
                await database.update(
                    DB_TABLE_GENRES,
                    {"item_id": db_row["item_id"]},
                    {"metadata": serialize_to_json(metadata)},
                )

    if prev_version <= 41:
        # add playback_speed column to playlog (per-item speed for audiobooks/episodes)
        try:
            await database.execute(
                f"ALTER TABLE {DB_TABLE_PLAYLOG} "
                "ADD COLUMN playback_speed REAL NOT NULL DEFAULT 1.0"
            )
        except Exception as err:
            if "duplicate column" not in str(err):
                raise

    if prev_version <= 42:
        # add translation_key/translation_params columns to the playlist table so localizable
        # builtin/provider playlist names (incl. parameterized ones like Spotify's per-account
        # "Liked Songs") survive the library round-trip; existing rows backfill on the next sync.
        for column in ("[translation_key] TEXT", "[translation_params] json"):
            try:
                await database.execute(f"ALTER TABLE {DB_TABLE_PLAYLISTS} ADD COLUMN {column}")
            except Exception as err:
                if "duplicate column" not in str(err):
                    raise

    if prev_version <= 43:
        # add content_type column to the genres table to namespace spoken-word taxonomies
        # (podcast/audiobook) apart from music genres. NULL = music/general; existing rows
        # stay NULL so nothing re-keys.
        try:
            await database.execute(f"ALTER TABLE {DB_TABLE_GENRES} ADD COLUMN [content_type] TEXT")
        except Exception as err:
            if "duplicate column" not in str(err):
                raise

    if prev_version <= 44:
        # add artist_type column to artist table, and make
        # artist_type=ARTIST_TYPE.SINGER the default, as this was the only artist type supported
        try:
            await database.execute(
                f"ALTER TABLE {DB_TABLE_ARTISTS} ADD COLUMN artist_type TEXT DEFAULT 'singer' NOT NULL"
            )
        except Exception as err:
            if "duplicate column" not in str(err):
                raise

    if prev_version <= 46:
        # add artists column to playlog (lightweight artist mappings for track rows) so
        # recency matching can recognize the same song across different releases/providers
        try:
            await database.execute(f"ALTER TABLE {DB_TABLE_PLAYLOG} ADD COLUMN artists json")
        except Exception as err:
            if "duplicate column" not in str(err):
                raise

    if prev_version <= 48:
        # databases from before the userid column still carry the original inline
        # UNIQUE(item_id, provider, media_type) constraint, which ALTER TABLE could not
        # remove. It collides with the per-user upsert (ON CONFLICT on 4 columns) and
        # raises IntegrityError on every replay of an item. SQLite can only drop an
        # inline constraint by rebuilding the table.
        stale_unique = False
        for index in await database.get_rows_from_query(
            f"PRAGMA index_list({DB_TABLE_PLAYLOG})", limit=0
        ):
            if not index["unique"]:
                continue
            index_columns = {
                column["name"]
                for column in await database.get_rows_from_query(
                    f"PRAGMA index_info({index['name']})", limit=0
                )
            }
            if "userid" not in index_columns:
                stale_unique = True
                break
        if stale_unique:
            logger.info("Rebuilding playlog table to update its unique constraint")
            await database.execute(
                f"ALTER TABLE {DB_TABLE_PLAYLOG} RENAME TO {DB_TABLE_PLAYLOG}_old"
            )
            await database.execute(
                f"""CREATE TABLE {DB_TABLE_PLAYLOG}(
                    [id] INTEGER PRIMARY KEY AUTOINCREMENT,
                    [item_id] TEXT NOT NULL,
                    [provider] TEXT NOT NULL,
                    [media_type] TEXT NOT NULL,
                    [name] TEXT NOT NULL,
                    [image] json,
                    [artists] json,
                    [timestamp] INTEGER DEFAULT 0,
                    [fully_played] BOOLEAN,
                    [seconds_played] INTEGER,
                    [userid] TEXT NOT NULL,
                    [queue_id] TEXT,
                    [user_initiated] BOOLEAN NOT NULL DEFAULT 1,
                    [playback_speed] REAL NOT NULL DEFAULT 1.0,
                    UNIQUE(item_id, provider, media_type, userid));"""
            )
            # rows from before the userid column existed have no owner and cannot be
            # kept under the NOT NULL schema
            await database.execute(
                f"INSERT INTO {DB_TABLE_PLAYLOG} "
                "(id, item_id, provider, media_type, name, image, artists, timestamp, "
                "fully_played, seconds_played, userid, queue_id, user_initiated, "
                "playback_speed) "
                "SELECT id, item_id, provider, media_type, name, image, artists, timestamp, "
                "fully_played, seconds_played, userid, queue_id, user_initiated, "
                f"playback_speed FROM {DB_TABLE_PLAYLOG}_old WHERE userid IS NOT NULL"
            )
            await database.execute(f"DROP TABLE {DB_TABLE_PLAYLOG}_old")

    if prev_version <= 50:
        # external id matching moved from a (unindexable) LIKE scan on the external_ids
        # JSON column to the new external_id_lookup table, which is now the single source
        # of truth: backfill the lookup rows from the external_ids JSON of all media item
        # tables, then drop that column and its old index (which could never be used by
        # the LIKE scan anyway). The backfill is idempotent, so v50 databases (which
        # already have a populated lookup table) simply get the column drop.
        for media_type, table in (
            (MediaType.ARTIST, DB_TABLE_ARTISTS),
            (MediaType.ALBUM, DB_TABLE_ALBUMS),
            (MediaType.TRACK, DB_TABLE_TRACKS),
            (MediaType.PLAYLIST, DB_TABLE_PLAYLISTS),
            (MediaType.RADIO, DB_TABLE_RADIOS),
            (MediaType.AUDIOBOOK, DB_TABLE_AUDIOBOOKS),
            (MediaType.PODCAST, DB_TABLE_PODCASTS),
            (MediaType.GENRE, DB_TABLE_GENRES),
        ):
            # tables (re)created by an earlier migration step already use the current
            # schema (no external_ids column) and have nothing to backfill
            table_columns = {
                column["name"]
                for column in await database.get_rows_from_query(
                    f"PRAGMA table_info({table})", limit=0
                )
            }
            if "external_ids" not in table_columns:
                continue
            # the column must not be indexed for DROP COLUMN to succeed
            await database.execute(f"DROP INDEX IF EXISTS {table}_external_ids_idx")
            # external_ids is a JSON array of [type, value] pairs; the NOCASE unique
            # index may collapse case-variants of the same id, hence OR IGNORE
            await database.execute(
                f"INSERT OR IGNORE INTO {DB_TABLE_EXTERNAL_ID_LOOKUP} "
                "(media_type, external_id_type, external_id, item_id) "
                f"SELECT '{media_type.value}', json_extract(ext.value, '$[0]'), "
                f"json_extract(ext.value, '$[1]'), {table}.item_id "
                f"FROM {table}, json_each({table}.external_ids) AS ext "
                "WHERE json_extract(ext.value, '$[0]') IS NOT NULL "
                "AND json_extract(ext.value, '$[1]') IS NOT NULL"
            )
            await database.execute(f"ALTER TABLE {table} DROP COLUMN external_ids")

    if prev_version <= 52:
        audio_analysis_table_exists = await database.get_rows_from_query(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = :table_name",
            {"table_name": DB_TABLE_AUDIO_ANALYSIS},
            limit=1,
        )
        if audio_analysis_table_exists:
            # SQLite does not guarantee WHERE-term evaluation order, so a bare
            # json_valid() term cannot reliably shield json_each()/json_type()
            # from raising on malformed rows - guard their input directly instead.
            # The json() wrapper is required: the JSON subtype does not reliably
            # survive the scalar-subquery boundary, so without it the rebuilt
            # array would be stored as an escaped string on some SQLite versions.
            result = await database.execute(
                f"""UPDATE {DB_TABLE_AUDIO_ANALYSIS} AS aa
                SET analysis_data = json_replace(
                    aa.analysis_data,
                    '$.spectral_centroid',
                    json((
                        SELECT json_group_array(
                            CASE WHEN centroid.type = 'null'
                                THEN 0.0 ELSE centroid.value END
                        )
                        FROM json_each(
                            aa.analysis_data, '$.spectral_centroid'
                        ) AS centroid
                    ))
                )
                WHERE aa.aa_provider_domain = :aa_provider_domain
                    AND aa.analysis_data LIKE '%null%'
                    AND json_type(
                        CASE WHEN json_valid(aa.analysis_data)
                            THEN aa.analysis_data END,
                        '$.spectral_centroid'
                    ) = 'array'
                    AND EXISTS (
                        SELECT 1
                        FROM json_each(
                            CASE WHEN json_valid(aa.analysis_data)
                                THEN aa.analysis_data END,
                            '$.spectral_centroid'
                        ) AS centroid
                        WHERE centroid.type = 'null'
                    )""",
                {"aa_provider_domain": "smart_fades"},
            )
            if result.rowcount:
                logger.info(
                    "Repaired null spectral centroid values in %d Smart Fades "
                    "audio analysis row(s)",
                    result.rowcount,
                )

    if prev_version <= 53:
        # normalize stored synced lyrics: strip LRC ID tags and expand multi-timestamp
        # (repeating) lines into one line per timestamp
        tracks_columns = {
            x["name"]
            for x in await database.get_rows_from_query(
                f"PRAGMA table_info({DB_TABLE_TRACKS})", limit=0
            )
        }
        repaired_lyrics_rows = 0
        if "metadata" in tracks_columns:
            # guard against (test) databases with stand-in tables
            async for db_row in database.iter_items(DB_TABLE_TRACKS):
                if not db_row["metadata"] or '"lrc_lyrics"' not in db_row["metadata"]:
                    continue
                try:
                    metadata = json_loads(db_row["metadata"])
                except ValueError:
                    # corrupt metadata rows are handled elsewhere (diagnostics), skip here
                    continue
                lrc_lyrics = metadata.get("lrc_lyrics")
                if not isinstance(lrc_lyrics, str):
                    continue
                normalized = normalize_lrc_lyrics(lrc_lyrics)
                if normalized == lrc_lyrics:
                    continue
                metadata["lrc_lyrics"] = normalized
                await database.update(
                    DB_TABLE_TRACKS,
                    {"item_id": db_row["item_id"]},
                    {"metadata": serialize_to_json(metadata)},
                )
                repaired_lyrics_rows += 1
        if repaired_lyrics_rows:
            logger.info("Normalized synced lyrics of %d track(s)", repaired_lyrics_rows)

    if prev_version <= 54:
        # apple music blobstore artwork URLs are presigned with a ~24h expiry and are
        # no longer persisted: replace the stored (long-dead) signed URLs with the
        # stable artwork token the provider resolves to a fresh URL on demand
        migrated_artwork_rows = 0
        for table, media_type_value in (
            (DB_TABLE_ARTISTS, "artist"),
            (DB_TABLE_ALBUMS, "album"),
            (DB_TABLE_TRACKS, "track"),
            (DB_TABLE_PLAYLISTS, "playlist"),
        ):
            table_columns = {
                x["name"]
                for x in await database.get_rows_from_query(f"PRAGMA table_info({table})", limit=0)
            }
            if "metadata" not in table_columns:
                # guard against (test) databases with stand-in tables
                continue
            # the (provider_instance, item_id) -> provider item id lookup needed to
            # derive each item's artwork token from its apple music mapping
            apple_item_ids = {
                (row["item_id"], row["provider_instance"]): row["provider_item_id"]
                for row in await database.get_rows_from_query(
                    f"SELECT item_id, provider_instance, provider_item_id "
                    f"FROM {DB_TABLE_PROVIDER_MAPPINGS} "
                    "WHERE media_type = :media_type AND provider_domain = 'apple_music'",
                    {"media_type": media_type_value},
                    limit=0,
                )
            }
            async for db_row in database.iter_items(table):
                if not db_row["metadata"] or "blobstore.apple.com" not in db_row["metadata"]:
                    continue
                try:
                    metadata = json_loads(db_row["metadata"])
                except ValueError:
                    # corrupt metadata rows are handled elsewhere (diagnostics), skip here
                    continue
                images = metadata.get("images")
                if not isinstance(images, list):
                    continue
                migrated_images = []
                changed = False
                for image in images:
                    if not isinstance(image, dict) or "blobstore.apple.com" not in (
                        image.get("path") or ""
                    ):
                        migrated_images.append(image)
                        continue
                    changed = True
                    prov_item_id = apple_item_ids.get((db_row["item_id"], image.get("provider")))
                    if prov_item_id is None:
                        # no mapping left to resolve through; drop the dead url
                        continue
                    image["path"] = f"{media_type_value}/{prov_item_id}"
                    image["remotely_accessible"] = False
                    migrated_images.append(image)
                if not changed:
                    continue
                metadata["images"] = migrated_images
                await database.update(
                    table,
                    {"item_id": db_row["item_id"]},
                    {"metadata": serialize_to_json(metadata)},
                )
                migrated_artwork_rows += 1
        if migrated_artwork_rows:
            logger.info(
                "Migrated the Apple Music artwork of %d library item(s) to resolvable tokens",
                migrated_artwork_rows,
            )

    if prev_version <= 55:
        # drop the sound effect media type from the stored playlists: clients that do not
        # know it yet refuse to parse a playlist that advertises it. Rewriting the rows
        # here makes upgrading enough, instead of having to wait for the next library sync.
        await database.execute(
            f"UPDATE {DB_TABLE_PLAYLISTS} SET supported_mediatypes = json(("
            "SELECT json_group_array(value) FROM json_each"
            f"({DB_TABLE_PLAYLISTS}.supported_mediatypes) WHERE value != 'sound_effect'))"
            " WHERE json_valid(supported_mediatypes)"
            " AND supported_mediatypes LIKE '%sound_effect%'"
        )

    if prev_version <= 56:
        # the stable branch numbers its schema versions independently of this one, so a
        # stable database can report a version that leapfrogs steps it never ran: stable
        # 41-43 never got the columns this branch adds at <= 41 and <= 42. Re-add them for
        # every pre-57 database; the ALTERs are no-ops where the column already exists.
        for table, column in (
            (DB_TABLE_PLAYLISTS, "[translation_key] TEXT"),
            (DB_TABLE_PLAYLISTS, "[translation_params] json"),
            (DB_TABLE_PLAYLOG, "[playback_speed] REAL NOT NULL DEFAULT 1.0"),
        ):
            try:
                await database.execute(f"ALTER TABLE {table} ADD COLUMN {column}")
            except Exception as err:
                if "duplicate column" not in str(err):
                    raise

    if prev_version <= 57:
        # add is_dynamic column to radio table
        try:
            await database.execute(
                f"ALTER TABLE {DB_TABLE_RADIOS} ADD COLUMN is_dynamic BOOLEAN NOT NULL DEFAULT 0"
            )
        except Exception as err:
            if "duplicate column" not in str(err):
                raise

    if prev_version <= 58:
        # the access record (owner + sharing) of a Music Assistant playlist; NULL for every
        # existing row, which keeps them household playlists
        try:
            await database.execute(f"ALTER TABLE {DB_TABLE_PLAYLISTS} ADD COLUMN [access] json")
        except Exception as err:
            if "duplicate column" not in str(err):
                raise

    if prev_version <= 59:
        # a library item mapping has no provider of its own, but was briefly stored as a
        # self-referential mapping with the literal string "None" as domain and instance.
        # Such a mapping never resolves and makes the item page query a provider that does
        # not exist, so drop it.
        provider_mappings_table_exists = await database.get_rows_from_query(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = :table_name",
            {"table_name": DB_TABLE_PROVIDER_MAPPINGS},
            limit=1,
        )
        if provider_mappings_table_exists:
            await database.execute(
                f"DELETE FROM {DB_TABLE_PROVIDER_MAPPINGS} "
                "WHERE provider_domain = 'None' OR provider_instance = 'None'"
            )

    if prev_version <= 60:
        # favorites move from one shared column on every media item table to a row per user
        # in the favorites table. Whose like an existing favorite becomes depends on the
        # owners of the music sources and on the users, and neither is known here: the
        # access records are migrated and the auth database opened only once the webserver
        # is up. Every favorite is parked under a placeholder user id, which
        # FavoritesStore.settle_pending() hands out on that same start.
        await database.execute(
            f"""CREATE TABLE IF NOT EXISTS {DB_TABLE_FAVORITES}(
                [user_id] TEXT NOT NULL,
                [media_type] TEXT NOT NULL,
                [item_id] INTEGER NOT NULL,
                [favorite] BOOLEAN,
                [timestamp] INTEGER NOT NULL DEFAULT 0,
                UNIQUE(user_id, media_type, item_id));"""
        )
        await database.execute(
            f"CREATE INDEX IF NOT EXISTS {DB_TABLE_FAVORITES}_item_idx "
            f"on {DB_TABLE_FAVORITES}(media_type,item_id);"
        )
        for media_type, table in (
            (MediaType.ARTIST, DB_TABLE_ARTISTS),
            (MediaType.ALBUM, DB_TABLE_ALBUMS),
            (MediaType.TRACK, DB_TABLE_TRACKS),
            (MediaType.PLAYLIST, DB_TABLE_PLAYLISTS),
            (MediaType.RADIO, DB_TABLE_RADIOS),
            (MediaType.AUDIOBOOK, DB_TABLE_AUDIOBOOKS),
            (MediaType.PODCAST, DB_TABLE_PODCASTS),
            (MediaType.GENRE, DB_TABLE_GENRES),
        ):
            table_columns = {
                column["name"]
                for column in await database.get_rows_from_query(
                    f"PRAGMA table_info({table})", limit=0
                )
            }
            if "favorite" not in table_columns:
                # a table (re)created by an earlier migration step already has the column gone
                continue
            # the moment of the favorite is unknown; the item's last change is the best guess
            timestamp = (
                f"COALESCE({table}.timestamp_modified, 0)"
                if "timestamp_modified" in table_columns
                else "0"
            )
            await database.execute(
                f"INSERT OR IGNORE INTO {DB_TABLE_FAVORITES}"
                "(user_id, media_type, item_id, favorite, timestamp) "
                f"SELECT :user_id, :media_type, {table}.item_id, 1, {timestamp} "
                f"FROM {table} WHERE {table}.favorite = 1",
                {"user_id": PENDING_USER_ID, "media_type": media_type.value},
            )
            # the column must not be indexed for DROP COLUMN to succeed
            await database.execute(f"DROP INDEX IF EXISTS {table}_favorite_idx")
            await database.execute(f"ALTER TABLE {table} DROP COLUMN favorite")

    if prev_version <= 61:
        # playlist rows can still carry the collages the metadata controller drew before the
        # Playlist Metadata provider took over playlist artwork. Merged images are appended,
        # so such a collage keeps winning over the newer artwork: drop the collages and their
        # files. The builtin system playlists keep changing content, so they only show their
        # static artwork, never generated art.
        system_playlist_ids = (
            "all_favorite_tracks",
            "random_artist",
            "random_album",
            "random_tracks",
            "recently_played",
            "recently_added_tracks",
            "infinite_mix",
            "infinite_mix_favorites",
        )
        static_images = [
            {"type": image_type, "path": path, "provider": "builtin", "remotely_accessible": False}
            for image_type, path in (("thumb", "logo.png"), ("fanart", "fanart.jpg"))
        ]
        system_playlist_item_ids: set[int] = set()
        if await database.get_rows_from_query(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = :table_name",
            {"table_name": DB_TABLE_PROVIDER_MAPPINGS},
            limit=1,
        ):
            system_playlist_item_ids = {
                row["item_id"]
                for row in await database.get_rows_from_query(
                    f"SELECT item_id FROM {DB_TABLE_PROVIDER_MAPPINGS} "
                    "WHERE media_type = 'playlist' AND provider_domain = 'builtin' "
                    "AND provider_item_id IN :system_playlist_ids",
                    {"system_playlist_ids": system_playlist_ids},
                    limit=0,
                )
            }
        playlist_columns = {
            x["name"]
            for x in await database.get_rows_from_query(
                f"PRAGMA table_info({DB_TABLE_PLAYLISTS})", limit=0
            )
        }
        migrated_playlist_rows = 0
        # guard against (test) databases with stand-in tables
        if "metadata" in playlist_columns:
            async for db_row in database.iter_items(DB_TABLE_PLAYLISTS):
                is_system_playlist = db_row["item_id"] in system_playlist_item_ids
                if not isinstance(raw_metadata := db_row["metadata"], str) or not (
                    is_system_playlist or "/collage/" in raw_metadata
                ):
                    continue
                try:
                    metadata = json_loads(raw_metadata)
                except ValueError:
                    continue
                images = metadata.get("images") if isinstance(metadata, dict) else None
                if not isinstance(images, list):
                    continue
                kept_images = []
                lost_collage_thumb = False
                for image in images:
                    if not isinstance(image, dict):
                        kept_images.append(image)
                        continue
                    path = image.get("path")
                    if (
                        image.get("provider") == "builtin"
                        and isinstance(path, str)
                        and path.startswith("/collage/")
                    ):
                        lost_collage_thumb |= image.get("type") == "thumb"
                        continue
                    if is_system_playlist and image.get("provider") == "playlist_metadata":
                        continue
                    kept_images.append(image)
                if is_system_playlist:
                    kept_types = {x.get("type") for x in kept_images if isinstance(x, dict)}
                    kept_images += [x for x in static_images if x["type"] not in kept_types]
                if kept_images == images:
                    continue
                metadata["images"] = kept_images
                if lost_collage_thumb:
                    # a playlist without a refresh timestamp gets a new cover on the next scan
                    metadata.pop("last_refresh", None)
                await database.update(
                    DB_TABLE_PLAYLISTS,
                    {"item_id": db_row["item_id"]},
                    {"metadata": serialize_to_json(metadata)},
                )
                migrated_playlist_rows += 1
        if migrated_playlist_rows:
            logger.info("Removed outdated artwork from %d playlist(s)", migrated_playlist_rows)
        playlog_columns = {
            x["name"]
            for x in await database.get_rows_from_query(
                f"PRAGMA table_info({DB_TABLE_PLAYLOG})", limit=0
            )
        }
        if {"image", "media_type"} <= playlog_columns:
            # the playlog keeps the image a playlist had when it was played, so a collage
            # would show as a broken image in the recently played listing
            await database.execute(
                f"UPDATE {DB_TABLE_PLAYLOG} SET image = NULL "
                "WHERE media_type = 'playlist' AND image LIKE '%\"/collage/%' "
                "AND image LIKE '%\"builtin\"%'"
            )
        await asyncio.to_thread(
            shutil.rmtree, os.path.join(mass.cache_path, "collage_images"), ignore_errors=True
        )

    if prev_version <= 62:
        # audio analysis moved out of library.db into a database file of its own
        await _move_audio_analysis_out(mass, database, logger)

    # NOTE: this genre restore runs after the <= 50 step on purpose: it inserts genres
    # with the current code/schema, so the external_ids column must be gone first.
    if prev_version <= 47:
        # seed the curated podcast & audiobook default genres into their namespaces so existing
        # installs get them on upgrade (music defaults already exist and are skipped), and
        # refresh 46/47-seeded genres so they pick up their (later added) icon metadata.
        # A partial restore is idempotent; failures here are non-fatal — defaults can be
        # restored later via the admin API rather than discarding the whole library.
        await database.commit()
        try:
            await mass.music.genres.restore_default_genres(full_restore=False)
        except Exception as err:
            logger.warning("Could not seed default podcast/audiobook genres: %s", err)

    # (re)build the FTS search tables so they are in sync with the content tables;
    # this both populates them on first migration to the FTS-enabled schema and
    # repairs them after any migration that rewrote rows without the sync triggers active
    for table in MEDIA_ITEM_DB_TABLES:
        table_columns = {
            x["name"]
            for x in await database.get_rows_from_query(f"PRAGMA table_info({table})", limit=0)
        }
        if "search_name" not in table_columns:
            # guard against (test) databases with stand-in tables
            continue
        await database.execute(
            f"""CREATE VIRTUAL TABLE IF NOT EXISTS {table}_fts USING fts5(
                search_name,
                content='{table}',
                content_rowid='item_id',
                tokenize='trigram'
                );"""
        )
        await database.execute(f"INSERT INTO {table}_fts({table}_fts) VALUES('rebuild')")

    # save changes
    await database.commit()

    # always clear the cache after a db migration
    await mass.cache.clear()


async def _move_audio_analysis_out(
    mass: MusicAssistant, database: DatabaseConnection, logger: logging.Logger
) -> None:
    """
    Move the audio analysis tables out of library.db into audio_analysis.db.

    Never raises: a table that cannot be moved completely stays in library.db.

    :param mass: The MusicAssistant instance, for the storage path.
    :param database: The music library connection.
    :param logger: Logger to report progress on.
    """
    tables = [
        (table, columns)
        for table, columns in (
            (DB_TABLE_AUDIO_ANALYSIS, _ANALYSIS_COLUMNS),
            (DB_TABLE_AUDIO_ANALYSIS_FAILURES, _FAILURE_COLUMNS),
        )
        if await database.get_rows_from_query(
            "SELECT 1 FROM main.sqlite_master WHERE type = 'table' AND name = :name",
            {"name": table},
            limit=1,
        )
    ]
    if not tables:
        return
    db_path = os.path.join(mass.storage_path, AA_DB_FILENAME)
    try:
        # ATTACH cannot run inside a transaction
        await database.commit()
        await database.execute(
            f"ATTACH DATABASE :path AS {_AUDIO_ANALYSIS_SCHEMA}", {"path": db_path}
        )
    # a failed library migration resets library.db, so nothing here may raise
    except Exception as err:
        logger.error(
            "Could not open %s (%s); audio analysis stays in library.db", AA_DB_FILENAME, err
        )
        return
    try:
        # a newer analysis schema raises here, before anything is written
        await create_analysis_tables(database, _AUDIO_ANALYSIS_SCHEMA)
        for table, columns in tables:
            await _move_audio_analysis_table(database, logger, table, columns)
    except Exception as err:
        logger.error(
            "Could not move audio analysis to %s (%s); the remaining rows stay in library.db",
            AA_DB_FILENAME,
            err,
        )
    finally:
        with suppress(sqlite3.Error):
            await database.commit()
            await database.execute(f"DETACH DATABASE {_AUDIO_ANALYSIS_SCHEMA}")


async def _move_audio_analysis_table(
    database: DatabaseConnection,
    logger: logging.Logger,
    table: str,
    columns: tuple[str, ...],
) -> None:
    """
    Copy one audio analysis table into the attached analysis database in batches, then drop it.

    Conflicts on the natural key keep the newer row, so rows a downgraded build wrote win
    over older copies.

    :param database: The music library connection with the analysis database attached.
    :param logger: Logger to report progress on.
    :param table: Name of the table, the same in library.db and the analysis database.
    :param columns: Column names (excluding id) the two tables share.
    """
    schema = _AUDIO_ANALYSIS_SCHEMA
    total = await database.get_count_from_query(f"SELECT id FROM main.{table}")
    logger.info("Moving %s rows of %s to %s", total, table, AA_DB_FILENAME)
    cols = ", ".join(columns)
    updates = ", ".join(f"{column} = excluded.{column}" for column in columns)
    copied = 0
    last_id = 0
    while True:
        # page by existing ids: insert_or_replace churn leaves the legacy ids sparse
        row = await database.get_rows_from_query(
            f"SELECT MAX(id) AS upper FROM (SELECT id FROM main.{table} "
            "WHERE id > :last_id ORDER BY id LIMIT :batch_size)",
            {"last_id": last_id, "batch_size": AUDIO_ANALYSIS_MOVE_BATCH_SIZE},
            limit=0,
        )
        if row[0]["upper"] is None:
            break
        upper = int(row[0]["upper"])
        cursor = await database.execute(
            f"INSERT INTO {schema}.{table} ({cols}) "
            f"SELECT {cols} FROM main.{table} "
            f"WHERE id > :last_id AND id <= :upper ORDER BY id "
            "ON CONFLICT(item_id, provider, aa_provider_domain, media_type) "
            f"DO UPDATE SET {updates} "
            f"WHERE excluded.timestamp_created > {table}.timestamp_created",
            {"last_id": last_id, "upper": upper},
        )
        await database.commit()
        copied += cursor.rowcount
        last_id = upper
        logger.debug("Moved %s/%s rows of %s", min(copied, total), total, table)
    # verify by natural key, not row count: the analysis file can already hold rows
    # of its own, so its count alone can't prove every row landed
    missing = await database.get_count_from_query(
        f"SELECT m.id FROM main.{table} m WHERE NOT EXISTS ("
        f"SELECT 1 FROM {schema}.{table} a "
        f"WHERE a.item_id = m.item_id AND a.provider = m.provider "
        f"AND a.aa_provider_domain = m.aa_provider_domain AND a.media_type = m.media_type)"
    )
    if missing:
        raise ProviderUnavailableError(
            f"Moving {table} incomplete ({missing} of {total} rows still in library.db)"
        )
    await database.execute(f"DROP TABLE main.{table}")
    await database.commit()
    logger.info("Moved %s of %s rows of %s to %s", copied, total, table, AA_DB_FILENAME)
