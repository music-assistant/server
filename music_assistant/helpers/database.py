"""Database logic."""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, Dict, List, Mapping, Optional

from databases import Database as Db
from databases import DatabaseURL
from music_assistant.helpers.typing import MusicAssistant

# pylint: disable=invalid-name


class Database:
    """Class that holds the (logic to the) database."""

    def __init__(self, mass: MusicAssistant, url: DatabaseURL):
        """Initialize class."""
        self.url = url
        self.mass = mass
        self.logger = mass.logger.getChild("db")

    @asynccontextmanager
    async def get_db(self, db: Optional[Db] = None) -> Db:
        """Context manager helper to get the active db connection."""
        if db is not None:
            yield db
        else:
            async with Db(self.url, timeout=360) as _db:
                yield _db

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
            await _db.execute(sql_query)
