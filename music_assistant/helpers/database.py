"""Database logic."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List, Mapping, Optional, Union

from databases import Database as Db
from sqlalchemy.sql import ClauseElement

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


SCHEMA_VERSION = 18

TABLE_TRACK_LOUDNESS = "track_loudness"
TABLE_PLAYLOG = "playlog"
TABLE_ARTISTS = "artists"
TABLE_ALBUMS = "albums"
TABLE_TRACKS = "tracks"
TABLE_PLAYLISTS = "playlists"
TABLE_RADIOS = "radios"
TABLE_CACHE = "cache"
TABLE_SETTINGS = "settings"
TABLE_THUMBS = "thumbnails"


class Database:
    """Class that holds the (logic to the) database."""

    def __init__(self, mass: MusicAssistant):
        """Initialize class."""
        self.url = mass.config.database_url
        self.mass = mass
        self.logger = mass.logger.getChild("db")
        # we maintain one global connection - otherwise we run into (dead)lock issues.
        # https://github.com/encode/databases/issues/456
        self._db = Db(self.url, timeout=360)

    async def setup(self) -> None:
        """Perform async initialization."""
        await self._db.connect()
        self.logger.info("Database connected.")
        await self._migrate()

    async def close(self) -> None:
        """Close db connection on exit."""
        self.logger.info("Database disconnected.")
        await self._db.disconnect()

    async def get_setting(self, key: str) -> str | None:
        """Get setting from settings table."""
        if db_row := await self.get_row(TABLE_SETTINGS, {"key": key}):
            return db_row["value"]
        return None

    async def set_setting(self, key: str, value: str) -> None:
        """Set setting in settings table."""
        if not isinstance(value, str):
            value = str(value)
        return await self.insert(
            TABLE_SETTINGS, {"key": key, "value": value}, allow_replace=True
        )

    async def get_rows(
        self,
        table: str,
        match: dict = None,
        order_by: str = None,
        limit: int = 500,
        offset: int = 0,
    ) -> List[Mapping]:
        """Get all rows for given table."""
        sql_query = f"SELECT * FROM {table}"
        if match is not None:
            sql_query += " WHERE " + " AND ".join((f"{x} = :{x}" for x in match))
        if order_by is not None:
            sql_query += f" ORDER BY {order_by}"
        sql_query += f" LIMIT {limit} OFFSET {offset}"
        return await self._db.fetch_all(sql_query, match)

    async def get_rows_from_query(
        self,
        query: str,
        params: Optional[dict] = None,
        limit: int = 500,
        offset: int = 0,
    ) -> List[Mapping]:
        """Get all rows for given custom query."""
        query = f"{query} LIMIT {limit} OFFSET {offset}"
        return await self._db.fetch_all(query, params)

    async def get_count_from_query(
        self,
        query: str,
        params: Optional[dict] = None,
    ) -> int:
        """Get row count for given custom query."""
        query = query.split("from", 1)[-1].split("FROM", 1)[-1]
        query = f"SELECT count() FROM {query}"
        if result := await self._db.fetch_one(query, params):
            return result[0]
        return 0

    async def search(
        self, table: str, search: str, column: str = "name"
    ) -> List[Mapping]:
        """Search table by column."""
        sql_query = f"SELECT * FROM {table} WHERE {column} LIKE :search"
        params = {"search": f"%{search}%"}
        return await self._db.fetch_all(sql_query, params)

    async def get_row(self, table: str, match: Dict[str, Any]) -> Mapping | None:
        """Get single row for given table where column matches keys/values."""
        sql_query = f"SELECT * FROM {table} WHERE "
        sql_query += " AND ".join((f"{x} = :{x}" for x in match))
        return await self._db.fetch_one(sql_query, match)

    async def insert(
        self,
        table: str,
        values: Dict[str, Any],
        allow_replace: bool = False,
    ) -> Mapping:
        """Insert data in given table."""
        keys = tuple(values.keys())
        if allow_replace:
            sql_query = f'INSERT OR REPLACE INTO {table}({",".join(keys)})'
        else:
            sql_query = f'INSERT INTO {table}({",".join(keys)})'
        sql_query += f' VALUES ({",".join((f":{x}" for x in keys))})'
        await self.execute(sql_query, values)
        # return inserted/replaced item
        lookup_vals = {
            key: value
            for key, value in values.items()
            if value is not None and value != ""
        }
        return await self.get_row(table, lookup_vals)

    async def insert_or_replace(self, table: str, values: Dict[str, Any]) -> Mapping:
        """Insert or replace data in given table."""
        return await self.insert(table=table, values=values, allow_replace=True)

    async def update(
        self,
        table: str,
        match: Dict[str, Any],
        values: Dict[str, Any],
    ) -> Mapping:
        """Update record."""
        keys = tuple(values.keys())
        sql_query = f'UPDATE {table} SET {",".join((f"{x}=:{x}" for x in keys))} WHERE '
        sql_query += " AND ".join((f"{x} = :{x}" for x in match))
        await self.execute(sql_query, {**match, **values})
        # return updated item
        return await self.get_row(table, match)

    async def delete(
        self, table: str, match: Optional[dict] = None, query: Optional[str] = None
    ) -> None:
        """Delete data in given table."""
        assert not (query and "where" in query.lower())
        sql_query = f"DELETE FROM {table} "
        if match:
            sql_query += " WHERE " + " AND ".join((f"{x} = :{x}" for x in match))
        elif query and "query" not in query.lower():
            sql_query += "WHERE " + query
        elif query:
            sql_query += query

        await self.execute(sql_query, match)

    async def delete_where_query(self, table: str, query: Optional[str] = None) -> None:
        """Delete data in given table using given where clausule."""
        sql_query = f"DELETE FROM {table} WHERE {query}"
        await self.execute(sql_query)

    async def execute(
        self, query: Union[ClauseElement, str], values: dict = None
    ) -> Any:
        """Execute command on the database."""
        return await self._db.execute(query, values)

    async def _migrate(self):
        """Perform database migration actions if needed."""
        # always create db tables if they don't exist to prevent errors trying to access them later
        await self.__create_database_tables()
        try:
            if prev_version := await self.get_setting("version"):
                prev_version = int(prev_version)
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

            if prev_version < 18:
                # too many changes, just recreate
                await self.execute(f"DROP TABLE IF EXISTS {TABLE_ARTISTS}")
                await self.execute(f"DROP TABLE IF EXISTS {TABLE_ALBUMS}")
                await self.execute(f"DROP TABLE IF EXISTS {TABLE_TRACKS}")
                await self.execute(f"DROP TABLE IF EXISTS {TABLE_PLAYLISTS}")
                await self.execute(f"DROP TABLE IF EXISTS {TABLE_RADIOS}")
                await self.execute(f"DROP TABLE IF EXISTS {TABLE_CACHE}")
                await self.execute(f"DROP TABLE IF EXISTS {TABLE_THUMBS}")
                await self.execute("DROP TABLE IF EXISTS provider_mappings")
                # recreate missing tables
                await self.__create_database_tables()

        # store current schema version
        await self.set_setting("version", str(SCHEMA_VERSION))

    async def __create_database_tables(self) -> None:
        """Init database tables."""
        await self.execute(
            """CREATE TABLE IF NOT EXISTS settings(
                    key TEXT PRIMARY KEY,
                    value TEXT
                );"""
        )
        await self.execute(
            f"""CREATE TABLE IF NOT EXISTS {TABLE_TRACK_LOUDNESS}(
                    item_id INTEGER NOT NULL,
                    provider TEXT NOT NULL,
                    loudness REAL,
                    UNIQUE(item_id, provider));"""
        )
        await self.execute(
            f"""CREATE TABLE IF NOT EXISTS {TABLE_PLAYLOG}(
                item_id INTEGER NOT NULL,
                provider TEXT NOT NULL,
                timestamp INTEGER DEFAULT 0,
                UNIQUE(item_id, provider));"""
        )
        await self.execute(
            f"""CREATE TABLE IF NOT EXISTS {TABLE_ALBUMS}(
                    item_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    sort_name TEXT NOT NULL,
                    sort_artist TEXT,
                    album_type TEXT,
                    year INTEGER,
                    version TEXT,
                    in_library BOOLEAN DEFAULT 0,
                    upc TEXT,
                    musicbrainz_id TEXT,
                    artists json,
                    metadata json,
                    provider_ids json,
                    timestamp INTEGER DEFAULT 0
                );"""
        )
        await self.execute(
            f"""CREATE TABLE IF NOT EXISTS {TABLE_ARTISTS}(
                    item_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    sort_name TEXT NOT NULL,
                    musicbrainz_id TEXT,
                    in_library BOOLEAN DEFAULT 0,
                    metadata json,
                    provider_ids json,
                    timestamp INTEGER DEFAULT 0
                    );"""
        )
        await self.execute(
            f"""CREATE TABLE IF NOT EXISTS {TABLE_TRACKS}(
                    item_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    sort_name TEXT NOT NULL,
                    sort_artist TEXT,
                    sort_album TEXT,
                    version TEXT,
                    duration INTEGER,
                    in_library BOOLEAN DEFAULT 0,
                    isrc TEXT,
                    musicbrainz_id TEXT,
                    artists json,
                    albums json,
                    metadata json,
                    provider_ids json,
                    timestamp INTEGER DEFAULT 0
                );"""
        )
        await self.execute(
            f"""CREATE TABLE IF NOT EXISTS {TABLE_PLAYLISTS}(
                    item_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    sort_name TEXT NOT NULL,
                    owner TEXT NOT NULL,
                    is_editable BOOLEAN NOT NULL,
                    in_library BOOLEAN DEFAULT 0,
                    metadata json,
                    provider_ids json,
                    timestamp INTEGER DEFAULT 0,
                    UNIQUE(name, owner)
                );"""
        )
        await self.execute(
            f"""CREATE TABLE IF NOT EXISTS {TABLE_RADIOS}(
                    item_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    sort_name TEXT NOT NULL,
                    in_library BOOLEAN DEFAULT 0,
                    metadata json,
                    provider_ids json,
                    timestamp INTEGER DEFAULT 0
                );"""
        )
        await self.execute(
            f"""CREATE TABLE IF NOT EXISTS {TABLE_CACHE}(
                    key TEXT UNIQUE NOT NULL, expires INTEGER NOT NULL, data TEXT, checksum TEXT NULL)"""
        )
        await self.execute(
            f"""CREATE TABLE IF NOT EXISTS {TABLE_THUMBS}(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT NOT NULL,
                size INTEGER DEFAULT 0,
                data BLOB,
                UNIQUE(path, size));"""
        )
        # create indexes
        # TODO: create indexes for the json columns ?
        await self.execute(
            "CREATE INDEX IF NOT EXISTS artists_in_library_idx on artists(in_library);"
        )
        await self.execute(
            "CREATE INDEX IF NOT EXISTS albums_in_library_idx on albums(in_library);"
        )
        await self.execute(
            "CREATE INDEX IF NOT EXISTS tracks_in_library_idx on tracks(in_library);"
        )
        await self.execute(
            "CREATE INDEX IF NOT EXISTS playlists_in_library_idx on playlists(in_library);"
        )
        await self.execute(
            "CREATE INDEX IF NOT EXISTS radios_in_library_idx on radios(in_library);"
        )
        await self.execute(
            "CREATE INDEX IF NOT EXISTS artists_sort_name_idx on artists(sort_name);"
        )
        await self.execute(
            "CREATE INDEX IF NOT EXISTS albums_sort_name_idx on albums(sort_name);"
        )
        await self.execute(
            "CREATE INDEX IF NOT EXISTS tracks_sort_name_idx on tracks(sort_name);"
        )
        await self.execute(
            "CREATE INDEX IF NOT EXISTS playlists_sort_name_idx on playlists(sort_name);"
        )
        await self.execute(
            "CREATE INDEX IF NOT EXISTS radios_sort_name_idx on radios(sort_name);"
        )
        await self.execute(
            "CREATE INDEX IF NOT EXISTS artists_musicbrainz_id_idx on artists(musicbrainz_id);"
        )
        await self.execute(
            "CREATE INDEX IF NOT EXISTS albums_musicbrainz_id_idx on albums(musicbrainz_id);"
        )
        await self.execute(
            "CREATE INDEX IF NOT EXISTS tracks_musicbrainz_id_idx on tracks(musicbrainz_id);"
        )
        await self.execute(
            "CREATE INDEX IF NOT EXISTS tracks_isrc_idx on tracks(isrc);"
        )
        await self.execute("CREATE INDEX IF NOT EXISTS albums_upc_idx on albums(upc);")
