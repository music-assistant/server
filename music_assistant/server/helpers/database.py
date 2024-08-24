"""Database helpers and logic."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from sqlite3 import OperationalError
from typing import TYPE_CHECKING, Any

import aiosqlite

from music_assistant.constants import MASS_LOGGER_NAME

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Mapping

LOGGER = logging.getLogger(f"{MASS_LOGGER_NAME}.database")

ENABLE_DEBUG = os.environ.get("PYTHONDEVMODE") == "1"


@asynccontextmanager
async def debug_query(sql_query: str, query_params: dict | None = None):
    """Time the processing time of an sql query."""
    if not ENABLE_DEBUG:
        yield
        return
    time_start = time.time()
    try:
        yield
    except OperationalError as err:
        LOGGER.error(f"{err}\n{sql_query}")
        raise
    finally:
        process_time = time.time() - time_start
        if process_time > 0.5:
            # log slow queries
            for key, value in (query_params or {}).items():
                sql_query = sql_query.replace(f":{key}", repr(value))
            LOGGER.warning("SQL Query took %s seconds! (\n%s\n", process_time, sql_query)


def query_params(query: str, params: dict[str, Any] | None) -> tuple[str, dict[str, Any]]:
    """Extend query parameters support."""
    if params is None:
        return (query, params)
    count = 0
    result_query = query
    result_params = {}
    for key, value in params.items():
        # add support for a list within the query params
        # recreates the params as (:_param_0, :_param_1) etc
        if isinstance(value, list | tuple):
            subparams = []
            for subval in value:
                subparam_name = f"_param_{count}"
                result_params[subparam_name] = subval
                subparams.append(subparam_name)
                count += 1
            params_str = ",".join(f":{x}" for x in subparams)
            result_query = result_query.replace(f" :{key}", f" ({params_str})")
        else:
            result_params[key] = params[key]
    return (result_query, result_params)


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
        await self.execute("PRAGMA analysis_limit=400;")
        await self.execute("PRAGMA optimize;")
        await self.commit()

    async def close(self) -> None:
        """Close db connection on exit."""
        await self.execute("PRAGMA optimize;")
        await self.commit()
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
        if limit:
            sql_query += f" LIMIT {limit} OFFSET {offset}"
        async with debug_query(sql_query):
            return await self._db.execute_fetchall(sql_query, match)

    async def get_rows_from_query(
        self,
        query: str,
        params: dict | None = None,
        limit: int = 500,
        offset: int = 0,
    ) -> list[Mapping]:
        """Get all rows for given custom query."""
        if limit:
            query += f" LIMIT {limit} OFFSET {offset}"
        _query, _params = query_params(query, params)
        async with debug_query(_query, _params):
            return await self._db.execute_fetchall(_query, _params)

    async def get_count_from_query(
        self,
        query: str,
        params: dict | None = None,
    ) -> int:
        """Get row count for given custom query."""
        query = f"SELECT count() FROM ({query})"
        _query, _params = query_params(query, params)
        async with debug_query(_query):
            async with self._db.execute(_query, _params) as cursor:
                if result := await cursor.fetchone():
                    return result[0]
            return 0

    async def get_count(
        self,
        table: str,
    ) -> int:
        """Get row count for given table."""
        query = f"SELECT count(*) FROM {table}"
        async with debug_query(query):
            async with self._db.execute(query) as cursor:
                if result := await cursor.fetchone():
                    return result[0]
            return 0

    async def search(self, table: str, search: str, column: str = "name") -> list[Mapping]:
        """Search table by column."""
        sql_query = f"SELECT * FROM {table} WHERE {table}.{column} LIKE :search"
        params = {"search": f"%{search}%"}
        async with debug_query(sql_query, params):
            return await self._db.execute_fetchall(sql_query, params)

    async def get_row(self, table: str, match: dict[str, Any]) -> Mapping | None:
        """Get single row for given table where column matches keys/values."""
        sql_query = f"SELECT * FROM {table} WHERE "
        sql_query += " AND ".join(f"{table}.{x} = :{x}" for x in match)
        async with debug_query(sql_query, match), self._db.execute(sql_query, match) as cursor:
            return await cursor.fetchone()

    async def insert(
        self,
        table: str,
        values: dict[str, Any],
        allow_replace: bool = False,
    ) -> int:
        """Insert data in given table."""
        keys = tuple(values.keys())
        if allow_replace:
            sql_query = f'INSERT OR REPLACE INTO {table}({",".join(keys)})'
        else:
            sql_query = f'INSERT INTO {table}({",".join(keys)})'
        sql_query += f' VALUES ({",".join(f":{x}" for x in keys)})'
        row_id = await self._db.execute_insert(sql_query, values)
        await self._db.commit()
        return row_id[0]

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
        elif query and "where" not in query.lower():
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

    async def commit(self) -> None:
        """Commit the current transaction."""
        return await self._db.commit()

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
            await asyncio.sleep(0)  # yield to eventloop
            offset += limit

    async def vacuum(self) -> None:
        """Run vacuum command on database."""
        await self._db.execute("VACUUM")
        await self._db.commit()
