"""Database logic."""
from __future__ import annotations
import asyncio

from typing import Any, Dict, List, Mapping

from databases import Database as Db, DatabaseURL
from databases.core import ClauseElement

from music_assistant import EventDetails
from music_assistant.constants import EventType
from music_assistant.helpers.typing import MusicAssistant


class Database(Db):
    """Class that holds the (logic to the) database."""

    def __init__(self, mass: MusicAssistant, url: DatabaseURL):
        """Initialize class."""
        super().__init__(url, timeout=360)
        self.mass = mass
        self.logger = mass.logger.getChild("db")
        self._lock = asyncio.Lock()
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

    async def get_rows_from_query(
        self, query: str
    ) -> List[Mapping]:
        """Get all rows for given custom query."""
        return await self.fetch_all(query)

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
        sql_query = f'UPDATE {table} SET {",".join((f"{x}=:{x}" for x in keys))} WHERE '
        sql_query += " AND ".join((f"{x} = :{x}" for x in match))
        await self.execute(sql_query, {**match, **values})
        # return updated item
        return await self.get_row(table, match)

    async def delete(self, table: str, match: Dict[str, Any]) -> None:
        """Delete data in given table."""
        sql_query = f"DELETE FROM {table}"
        sql_query += " WHERE " + " AND ".join((f"{x} = :{x}" for x in match))
        await self.execute(sql_query)

    async def execute(self, query: ClauseElement | str, values: dict = None) -> Any:
        """Execute query on database."""
        async with self._lock:
            await super().execute(query, values)

    async def __on_shutdown_event(
        self, event: EventType, details: EventDetails
    ) -> None:
        """Handle shutdown event."""
        await self.disconnect()
