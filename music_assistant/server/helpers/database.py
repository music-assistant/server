"""Database helpers and logic."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import aiosqlite

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Mapping


class DatabaseConnection:
    """Class that holds the (connection to the) database with some convenience helper functions."""

    _db: aiosqlite.Connection

    def __init__(self, db_path: str) -> None:
        """Initialize class."""
        self.db_path = db_path

    async def setup(self) -> None:
        """Perform async initialization."""
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row

    async def close(self) -> None:
        """Close db connection on exit."""
        await self._db.close()

    async def get_rows(
        self,
        table: str,
        match: dict | None = None,
        order_by: str | None = None,
        limit: int = 500,
        offset: int = 0,
    ) -> list[Mapping]:
        """Get all rows for given table."""
        sql_query = f"SELECT * FROM {table}"
        if match is not None:
            sql_query += " WHERE " + " AND ".join(f"{x} = :{x}" for x in match)
        if order_by is not None:
            sql_query += f" ORDER BY {order_by}"
        sql_query += f" LIMIT {limit} OFFSET {offset}"
        return await self._db.execute_fetchall(sql_query, match)

    async def get_rows_from_query(
        self,
        query: str,
        params: dict | None = None,
        limit: int = 500,
        offset: int = 0,
    ) -> list[Mapping]:
        """Get all rows for given custom query."""
        query = f"{query} LIMIT {limit} OFFSET {offset}"
        return await self._db.execute_fetchall(query, params)

    async def get_count_from_query(
        self,
        query: str,
        params: dict | None = None,
    ) -> int:
        """Get row count for given custom query."""
        query = f"SELECT count() FROM ({query})"
        async with self._db.execute(query, params) as cursor:
            if result := await cursor.fetchone():
                return result[0]
        return 0

    async def get_count(
        self,
        table: str,
    ) -> int:
        """Get row count for given table."""
        query = f"SELECT count(*) FROM {table}"
        async with self._db.execute(query) as cursor:
            if result := await cursor.fetchone():
                return result[0]
        return 0

    async def search(self, table: str, search: str, column: str = "name") -> list[Mapping]:
        """Search table by column."""
        sql_query = f"SELECT * FROM {table} WHERE {table}.{column} LIKE :search"
        params = {"search": f"%{search}%"}
        return await self._db.execute_fetchall(sql_query, params)

    async def get_row(self, table: str, match: dict[str, Any]) -> Mapping | None:
        """Get single row for given table where column matches keys/values."""
        sql_query = f"SELECT * FROM {table} WHERE "
        sql_query += " AND ".join(f"{table}.{x} = :{x}" for x in match)
        async with self._db.execute(sql_query, match) as cursor:
            return await cursor.fetchone()

    async def insert(
        self,
        table: str,
        values: dict[str, Any],
        allow_replace: bool = False,
    ) -> Mapping:
        """Insert data in given table."""
        keys = tuple(values.keys())
        if allow_replace:
            sql_query = f'INSERT OR REPLACE INTO {table}({",".join(keys)})'
        else:
            sql_query = f'INSERT INTO {table}({",".join(keys)})'
        sql_query += f' VALUES ({",".join(f":{x}" for x in keys)})'
        await self.execute(sql_query, values)
        await self._db.commit()
        # return inserted/replaced item
        lookup_vals = {key: value for key, value in values.items() if value not in (None, "")}
        return await self.get_row(table, lookup_vals)

    async def insert_or_replace(self, table: str, values: dict[str, Any]) -> Mapping:
        """Insert or replace data in given table."""
        return await self.insert(table=table, values=values, allow_replace=True)

    async def update(
        self,
        table: str,
        match: dict[str, Any],
        values: dict[str, Any],
    ) -> Mapping:
        """Update record."""
        keys = tuple(values.keys())
        sql_query = f'UPDATE {table} SET {",".join(f"{x}=:{x}" for x in keys)} WHERE '
        sql_query += " AND ".join(f"{x} = :{x}" for x in match)
        await self.execute(sql_query, {**match, **values})
        await self._db.commit()
        # return updated item
        return await self.get_row(table, match)

    async def delete(self, table: str, match: dict | None = None, query: str | None = None) -> None:
        """Delete data in given table."""
        assert not (query and "where" in query.lower())
        sql_query = f"DELETE FROM {table} "
        if match:
            sql_query += " WHERE " + " AND ".join(f"{x} = :{x}" for x in match)
        elif query and "query" not in query.lower():
            sql_query += "WHERE " + query
        elif query:
            sql_query += query
        await self.execute(sql_query, match)
        await self._db.commit()

    async def delete_where_query(self, table: str, query: str | None = None) -> None:
        """Delete data in given table using given where clausule."""
        sql_query = f"DELETE FROM {table} WHERE {query}"
        await self.execute(sql_query)
        await self._db.commit()

    async def execute(self, query: str, values: dict | None = None) -> Any:
        """Execute command on the database."""
        return await self._db.execute(query, values)

    async def iter_items(
        self,
        table: str,
        match: dict | None = None,
    ) -> AsyncGenerator[Mapping, None]:
        """Iterate all items within a table."""
        limit: int = 500
        offset: int = 0
        while True:
            next_items = await self.get_rows(
                table=table,
                match=match,
                offset=offset,
                limit=limit,
            )
            for item in next_items:
                yield item
            if len(next_items) < limit:
                break
            offset += limit

    async def vacuum(self) -> None:
        """Run vacuum command on database."""
        await self._db.execute("VACUUM")
        await self._db.commit()
