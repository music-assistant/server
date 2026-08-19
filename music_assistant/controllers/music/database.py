"""
Database setup logic for the MusicController.

Handles initialization of the library database, schema creation
(tables/indexes/triggers) and periodic maintenance. The (large) version-by-version
migration logic lives in the sibling ``migrations`` module.

This module provides the MusicDatabaseSetupMixin class which is inherited by
MusicController to add database setup capabilities, keeping this code separated
from the main controller logic.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sqlite3
from typing import TYPE_CHECKING, Final

from music_assistant_models.errors import MusicAssistantError

from music_assistant.constants import (
    DB_TABLE_ALBUM_ARTISTS,
    DB_TABLE_ALBUM_TRACKS,
    DB_TABLE_ALBUMS,
    DB_TABLE_ARTISTS,
    DB_TABLE_AUDIO_ANALYSIS,
    DB_TABLE_AUDIO_ANALYSIS_FAILURES,
    DB_TABLE_AUDIOBOOK_ARTISTS,
    DB_TABLE_AUDIOBOOKS,
    DB_TABLE_EXTERNAL_ID_LOOKUP,
    DB_TABLE_GENRE_MEDIA_ITEM_EXCLUSION,
    DB_TABLE_GENRE_MEDIA_ITEM_MAPPING,
    DB_TABLE_GENRES,
    DB_TABLE_PLAYLISTS,
    DB_TABLE_PLAYLOG,
    DB_TABLE_PODCASTS,
    DB_TABLE_PROVIDER_MAPPINGS,
    DB_TABLE_RADIOS,
    DB_TABLE_SETTINGS,
    DB_TABLE_TRACK_ARTISTS,
    DB_TABLE_TRACKS,
    MEDIA_ITEM_DB_TABLES,
    VACUUM_MIN_RECLAIM_RATIO,
)
from music_assistant.controllers.music.constants import DB_SCHEMA_VERSION
from music_assistant.controllers.music.media.genres import GenreController
from music_assistant.controllers.music.migrations import migrate_database
from music_assistant.controllers.tasks.context import update_current_task_progress_text
from music_assistant.helpers.database import DatabaseConnection

if TYPE_CHECKING:
    import logging

    from music_assistant_models.background_task import BackgroundTask
    from music_assistant_models.enums import MediaType

    from music_assistant import MusicAssistant
    from music_assistant.controllers.music.media.albums import AlbumsController
    from music_assistant.controllers.music.media.artists import ArtistsController
    from music_assistant.controllers.music.media.audiobooks import AudiobooksController
    from music_assistant.controllers.music.media.playlists import PlaylistController
    from music_assistant.controllers.music.media.podcasts import PodcastsController
    from music_assistant.controllers.music.media.radio import RadioController
    from music_assistant.controllers.music.media.tracks import TracksController

# the playlog's unique constraint: one row per item, per media type, per user
PLAYLOG_CONFLICT_KEYS: Final[tuple[str, ...]] = ("item_id", "provider", "media_type", "userid")


class MusicDatabaseSetupMixin:
    """
    Mixin class providing database setup and migration for the MusicController.

    Handles initialization of the library database connection, creation of the
    schema (tables, indexes and triggers), migration between schema versions and
    periodic cleanup/maintenance.

    This mixin expects to be mixed with a class that provides:
    - mass: MusicAssistant instance
    - logger: logging.Logger instance
    - database: the active DatabaseConnection
    - the per-media-type controllers (albums, artists, tracks, playlists, radio,
      podcasts, audiobooks, genres)
    - close() and start_sync() methods
    """

    # Type hints for attributes/methods provided by the class this mixin is used with
    if TYPE_CHECKING:
        mass: MusicAssistant
        logger: logging.Logger
        _database: DatabaseConnection | None
        albums: AlbumsController
        artists: ArtistsController
        tracks: TracksController
        playlists: PlaylistController
        radio: RadioController
        podcasts: PodcastsController
        audiobooks: AudiobooksController
        genres: GenreController

        @property
        def database(self) -> DatabaseConnection: ...  # noqa: D102

        async def close(self) -> None: ...  # noqa: D102

        async def start_sync(  # noqa: D102
            self,
            media_types: list[MediaType] | None = None,
            providers: list[str] | None = None,
        ) -> list[BackgroundTask]: ...

    async def _cleanup_database(self) -> None:
        """Perform database cleanup/maintenance."""
        self.logger.debug("Performing database cleanup...")
        update_current_task_progress_text("Cleaning old playlog entries")
        # Remove playlog entries older than 90 days
        await self.database.delete_where_query(
            DB_TABLE_PLAYLOG, f"timestamp < strftime('%s','now') - {3600 * 24 * 90}"
        )
        # db tables cleanup
        for ctrl in (
            self.albums,
            self.artists,
            self.tracks,
            self.playlists,
            self.radio,
            self.podcasts,
            self.audiobooks,
        ):
            update_current_task_progress_text(f"Cleaning {ctrl.media_type.value} library records")
            # Provider mappings where the db item is removed
            query = (
                f"item_id not in (SELECT item_id from {ctrl.db_table}) "
                f"AND media_type = '{ctrl.media_type}'"
            )
            await self.database.delete_where_query(DB_TABLE_PROVIDER_MAPPINGS, query)
            # Orphaned db items
            query = (
                f"item_id not in (SELECT item_id from {DB_TABLE_PROVIDER_MAPPINGS} "
                f"WHERE media_type = '{ctrl.media_type}')"
            )
            await self.database.delete_where_query(ctrl.db_table, query)
            # External id lookup rows where the db item is removed
            query = (
                f"item_id not in (SELECT item_id from {ctrl.db_table}) "
                f"AND media_type = '{ctrl.media_type}'"
            )
            await self.database.delete_where_query(DB_TABLE_EXTERNAL_ID_LOOKUP, query)
            # Cleanup removed db items from the playlog
            where_clause = (
                f"media_type = '{ctrl.media_type}' AND provider = 'library' "
                f"AND item_id not in (select item_id from {ctrl.db_table})"
            )
            await self.mass.music.database.delete_where_query(DB_TABLE_PLAYLOG, where_clause)
        update_current_task_progress_text("Database cleanup finished")
        self.logger.debug("Database cleanup done")

    async def _setup_database(self) -> None:
        """Initialize database."""
        db_path = os.path.join(self.mass.storage_path, "library.db")
        self._database = DatabaseConnection(db_path)
        await self._database.setup()

        # always create db tables if they don't exist to prevent errors trying to access them later
        await self.__create_database_tables()
        try:
            if db_row := await self._database.get_row(DB_TABLE_SETTINGS, {"key": "version"}):
                prev_version = int(db_row["value"])
            else:
                prev_version = 0
        except KeyError, ValueError:
            prev_version = 0

        if prev_version not in (0, DB_SCHEMA_VERSION):
            # db version mismatch - we need to do a migration
            # make a backup of db file
            db_path_backup = db_path + ".backup"
            await asyncio.to_thread(shutil.copyfile, db_path, db_path_backup)

            # handle db migration from previous schema(s) to this one
            try:
                await migrate_database(
                    self.mass,
                    self.database,
                    self.logger,
                    prev_version,
                    self.__create_database_tables,
                )
            except Exception as err:
                # if the migration fails completely we reset the db
                # so the user at least can have a working situation back
                # a backup file is made with the previous version
                self.logger.error(
                    "Database migration failed - starting with a fresh library database, "
                    "a full rescan will be performed, this can take a while!",
                )
                if not isinstance(err, MusicAssistantError):
                    self.logger.exception("Unexpected error during database migration")

                await self._database.close()
                await asyncio.to_thread(os.remove, db_path)
                self._database = DatabaseConnection(db_path)
                await self._database.setup()
                await self.mass.cache.clear()
                await self.__create_database_tables()
                prev_version = 0

        # store current schema version
        await self._database.insert_or_replace(
            DB_TABLE_SETTINGS,
            {"key": "version", "value": str(DB_SCHEMA_VERSION), "type": "str"},
        )
        # create indexes and triggers if needed
        await self.__create_database_indexes()
        await self.__create_database_triggers()
        if prev_version == 0:
            # fresh install - populate default genres
            await self.genres.restore_default_genres()
        # compact db - skip the full rebuild unless a meaningful share is reclaimable
        try:
            reclaimable_ratio = await self._database.get_reclaimable_ratio()
            if reclaimable_ratio < VACUUM_MIN_RECLAIM_RATIO:
                self.logger.debug(
                    "Skipping database compaction (only %.1f%% reclaimable)",
                    reclaimable_ratio * 100,
                )
            else:
                self.logger.debug(
                    "Compacting database (%.1f%% reclaimable)...", reclaimable_ratio * 100
                )
                await self._database.vacuum()
                self.logger.debug("Compacting database done")
        except Exception as err:
            self.logger.warning("Database vacuum failed: %s", str(err))

    async def _reset_database(self) -> None:
        """Reset the database."""
        await self.close()
        db_path = os.path.join(self.mass.storage_path, "library.db")
        await asyncio.to_thread(os.remove, db_path)
        await self._setup_database()
        # initiate full sync
        await self.start_sync()

    async def __create_database_tables(self) -> None:
        """Create database tables."""
        await self.database.execute(
            f"""CREATE TABLE IF NOT EXISTS {DB_TABLE_SETTINGS}(
                    [key] TEXT PRIMARY KEY,
                    [value] TEXT,
                    [type] TEXT
                );"""
        )
        await self.database.execute(
            f"""CREATE TABLE IF NOT EXISTS {DB_TABLE_PLAYLOG}(
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
        await self.database.execute(
            f"""CREATE TABLE IF NOT EXISTS {DB_TABLE_ALBUMS}(
                    [item_id] INTEGER PRIMARY KEY AUTOINCREMENT,
                    [name] TEXT NOT NULL,
                    [sort_name] TEXT NOT NULL,
                    [version] TEXT,
                    [album_type] TEXT NOT NULL,
                    [year] INTEGER,
                    [favorite] BOOLEAN NOT NULL DEFAULT 0,
                    [metadata] json NOT NULL,
                    [play_count] INTEGER NOT NULL DEFAULT 0,
                    [last_played] INTEGER NOT NULL DEFAULT 0,
                    [timestamp_added] INTEGER DEFAULT (cast(strftime('%s','now') as int)),
                    [timestamp_modified] INTEGER NOT NULL DEFAULT 0,
                    [search_name] TEXT NOT NULL,
                    [search_sort_name] TEXT NOT NULL
                );"""
        )
        await self.database.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {DB_TABLE_ARTISTS}(
            [item_id] INTEGER PRIMARY KEY AUTOINCREMENT,
            [name] TEXT NOT NULL,
            [sort_name] TEXT NOT NULL,
            [favorite] BOOLEAN NOT NULL DEFAULT 0,
            [metadata] json NOT NULL,
            [play_count] INTEGER DEFAULT 0,
            [last_played] INTEGER DEFAULT 0,
            [timestamp_added] INTEGER DEFAULT (cast(strftime('%s','now') as int)),
            [timestamp_modified] INTEGER NOT NULL DEFAULT 0,
            [search_name] TEXT NOT NULL,
            [search_sort_name] TEXT NOT NULL,
            [artist_type] TEXT NOT NULL
            );"""
        )
        await self.database.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {DB_TABLE_TRACKS}(
            [item_id] INTEGER PRIMARY KEY AUTOINCREMENT,
            [name] TEXT NOT NULL,
            [sort_name] TEXT NOT NULL,
            [version] TEXT,
            [duration] INTEGER,
            [favorite] BOOLEAN NOT NULL DEFAULT 0,
            [metadata] json NOT NULL,
            [play_count] INTEGER DEFAULT 0,
            [last_played] INTEGER DEFAULT 0,
            [timestamp_added] INTEGER DEFAULT (cast(strftime('%s','now') as int)),
            [timestamp_modified] INTEGER NOT NULL DEFAULT 0,
            [search_name] TEXT NOT NULL,
            [search_sort_name] TEXT NOT NULL
            );"""
        )
        await self.database.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {DB_TABLE_PLAYLISTS}(
            [item_id] INTEGER PRIMARY KEY AUTOINCREMENT,
            [name] TEXT NOT NULL,
            [sort_name] TEXT NOT NULL,
            [translation_key] TEXT,
            [translation_params] json,
            [owner] TEXT NOT NULL,
            [is_editable] BOOLEAN NOT NULL,
            [favorite] BOOLEAN NOT NULL DEFAULT 0,
            [metadata] json NOT NULL,
            [play_count] INTEGER DEFAULT 0,
            [last_played] INTEGER DEFAULT 0,
            [timestamp_added] INTEGER DEFAULT (cast(strftime('%s','now') as int)),
            [timestamp_modified] INTEGER NOT NULL DEFAULT 0,
            [search_name] TEXT NOT NULL,
            [search_sort_name] TEXT NOT NULL,
            [supported_mediatypes] json NOT NULL DEFAULT '[\"track\"]',
            [is_dynamic] BOOLEAN NOT NULL DEFAULT 0
            );"""
        )
        await self.database.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {DB_TABLE_RADIOS}(
            [item_id] INTEGER PRIMARY KEY AUTOINCREMENT,
            [name] TEXT NOT NULL,
            [sort_name] TEXT NOT NULL,
            [favorite] BOOLEAN NOT NULL DEFAULT 0,
            [metadata] json NOT NULL,
            [play_count] INTEGER DEFAULT 0,
            [last_played] INTEGER DEFAULT 0,
            [timestamp_added] INTEGER DEFAULT (cast(strftime('%s','now') as int)),
            [timestamp_modified] INTEGER NOT NULL DEFAULT 0,
            [search_name] TEXT NOT NULL,
            [search_sort_name] TEXT NOT NULL,
            [is_dynamic] BOOLEAN NOT NULL DEFAULT 0
            );"""
        )
        await self.database.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {DB_TABLE_AUDIOBOOKS}(
            [item_id] INTEGER PRIMARY KEY AUTOINCREMENT,
            [name] TEXT NOT NULL,
            [sort_name] TEXT NOT NULL,
            [version] TEXT,
            [favorite] BOOLEAN NOT NULL DEFAULT 0,
            [publisher] TEXT,
            [authors] json NOT NULL,
            [narrators] json NOT NULL,
            [metadata] json NOT NULL,
            [duration] INTEGER,
            [play_count] INTEGER DEFAULT 0,
            [last_played] INTEGER DEFAULT 0,
            [timestamp_added] INTEGER DEFAULT (cast(strftime('%s','now') as int)),
            [timestamp_modified] INTEGER NOT NULL DEFAULT 0,
            [search_name] TEXT NOT NULL,
            [search_sort_name] TEXT NOT NULL
            );"""
        )
        await self.database.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {DB_TABLE_PODCASTS}(
            [item_id] INTEGER PRIMARY KEY AUTOINCREMENT,
            [name] TEXT NOT NULL,
            [sort_name] TEXT NOT NULL,
            [version] TEXT,
            [favorite] BOOLEAN NOT NULL DEFAULT 0,
            [publisher] TEXT,
            [total_episodes] INTEGER NOT NULL,
            [metadata] json NOT NULL,
            [play_count] INTEGER NOT NULL DEFAULT 0,
            [last_played] INTEGER NOT NULL DEFAULT 0,
            [timestamp_added] INTEGER DEFAULT (cast(strftime('%s','now') as int)),
            [timestamp_modified] INTEGER NOT NULL DEFAULT 0,
            [search_name] TEXT NOT NULL,
            [search_sort_name] TEXT NOT NULL
            );"""
        )
        await self.database.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {DB_TABLE_GENRES}(
            [item_id] INTEGER PRIMARY KEY AUTOINCREMENT,
            [name] TEXT NOT NULL,
            [sort_name] TEXT NOT NULL,
            [translation_key] TEXT,
            [description] TEXT,
            [favorite] BOOLEAN NOT NULL DEFAULT 0,
            [metadata] json NOT NULL,
            [genre_aliases] json NOT NULL DEFAULT '[]',
            [play_count] INTEGER NOT NULL DEFAULT 0,
            [last_played] INTEGER NOT NULL DEFAULT 0,
            [timestamp_added] INTEGER DEFAULT (cast(strftime('%s','now') as int)),
            [timestamp_modified] INTEGER NOT NULL DEFAULT 0,
            [search_name] TEXT NOT NULL,
            [search_sort_name] TEXT NOT NULL,
            [is_excluded] BOOLEAN NOT NULL DEFAULT 0,
            [is_default] BOOLEAN NOT NULL DEFAULT 0,
            [content_type] TEXT
            );"""
        )
        await self.database.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING}(
            [genre_id] INTEGER NOT NULL,
            [media_id] INTEGER NOT NULL,
            [media_type] TEXT NOT NULL,
            [alias] TEXT,
            [is_derived] BOOLEAN NOT NULL DEFAULT 0,
            [is_manual] BOOLEAN NOT NULL DEFAULT 0,
            FOREIGN KEY([genre_id]) REFERENCES [genres]([item_id]),
            UNIQUE(genre_id, media_id, media_type)
            );"""
        )
        await self.database.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {DB_TABLE_GENRE_MEDIA_ITEM_EXCLUSION}(
            [genre_id] INTEGER NOT NULL,
            [media_id] INTEGER NOT NULL,
            [media_type] TEXT NOT NULL,
            FOREIGN KEY([genre_id]) REFERENCES [genres]([item_id]),
            UNIQUE(genre_id, media_id, media_type)
            );"""
        )
        await self.database.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {DB_TABLE_ALBUM_TRACKS}(
            [id] INTEGER PRIMARY KEY AUTOINCREMENT,
            [track_id] INTEGER NOT NULL,
            [album_id] INTEGER NOT NULL,
            [disc_number] INTEGER NOT NULL,
            [track_number] INTEGER NOT NULL,
            FOREIGN KEY([track_id]) REFERENCES [tracks]([item_id]),
            FOREIGN KEY([album_id]) REFERENCES [albums]([item_id]),
            UNIQUE(track_id, album_id)
            );"""
        )
        await self.database.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {DB_TABLE_PROVIDER_MAPPINGS}(
            [media_type] TEXT NOT NULL,
            [item_id] INTEGER NOT NULL,
            [provider_domain] TEXT NOT NULL,
            [provider_instance] TEXT NOT NULL,
            [provider_item_id] TEXT NOT NULL,
            [available] BOOLEAN NOT NULL DEFAULT 1,
            [in_library] BOOLEAN NOT NULL DEFAULT 0,
            [is_unique] BOOLEAN,
            [url] text,
            [audio_format] json,
            [details] TEXT,
            UNIQUE(media_type, provider_instance, provider_item_id)
            );"""
        )
        await self.database.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {DB_TABLE_EXTERNAL_ID_LOOKUP}(
            [media_type] TEXT NOT NULL,
            [external_id_type] TEXT NOT NULL,
            [external_id] TEXT NOT NULL COLLATE NOCASE,
            [item_id] INTEGER NOT NULL,
            UNIQUE(media_type, external_id, external_id_type, item_id)
            );"""
        )
        await self.database.execute(
            f"""CREATE TABLE IF NOT EXISTS {DB_TABLE_TRACK_ARTISTS}(
            [track_id] INTEGER NOT NULL,
            [artist_id] INTEGER NOT NULL,
            FOREIGN KEY([track_id]) REFERENCES [tracks]([item_id]),
            FOREIGN KEY([artist_id]) REFERENCES [artists]([item_id]),
            UNIQUE(track_id, artist_id)
            );"""
        )
        await self.database.execute(
            f"""CREATE TABLE IF NOT EXISTS {DB_TABLE_ALBUM_ARTISTS}(
            [album_id] INTEGER NOT NULL,
            [artist_id] INTEGER NOT NULL,
            FOREIGN KEY([album_id]) REFERENCES [albums]([item_id]),
            FOREIGN KEY([artist_id]) REFERENCES [artists]([item_id]),
            UNIQUE(album_id, artist_id)
            );"""
        )
        await self.database.execute(
            f"""CREATE TABLE IF NOT EXISTS {DB_TABLE_AUDIOBOOK_ARTISTS}(
            [audiobook_id] INTEGER NOT NULL,
            [artist_id] INTEGER NOT NULL,
            FOREIGN KEY([audiobook_id]) REFERENCES [audiobooks]([item_id]),
            FOREIGN KEY([artist_id]) REFERENCES [artists]([item_id]),
            UNIQUE(audiobook_id, artist_id)
            );"""
        )

        await self.database.execute(
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

        await self.database.execute(
            f"""CREATE TABLE IF NOT EXISTS {DB_TABLE_AUDIO_ANALYSIS_FAILURES}(
                    [id] INTEGER PRIMARY KEY AUTOINCREMENT,
                    [media_type] TEXT NOT NULL,
                    [item_id] TEXT NOT NULL,
                    [provider] TEXT NOT NULL,
                    [aa_provider_domain] TEXT NOT NULL,
                    [reason] TEXT NOT NULL,
                    [analysis_version] INTEGER NOT NULL DEFAULT 1,
                    [next_retry] INTEGER,
                    [timestamp_created] INTEGER DEFAULT (cast(strftime('%s','now') as int)),
                    UNIQUE(item_id,provider,aa_provider_domain,media_type));"""
        )

        # full-text search tables (trigram tokenizer for substring matching on search_name)
        for db_table in MEDIA_ITEM_DB_TABLES:
            try:
                await self.database.execute(
                    f"""CREATE VIRTUAL TABLE IF NOT EXISTS {db_table}_fts USING fts5(
                        search_name,
                        content='{db_table}',
                        content_rowid='item_id',
                        tokenize='trigram'
                        );"""
                )
            except sqlite3.OperationalError as err:
                msg = (
                    "The library database requires SQLite 3.34+ with FTS5 support "
                    f"(detected version: {sqlite3.sqlite_version})"
                )
                raise MusicAssistantError(msg) from err

        await self.database.commit()

    async def __create_database_indexes(self) -> None:
        """Create database indexes."""
        for db_table in (
            DB_TABLE_ARTISTS,
            DB_TABLE_ALBUMS,
            DB_TABLE_TRACKS,
            DB_TABLE_PLAYLISTS,
            DB_TABLE_RADIOS,
            DB_TABLE_AUDIOBOOKS,
            DB_TABLE_PODCASTS,
            DB_TABLE_GENRES,
        ):
            # index on favorite column
            await self.database.execute(
                f"CREATE INDEX IF NOT EXISTS {db_table}_favorite_idx on {db_table}(favorite);"
            )
            # index on name
            await self.database.execute(
                f"CREATE INDEX IF NOT EXISTS {db_table}_name_idx on {db_table}(name);"
            )
            # index on search_name (=lowercase name without diacritics)
            await self.database.execute(
                f"CREATE INDEX IF NOT EXISTS {db_table}_name_nocase_idx ON {db_table}(search_name);"
            )
            # index on sort_name
            await self.database.execute(
                f"CREATE INDEX IF NOT EXISTS {db_table}_sort_name_idx on {db_table}(sort_name);"
            )
            # index on search_sort_name (=lowercase sort_name without diacritics)
            await self.database.execute(
                f"CREATE INDEX IF NOT EXISTS {db_table}_search_sort_name_idx "
                f"ON {db_table}(search_sort_name);"
            )
            # index on timestamp_added
            await self.database.execute(
                f"CREATE INDEX IF NOT EXISTS {db_table}_timestamp_added_idx "
                f"on {db_table}(timestamp_added);"
            )
            # index on play_count
            await self.database.execute(
                f"CREATE INDEX IF NOT EXISTS {db_table}_play_count_idx on {db_table}(play_count);"
            )
            # index on last_played
            await self.database.execute(
                f"CREATE INDEX IF NOT EXISTS {db_table}_last_played_idx on {db_table}(last_played);"
            )

        # indexes on provider_mappings table
        await self.database.execute(
            f"CREATE INDEX IF NOT EXISTS {DB_TABLE_PROVIDER_MAPPINGS}_media_type_item_id_idx "
            f"on {DB_TABLE_PROVIDER_MAPPINGS}(media_type,item_id);"
        )
        await self.database.execute(
            f"CREATE INDEX IF NOT EXISTS {DB_TABLE_PROVIDER_MAPPINGS}_provider_domain_idx "
            f"on {DB_TABLE_PROVIDER_MAPPINGS}(media_type,provider_domain,provider_item_id);"
        )
        await self.database.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS {DB_TABLE_PROVIDER_MAPPINGS}_provider_instance_idx "
            f"on {DB_TABLE_PROVIDER_MAPPINGS}(media_type,provider_instance,provider_item_id);"
        )
        await self.database.execute(
            "CREATE INDEX IF NOT EXISTS "
            f"{DB_TABLE_PROVIDER_MAPPINGS}_media_type_provider_instance_idx "
            f"on {DB_TABLE_PROVIDER_MAPPINGS}(media_type,provider_instance);"
        )
        await self.database.execute(
            "CREATE INDEX IF NOT EXISTS "
            f"{DB_TABLE_PROVIDER_MAPPINGS}_media_type_provider_domain_idx "
            f"on {DB_TABLE_PROVIDER_MAPPINGS}(media_type,provider_domain);"
        )
        await self.database.execute(
            "CREATE INDEX IF NOT EXISTS "
            f"{DB_TABLE_PROVIDER_MAPPINGS}_media_type_provider_instance_library_idx "
            f"on {DB_TABLE_PROVIDER_MAPPINGS}(media_type,provider_instance,in_library);"
        )

        # index on external_id_lookup table to serve the per-item delete/rewrite path;
        # the typed and untyped external id lookups are served by the table's unique
        # index, which is deliberately ordered (media_type,external_id,...) for that
        await self.database.execute(
            f"CREATE INDEX IF NOT EXISTS {DB_TABLE_EXTERNAL_ID_LOOKUP}_item_id_idx "
            f"on {DB_TABLE_EXTERNAL_ID_LOOKUP}(media_type,item_id);"
        )

        # indexes on track_artists table
        await self.database.execute(
            f"CREATE INDEX IF NOT EXISTS {DB_TABLE_TRACK_ARTISTS}_track_id_idx "
            f"on {DB_TABLE_TRACK_ARTISTS}(track_id);"
        )
        await self.database.execute(
            f"CREATE INDEX IF NOT EXISTS {DB_TABLE_TRACK_ARTISTS}_artist_id_idx "
            f"on {DB_TABLE_TRACK_ARTISTS}(artist_id);"
        )
        # indexes on album_artists table
        await self.database.execute(
            f"CREATE INDEX IF NOT EXISTS {DB_TABLE_ALBUM_ARTISTS}_album_id_idx "
            f"on {DB_TABLE_ALBUM_ARTISTS}(album_id);"
        )
        await self.database.execute(
            f"CREATE INDEX IF NOT EXISTS {DB_TABLE_ALBUM_ARTISTS}_artist_id_idx "
            f"on {DB_TABLE_ALBUM_ARTISTS}(artist_id);"
        )
        # indexes on genre_media_item_mapping table
        await self.database.execute(
            f"CREATE INDEX IF NOT EXISTS {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING}_media_idx "
            f"on {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING}(media_id,media_type);"
        )
        await self.database.execute(
            f"CREATE INDEX IF NOT EXISTS {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING}_genre_alias_idx "
            f"on {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING}(genre_id,alias);"
        )
        # indexes on genre_media_item_exclusion table
        await self.database.execute(
            f"CREATE INDEX IF NOT EXISTS {DB_TABLE_GENRE_MEDIA_ITEM_EXCLUSION}_media_idx "
            f"on {DB_TABLE_GENRE_MEDIA_ITEM_EXCLUSION}(media_id,media_type);"
        )
        await self.database.execute(
            f"CREATE INDEX IF NOT EXISTS {DB_TABLE_GENRE_MEDIA_ITEM_EXCLUSION}_genre_idx "
            f"on {DB_TABLE_GENRE_MEDIA_ITEM_EXCLUSION}(genre_id);"
        )
        # unique index on playlog table
        await self.database.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS {DB_TABLE_PLAYLOG}_unique_idx "
            f"on {DB_TABLE_PLAYLOG}(item_id,provider,media_type,userid);"
        )
        # speed up recency lookups (smart shuffle / dedup) by user and time window
        await self.database.execute(
            f"CREATE INDEX IF NOT EXISTS {DB_TABLE_PLAYLOG}_userid_timestamp_idx "
            f"on {DB_TABLE_PLAYLOG}(userid,timestamp);"
        )
        # serves the podcast episode resume lookup, which no existing index can: they all
        # lead with item_id or userid, neither of which that query filters on. Column order
        # matches its filter, so with a userid it needs no sort for the ORDER BY either
        await self.database.execute(
            f"CREATE INDEX IF NOT EXISTS {DB_TABLE_PLAYLOG}_provider_media_type_idx "
            f"on {DB_TABLE_PLAYLOG}(provider,media_type,userid,timestamp);"
        )
        await self.database.commit()

    async def __create_database_triggers(self) -> None:
        """Create database triggers."""
        # triggers to auto update timestamps
        for db_table in MEDIA_ITEM_DB_TABLES:
            await self.database.execute(
                f"""
                CREATE TRIGGER IF NOT EXISTS update_{db_table}_timestamp
                AFTER UPDATE ON {db_table}
                BEGIN
                    UPDATE {db_table} SET timestamp_modified=cast(strftime('%s','now') as int)
                    WHERE rowid = new.rowid;
                END;
                """
            )
        # triggers to keep the FTS search tables in sync with the content tables
        for db_table in MEDIA_ITEM_DB_TABLES:
            await self.database.execute(
                f"""
                CREATE TRIGGER IF NOT EXISTS {db_table}_fts_insert
                AFTER INSERT ON {db_table}
                BEGIN
                    INSERT INTO {db_table}_fts(rowid, search_name)
                    VALUES (new.item_id, new.search_name);
                END;
                """
            )
            await self.database.execute(
                f"""
                CREATE TRIGGER IF NOT EXISTS {db_table}_fts_delete
                AFTER DELETE ON {db_table}
                BEGIN
                    INSERT INTO {db_table}_fts({db_table}_fts, rowid, search_name)
                    VALUES ('delete', old.item_id, old.search_name);
                END;
                """
            )
            await self.database.execute(
                f"""
                CREATE TRIGGER IF NOT EXISTS {db_table}_fts_update
                AFTER UPDATE OF search_name ON {db_table}
                BEGIN
                    INSERT INTO {db_table}_fts({db_table}_fts, rowid, search_name)
                    VALUES ('delete', old.item_id, old.search_name);
                    INSERT INTO {db_table}_fts(rowid, search_name)
                    VALUES (new.item_id, new.search_name);
                END;
                """
            )
        await self.database.commit()
