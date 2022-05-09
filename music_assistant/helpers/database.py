"""Database logic."""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, Dict, List, Mapping, Optional

from databases import Database as Db
from databases import DatabaseURL

from music_assistant.helpers.typing import MusicAssistant

# pylint: disable=invalid-name

SCHEMA_VERSION = 6

TABLE_PROV_MAPPINGS = "provider_mappings"
TABLE_TRACK_LOUDNESS = "track_loudness"
TABLE_PLAYLOG = "playlog"
TABLE_ARTISTS = "artists"
TABLE_ALBUMS = "albums"
TABLE_TRACKS = "tracks"
TABLE_PLAYLISTS = "playlists"
TABLE_RADIOS = "radios"
TABLE_CACHE = "cache"
TABLE_SETTINGS = "settings"


class Database:
    """Class that holds the (logic to the) database."""

    def __init__(self, mass: MusicAssistant, url: DatabaseURL):
        """Initialize class."""
        self.url = url
        self.mass = mass
        self.logger = mass.logger.getChild("db")

    async def setup(self) -> None:
        """Perform async initialization."""
        async with self.get_db() as _db:
            await _db.execute(
                """CREATE TABLE IF NOT EXISTS settings(
                        key TEXT PRIMARY KEY,
                        value TEXT
                    );"""
            )
        await self._migrate()

    @asynccontextmanager
    async def get_db(self, db: Optional[Db] = None) -> Db:
        """Context manager helper to get the active db connection."""
        if db is not None:
            yield db
        else:
            async with Db(self.url, timeout=360) as _db:
                yield _db

    async def get_setting(self, key: str, db: Optional[Db] = None) -> str | None:
        """Get setting from settings table."""
        return await self.get_row(TABLE_SETTINGS, {"key": key}, db=db)

    async def set_setting(self, key: str, value: str, db: Optional[Db] = None) -> None:
        """Set setting in settings table."""
        if not isinstance(value, str):
            value = str(value)
        return await self.insert_or_replace(
            TABLE_SETTINGS, {"key": key, "value": value}
        )

    async def get_count(
        self,
        table: str,
        match: dict = None,
        db: Optional[Db] = None,
    ) -> int:
        """Get row count for given table/query."""
        async with self.get_db(db) as _db:
            sql_query = f"SELECT count() FROM {table}"
            if match is not None:
                sql_query += " WHERE " + " AND ".join((f"{x} = :{x}" for x in match))
            if res := await _db.fetch_one(sql_query, match):
                return res["count()"]
            return 0

    async def get_rows(
        self,
        table: str,
        match: dict = None,
        order_by: str = None,
        db: Optional[Db] = None,
    ) -> List[Mapping]:
        """Get all rows for given table."""
        async with self.get_db(db) as _db:
            sql_query = f"SELECT * FROM {table}"
            if match is not None:
                sql_query += " WHERE " + " AND ".join((f"{x} = :{x}" for x in match))
            if order_by is not None:
                sql_query += f"ORDER BY {order_by}"
            return await _db.fetch_all(sql_query, match)

    async def get_rows_from_query(
        self, query: str, db: Optional[Db] = None
    ) -> List[Mapping]:
        """Get all rows for given custom query."""
        async with self.get_db(db) as _db:
            return await _db.fetch_all(query)

    async def search(
        self, table: str, search: str, column: str = "name", db: Optional[Db] = None
    ) -> List[Mapping]:
        """Search table by column."""
        async with self.get_db(db) as _db:
            sql_query = f'SELECT * FROM {table} WHERE {column} LIKE "{search}"'
            return await _db.fetch_all(sql_query)

    async def get_row(
        self, table: str, match: Dict[str, Any] = None, db: Optional[Db] = None
    ) -> Mapping | None:
        """Get single row for given table where column matches keys/values."""
        async with self.get_db(db) as _db:
            sql_query = f"SELECT * FROM {table} WHERE "
            sql_query += " AND ".join((f"{x} = :{x}" for x in match))
            return await _db.fetch_one(sql_query, match)

    async def insert_or_replace(
        self, table: str, values: Dict[str, Any], db: Optional[Db] = None
    ) -> Mapping:
        """Insert or replace data in given table."""
        async with self.get_db(db) as _db:
            keys = tuple(values.keys())
            sql_query = f'INSERT OR REPLACE INTO {table}({",".join(keys)})'
            sql_query += f' VALUES ({",".join((f":{x}" for x in keys))})'
            await _db.execute(sql_query, values)
            # return inserted/replaced item
            lookup_vals = {
                key: value
                for key, value in values.items()
                if value is not None and value != ""
            }
            return await self.get_row(table, lookup_vals, db=_db)

    async def update(
        self,
        table: str,
        match: Dict[str, Any],
        values: Dict[str, Any],
        db: Optional[Db] = None,
    ) -> Mapping:
        """Update record."""
        async with self.get_db(db) as _db:
            keys = tuple(values.keys())
            sql_query = (
                f'UPDATE {table} SET {",".join((f"{x}=:{x}" for x in keys))} WHERE '
            )
            sql_query += " AND ".join((f"{x} = :{x}" for x in match))
            await _db.execute(sql_query, {**match, **values})
            # return updated item
            return await self.get_row(table, match, db=_db)

    async def delete(
        self, table: str, match: Dict[str, Any], db: Optional[Db] = None
    ) -> None:
        """Delete data in given table."""
        async with self.get_db(db) as _db:
            sql_query = f"DELETE FROM {table}"
            sql_query += " WHERE " + " AND ".join((f"{x} = :{x}" for x in match))
            await _db.execute(sql_query, match)

    async def _migrate(self):
        """Perform database migration actions if needed."""
        async with self.get_db() as db:
            try:
                if prev_version := await self.get_setting("version", db):
                    prev_version = int(prev_version["value"])
                else:
                    prev_version = 0
            except (KeyError, ValueError):
                prev_version = 0

            if SCHEMA_VERSION != prev_version:
                self.logger.info(
                    "Performing database migration from %s to %s",
                    prev_version,
                    SCHEMA_VERSION,
                )
                # always create db tables if they don't exist to prevent errors trying to access them later
                await self.__create_database_tables(db)

                if prev_version < 4:
                    # schema version 3: too many breaking changes, rebuild db
                    await db.execute(f"DROP TABLE IF EXISTS {TABLE_ARTISTS}")
                    await db.execute(f"DROP TABLE IF EXISTS {TABLE_ALBUMS}")
                    await db.execute(f"DROP TABLE IF EXISTS {TABLE_TRACKS}")
                    await db.execute(f"DROP TABLE IF EXISTS {TABLE_PLAYLISTS}")
                    await db.execute(f"DROP TABLE IF EXISTS {TABLE_RADIOS}")
                    await db.execute(f"DROP TABLE IF EXISTS {TABLE_PROV_MAPPINGS}")
                    await db.execute(f"DROP TABLE IF EXISTS {TABLE_CACHE}")
                    # recreate missing tables
                    await self.__create_database_tables(db)

                if prev_version < 5:
                    # delete player_settings table: use generic settings table instead
                    await db.execute("DROP TABLE IF EXISTS queue_settings")
                    # recreate table
                    await self.__create_database_tables(db)

                if prev_version < 6:
                    # recreate radio items due to some changes
                    await db.execute(f"DROP TABLE IF EXISTS {TABLE_RADIOS}")
                    # recreate table
                    await self.__create_database_tables(db)
                    match = {"media_type": "radio"}
                    if await self.get_count(TABLE_PROV_MAPPINGS, match):
                        await self.delete(TABLE_PROV_MAPPINGS, match, db=db)

            # store current schema version
            await self.set_setting("version", str(SCHEMA_VERSION), db=db)

    @staticmethod
    async def __create_database_tables(db: Db) -> None:
        """Init generic database tables."""
        await db.execute(
            f"""CREATE TABLE IF NOT EXISTS {TABLE_PROV_MAPPINGS}(
                    item_id INTEGER NOT NULL,
                    media_type TEXT NOT NULL,
                    prov_item_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    quality INTEGER NULL,
                    details TEXT NULL,
                    url TEXT NULL,
                    UNIQUE(item_id, media_type, prov_item_id, provider)
                    );"""
        )
        await db.execute(
            f"""CREATE TABLE IF NOT EXISTS {TABLE_TRACK_LOUDNESS}(
                    item_id INTEGER NOT NULL,
                    provider TEXT NOT NULL,
                    loudness REAL,
                    UNIQUE(item_id, provider));"""
        )
        await db.execute(
            f"""CREATE TABLE IF NOT EXISTS {TABLE_PLAYLOG}(
                item_id INTEGER NOT NULL,
                provider TEXT NOT NULL,
                timestamp REAL,
                UNIQUE(item_id, provider));"""
        )
        await db.execute(
            f"""CREATE TABLE IF NOT EXISTS {TABLE_ALBUMS}(
                    item_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    sort_name TEXT NOT NULL,
                    album_type TEXT,
                    year INTEGER,
                    version TEXT,
                    in_library BOOLEAN DEFAULT 0,
                    upc TEXT,
                    musicbrainz_id TEXT,
                    artist json,
                    metadata json,
                    provider_ids json
                );"""
        )
        await db.execute(
            f"""CREATE TABLE IF NOT EXISTS {TABLE_ARTISTS}(
                    item_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    sort_name TEXT NOT NULL,
                    musicbrainz_id TEXT NOT NULL UNIQUE,
                    in_library BOOLEAN DEFAULT 0,
                    metadata json,
                    provider_ids json
                    );"""
        )
        await db.execute(
            f"""CREATE TABLE IF NOT EXISTS {TABLE_TRACKS}(
                    item_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    sort_name TEXT NOT NULL,
                    version TEXT,
                    duration INTEGER,
                    in_library BOOLEAN DEFAULT 0,
                    isrc TEXT,
                    musicbrainz_id TEXT,
                    artists json,
                    album json,
                    metadata json,
                    provider_ids json
                );"""
        )
        await db.execute(
            f"""CREATE TABLE IF NOT EXISTS {TABLE_PLAYLISTS}(
                    item_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    sort_name TEXT NOT NULL,
                    owner TEXT NOT NULL,
                    is_editable BOOLEAN NOT NULL,
                    checksum TEXT NOT NULL,
                    in_library BOOLEAN DEFAULT 0,
                    metadata json,
                    provider_ids json,
                    UNIQUE(name, owner)
                );"""
        )
        await db.execute(
            f"""CREATE TABLE IF NOT EXISTS {TABLE_RADIOS}(
                    item_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    sort_name TEXT NOT NULL,
                    in_library BOOLEAN DEFAULT 0,
                    metadata json,
                    provider_ids json
                );"""
        )
