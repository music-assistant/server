"""
Database setup logic for the AudioAnalysisController.

Opens ``audio_analysis.db`` on its own connection, creates its tables, checks its schema
version and moves an unusable file aside. Rows that still live in library.db from before the
split are moved over by the music library migrations, which create the tables through
``create_analysis_tables``.

This module provides the AudioAnalysisDatabaseMixin class which is inherited by
AudioAnalysisController, keeping the database lifecycle separate from the analysis logic.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sqlite3
from typing import TYPE_CHECKING

from music_assistant_models.errors import ProviderUnavailableError

from music_assistant.constants import (
    DB_TABLE_AUDIO_ANALYSIS,
    DB_TABLE_AUDIO_ANALYSIS_FAILURES,
    DB_TABLE_SETTINGS,
)
from music_assistant.controllers.streams.constants import (
    AA_DB_FILENAME,
    AA_DB_SCHEMA_VERSION,
    AA_TABLE_SETTINGS,
)
from music_assistant.helpers.database import DatabaseConnection

if TYPE_CHECKING:
    import logging

    from music_assistant import MusicAssistant


async def create_analysis_tables(database: DatabaseConnection, schema: str = "main") -> int:
    """
    Create the audio analysis tables if missing and return the stored schema version.

    :param database: Connection the analysis database is reachable on.
    :param schema: Schema name of the analysis database on that connection.
    :returns: The stored schema version, 0 for a new file.
    :raises ProviderUnavailableError: When the file holds an invalid or newer schema version.
    """
    # creating the settings table doubles as the probe that the file is readable at all
    await database.execute(
        f"""CREATE TABLE IF NOT EXISTS {schema}.{DB_TABLE_SETTINGS}(
                [key] TEXT PRIMARY KEY,
                [value] TEXT,
                [type] TEXT
            );"""
    )
    # checked before any table is created, so a newer file is never modified
    version_row = await database.get_row(f"{schema}.{DB_TABLE_SETTINGS}", {"key": "version"})
    prev_version = 0
    if version_row is not None:
        if version_row["value"] is None:
            raise ProviderUnavailableError(f"{AA_DB_FILENAME} has an invalid schema version")
        prev_version = int(version_row["value"])
        if prev_version > AA_DB_SCHEMA_VERSION:
            raise ProviderUnavailableError(
                f"{AA_DB_FILENAME} schema version {prev_version} is newer than "
                f"this build supports ({AA_DB_SCHEMA_VERSION}); upgrade Music Assistant"
            )
    await database.execute(
        f"""CREATE TABLE IF NOT EXISTS {schema}.{DB_TABLE_AUDIO_ANALYSIS}(
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
    await database.execute(
        f"""CREATE TABLE IF NOT EXISTS {schema}.{DB_TABLE_AUDIO_ANALYSIS_FAILURES}(
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
    await database.commit()
    return prev_version


class AudioAnalysisDatabaseMixin:
    """
    Mixin class providing the audio analysis database lifecycle for the AudioAnalysisController.

    This mixin expects to be mixed with a class that provides:
    - mass: MusicAssistant instance
    - logger: logging.Logger instance
    - _database: the analysis database connection, None until opened
    - _database_ready: whether the analysis database is open and its schema is current
    """

    # Type hints for attributes provided by the class this mixin is used with
    if TYPE_CHECKING:
        mass: MusicAssistant
        logger: logging.Logger
        _database: DatabaseConnection | None
        _database_ready: bool

    @property
    def database_ready(self) -> bool:
        """Return whether the analysis database is open and its schema is current."""
        return self._database_ready

    @property
    def database(self) -> DatabaseConnection:
        """Return the analysis database connection."""
        self._require_database()
        assert self._database is not None
        return self._database

    async def setup_database(self) -> None:
        """
        Open the audio analysis database and make sure its tables exist.

        Must run before any analysis query. An unusable file is moved aside and replaced
        by an empty one; any other failure leaves analysis unavailable until a restart.
        """
        db_path = os.path.join(self.mass.storage_path, AA_DB_FILENAME)
        self._database_ready = False
        try:
            try:
                prev_version = await self._open_database(db_path)
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
                prev_version = await self._open_database(db_path)
            if prev_version != AA_DB_SCHEMA_VERSION:
                assert self._database is not None
                await self._database.insert_or_replace(
                    AA_TABLE_SETTINGS,
                    {"key": "version", "value": str(AA_DB_SCHEMA_VERSION), "type": "str"},
                )
                await self._database.commit()
        except (sqlite3.Error, OSError, ValueError, ProviderUnavailableError) as err:
            self.logger.error(
                "Audio analysis unavailable: %s (%s). Playback remains available; "
                "check storage and database compatibility, then restart to retry",
                db_path,
                err,
            )
            return
        self._database_ready = True

    async def close_database(self) -> None:
        """Close the analysis database connection."""
        self._database_ready = False
        await self._close_connection()

    def _require_database(self) -> None:
        """Reject analysis operations until the database is open and its schema is current."""
        if not self._database_ready:
            raise ProviderUnavailableError(
                "Audio analysis database is unavailable; check the server log and restart to retry"
            )

    async def _open_database(self, db_path: str) -> int:
        """
        Open the analysis database (if not open yet) and create its tables.

        :param db_path: Path of the analysis database file.
        :returns: The stored schema version, 0 for a new file.
        """
        if self._database is None:
            database = DatabaseConnection(db_path)
            try:
                await database.setup()
            except BaseException:
                with contextlib.suppress(Exception):
                    await database.close()
                raise
            self._database = database
        try:
            return await create_analysis_tables(self._database)
        except BaseException:
            await self._close_connection()
            raise

    async def _close_connection(self) -> None:
        """Close the analysis connection if open, tolerating a connection that is unusable."""
        if self._database is None:
            return
        database, self._database = self._database, None
        with contextlib.suppress(sqlite3.Error):
            await database.close()

    async def _quarantine_database(self, db_path: str) -> None:
        """
        Close an unusable analysis database and move it (and its sidecars) out of the way.

        :param db_path: Path of the analysis database file to move aside.
        """
        await self._close_connection()
        for suffix in ("", "-wal", "-shm"):
            source = f"{db_path}{suffix}"
            if await asyncio.to_thread(os.path.exists, source):
                # overwrites an older quarantine; we only ever keep the most recent one
                await asyncio.to_thread(os.replace, source, f"{source}.corrupt")
