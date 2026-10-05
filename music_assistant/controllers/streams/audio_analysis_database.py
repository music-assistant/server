"""
Database setup logic for the AudioAnalysisController.

Attaches ``audio_analysis.db`` onto the music library connection as schema ``aa``, creates
its tables, runs its migrations and moves an unusable file aside. The version-by-version
migration steps live in the sibling ``audio_analysis_migrations`` module.

This module provides the AudioAnalysisDatabaseMixin class which is inherited by
AudioAnalysisController, keeping the database lifecycle separate from the analysis logic.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
from typing import TYPE_CHECKING

from music_assistant_models.errors import ProviderUnavailableError

from music_assistant.controllers.streams.audio_analysis_migrations import (
    has_legacy_tables,
    migrate_analysis_database,
)
from music_assistant.controllers.streams.constants import (
    AA_DB_FILENAME,
    AA_DB_SCHEMA,
    AA_DB_SCHEMA_VERSION,
    AA_TABLE_ANALYSIS,
    AA_TABLE_FAILURES,
    AA_TABLE_SETTINGS,
)

if TYPE_CHECKING:
    import logging

    from music_assistant import MusicAssistant


class AudioAnalysisDatabaseMixin:
    """
    Mixin class providing the audio analysis database lifecycle for the AudioAnalysisController.

    This mixin expects to be mixed with a class that provides:
    - mass: MusicAssistant instance
    - logger: logging.Logger instance
    - _database_ready: whether the analysis database and its migrations are ready
    """

    # Type hints for attributes provided by the class this mixin is used with
    if TYPE_CHECKING:
        mass: MusicAssistant
        logger: logging.Logger
        _database_ready: bool

    @property
    def database_ready(self) -> bool:
        """Return whether the analysis database and its migrations are ready."""
        return self._database_ready

    async def setup_database(self) -> None:
        """
        Attach the audio analysis database and make sure its tables exist.

        Safe to call more than once. Must run after the music database connection exists
        (it is attached onto that connection) and before any analysis query.
        """
        db = self.mass.music.database
        db_path = os.path.join(self.mass.storage_path, AA_DB_FILENAME)
        self._database_ready = False
        try:
            try:
                prev_version = await self._attach_and_create(db_path)
            except sqlite3.DatabaseError as err:
                # Extended result codes retain the primary SQLite code in the low byte.
                if getattr(err, "sqlite_errorcode", 0) & 0xFF not in (
                    sqlite3.SQLITE_CORRUPT,
                    sqlite3.SQLITE_NOTADB,
                ):
                    raise
                self.logger.error(
                    "Audio analysis database %s is unusable (%s); moving it aside and starting over",
                    db_path,
                    err,
                )
                await self._quarantine_database(db_path)
                prev_version = await self._attach_and_create(db_path)
            moved = 0
            # switching back to a stable build recreates the analysis tables in library.db
            # and fills them again, so their presence restarts the ladder from the beginning
            legacy = await has_legacy_tables(db)
            if legacy or 0 < prev_version < AA_DB_SCHEMA_VERSION:
                moved = await migrate_analysis_database(
                    db, self.logger, 0 if legacy else prev_version
                )
            if prev_version != AA_DB_SCHEMA_VERSION:
                await db.insert_or_replace(
                    AA_TABLE_SETTINGS,
                    {"key": "version", "value": str(AA_DB_SCHEMA_VERSION), "type": "str"},
                )
                await db.commit()
        except (sqlite3.Error, OSError, ValueError, ProviderUnavailableError) as err:
            self.logger.error(
                "Audio analysis unavailable: %s (%s). Playback remains available; "
                "check storage and database compatibility, then restart to retry",
                db_path,
                err,
            )
            return
        self._database_ready = True
        if moved > 0:
            self.logger.info("Compacting library.db after moving %s audio analysis rows", moved)
            try:
                await db.vacuum()
            except sqlite3.Error as err:
                self.logger.warning("Compacting library.db failed: %s", err)

    async def before_library_reset(self) -> None:
        """Refuse a library reset while the analysis database is unavailable."""
        # an unfinished relocation leaves analysis rows in library.db that a reset would delete
        if not self._database_ready:
            raise ProviderUnavailableError(
                "Cannot reset the library while audio analysis storage is unavailable; "
                "resolve the storage or migration error first"
            )

    async def after_library_reset(self) -> None:
        """Attach the analysis database onto the new library connection."""
        await self.setup_database()

    def _require_database(self) -> None:
        """Reject analysis operations until the database and its migrations are ready."""
        if not self._database_ready:
            raise ProviderUnavailableError(
                "Audio analysis database is unavailable; check the server log and restart to retry"
            )

    async def _attach_and_create(self, db_path: str) -> int:
        """
        Attach the analysis database (if not attached yet) and create its tables.

        :param db_path: Path of the analysis database file to attach.
        :returns: The stored schema version, 0 for a new file.
        """
        db = self.mass.music.database
        attached = await db.get_rows_from_query("PRAGMA database_list", limit=0)
        if not any(row["name"] == AA_DB_SCHEMA for row in attached):
            # ATTACH cannot run inside a transaction
            await db.commit()
            await db.execute(f"ATTACH DATABASE :path AS {AA_DB_SCHEMA}", {"path": db_path})
            # the music connection holds an exclusive lock; the analysis file stays readable
            # by other processes (diagnostics, exports) and gets WAL like the other db files
            await db.execute(f"PRAGMA {AA_DB_SCHEMA}.locking_mode=NORMAL;")
            await db.execute(f"PRAGMA {AA_DB_SCHEMA}.journal_mode=WAL;")
            await db.execute(f"PRAGMA {AA_DB_SCHEMA}.journal_size_limit = 6144000;")
            await db.execute(f"PRAGMA {AA_DB_SCHEMA}.synchronous=normal;")
        # creating the settings table doubles as the probe that the file is readable at all:
        # ATTACH opens it lazily, so a corrupt file first fails here
        await db.execute(
            f"""CREATE TABLE IF NOT EXISTS {AA_TABLE_SETTINGS}(
                    [key] TEXT PRIMARY KEY,
                    [value] TEXT,
                    [type] TEXT
                );"""
        )
        # checked before any table is created, so a newer file is never modified
        prev_version = await self._get_schema_version()
        await db.execute(
            f"""CREATE TABLE IF NOT EXISTS {AA_TABLE_ANALYSIS}(
                    [id] INTEGER PRIMARY KEY AUTOINCREMENT,
                    [media_type] TEXT NOT NULL,
                    [item_id] TEXT NOT NULL,
                    [provider] TEXT NOT NULL,
                    [aa_provider_domain] TEXT NOT NULL,
                    [analysis_version] INTEGER DEFAULT 1,
                    [timestamp_created] INTEGER DEFAULT (cast(strftime('%s','now') as int)),
                    [header] TEXT NOT NULL,
                    [payload] BLOB NOT NULL,
                    UNIQUE(item_id,provider,aa_provider_domain,media_type));"""
        )
        await db.execute(
            f"""CREATE TABLE IF NOT EXISTS {AA_TABLE_FAILURES}(
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
        await db.commit()
        return prev_version

    async def _get_schema_version(self) -> int:
        """Return the stored analysis schema version (0 for a new file), rejecting newer ones."""
        version_row = await self.mass.music.database.get_row(AA_TABLE_SETTINGS, {"key": "version"})
        if version_row is None:
            return 0
        if version_row["value"] is None:
            raise ProviderUnavailableError(f"{AA_DB_FILENAME} has an invalid schema version")
        version = int(version_row["value"])
        if version > AA_DB_SCHEMA_VERSION:
            raise ProviderUnavailableError(
                f"{AA_DB_FILENAME} schema version {version} is newer than "
                f"this build supports ({AA_DB_SCHEMA_VERSION}); upgrade Music Assistant"
            )
        return version

    async def _quarantine_database(self, db_path: str) -> None:
        """
        Detach an unusable analysis database and move it (and its sidecars) out of the way.

        :param db_path: Path of the analysis database file to move aside.
        """
        db = self.mass.music.database
        attached = await db.get_rows_from_query("PRAGMA database_list", limit=0)
        if any(row["name"] == AA_DB_SCHEMA for row in attached):
            await db.commit()
            await db.execute(f"DETACH DATABASE {AA_DB_SCHEMA}")
        for suffix in ("", "-wal", "-shm"):
            source = f"{db_path}{suffix}"
            if await asyncio.to_thread(os.path.exists, source):
                # overwrites an older quarantine; we only ever keep the most recent one
                await asyncio.to_thread(os.replace, source, f"{source}.corrupt")
