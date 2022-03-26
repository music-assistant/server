"""Database logic."""
from __future__ import annotations

from typing import Any, Dict, List, Mapping

from databases import Database, DatabaseURL

from music_assistant import EventDetails
from music_assistant.constants import EventType
from music_assistant.helpers.typing import MusicAssistant


class DatabaseController(Database):
    """Class that holds the (logic to the) database."""

    def __init__(self, mass: MusicAssistant, url: DatabaseURL):
        """Initialize class."""
        super().__init__(url)
        self.mass = mass
        self.logger = mass.logger.getChild("db")
        mass.subscribe(self.__on_shutdown_event, EventType.SHUTDOWN)

    async def setup(self):
        """Async initialize of module."""
        await self.connect()

    async def get_rows(
        self, table: str, match: dict = None, order_by: str = None
    ) -> List[Mapping]:
        """Get all rows for given table."""
        sql_query = f"SELECT * FROM {table}"
        if match is not None:
            sql_query += " WHERE " + " AND ".join((f"{x} = :{x}" for x in match))
        if order_by is not None:
            sql_query += f"ORDER BY {order_by}"
        return await self.fetch_all(sql_query, match)

    async def search(
        self, table: str, search: str, column: str = "name"
    ) -> List[Mapping]:
        """Search table by column."""
        sql_query = f'SELECT * FROM {table} WHERE {column} LIKE "{search}"'
        return await self.fetch_all(sql_query)

    async def get_row(self, table: str, match: Dict[str, Any] = None) -> Mapping | None:
        """Get single row for given table where column matches keys/values."""
        sql_query = f"SELECT * FROM {table} WHERE "
        sql_query += " AND ".join((f"{x} = :{x}" for x in match))
        return await self.fetch_one(sql_query, match)

    async def insert_or_replace(self, table: str, values: Dict[str, Any]) -> Mapping:
        """Insert or replace data in given table."""
        keys = tuple(values.keys())
        sql_query = f'INSERT OR REPLACE INTO {table}({",".join(keys)})'
        sql_query += f' VALUES ({",".join((f":{x}" for x in keys))})'
        await self.execute(sql_query, values)
        # return inserted/replaced item
        return await self.get_row(table, values)

    async def update(
        self, table: str, match: Dict[str, Any], values: Dict[str, Any]
    ) -> Mapping:
        """Update record."""
        keys = tuple(values.keys())
        sql_query = f"""
            UPDATE {table} "
            SET ({",".join((f":{x}" for x in keys))})'
        """
        sql_query += "WHERE " + " AND ".join((f"{x} = :{x}" for x in match))
        await self.execute(sql_query, values)
        # return updated item
        return await self.get_row(table, match)

    async def __on_shutdown_event(
        self, event: EventType, details: EventDetails
    ) -> None:
        """Handle shutdown event."""
        await self.disconnect()


# async def check_migrations(mass: MusicAssistant):
#     """Check for any migrations that need to be done."""

#     is_fresh_setup = len(mass.config.stored_config.keys()) == 0
#     prev_version = packaging.version.parse(mass.config.stored_config.get("version", ""))

#     # perform version specific migrations
#     if not is_fresh_setup and prev_version < packaging.version.parse("0.1.1"):
#         await run_migration_1(mass)

#     # store version in config
#     mass.config.stored_config["version"] = app_version
#     # create unique server id from machine id
#     if "server_id" not in mass.config.stored_config:
#         mass.config.stored_config["server_id"] = str(uuid.getnode())
#     if "initialized" not in mass.config.stored_config:
#         mass.config.stored_config["initialized"] = False
#     if "friendly_name" not in mass.config.stored_config:
#         mass.config.stored_config["friendly_name"] = get_hostname()
#     mass.config.save()

#     # create default db tables (if needed)
#     await create_db_tables(mass.database.db_file)


# async def run_migration_1(mass: MusicAssistant):
#     """Run migration for version 0.1.1."""
#     # 0.1.0 introduced major changes to all data models and db structure
#     # a full refresh of data is unavoidable
#     data_path = mass.config.data_path
#     tracks_loudness = []

#     for dbname in ["mass.db", "database.db", "music_assistant.db"]:
#         filename = os.path.join(data_path, dbname)
#         if os.path.isfile(filename):
#             # we try to backup the loudness measurements
#             async with aiosqlite.connect(filename, timeout=120) as db_conn:
#                 db_conn.row_factory = aiosqlite.Row
#                 sql_query = "SELECT * FROM track_loudness"
#                 for db_row in await db_conn.execute_fetchall(sql_query, ()):
#                     if "provider_track_id" in db_row.keys():
#                         track_id = db_row["provider_track_id"]
#                     else:
#                         track_id = db_row["item_id"]
#                     tracks_loudness.append(
#                         (
#                             track_id,
#                             db_row["provider"],
#                             db_row["loudness"],
#                         )
#                     )
#             # remove old db file
#             os.remove(filename)

#     # remove old cache db
#     for dbname in ["cache.db", ".cache.db"]:
#         filename = os.path.join(data_path, dbname)
#         if os.path.isfile(filename):
#             os.remove(filename)

#     # remove old thumbs db
#     for dirname in ["thumbs", ".thumbs", ".thumbnails"]:
#         dirname = os.path.join(data_path, dirname)
#         if os.path.isdir(dirname):
#             shutil.rmtree(dirname, True)

#     # create default db tables (if needed)
#     await create_db_tables(mass.database.db_file)

#     # restore loudness measurements
#     if tracks_loudness:
#         async with aiosqlite.connect(mass.database.db_file, timeout=120) as db_conn:
#             sql_query = """INSERT or REPLACE INTO track_loudness
#                 (item_id, provider, loudness) VALUES(?,?,?);"""
#             for item in tracks_loudness:
#                 await db_conn.execute(sql_query, item)
#             await db_conn.commit()
