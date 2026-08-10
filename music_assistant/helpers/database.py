"""Database helpers and logic."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections.abc import Mapping
from contextlib import asynccontextmanager
from contextvars import ContextVar
from sqlite3 import OperationalError
from typing import TYPE_CHECKING, Any, cast

import aiosqlite

from music_assistant.constants import MASS_LOGGER_NAME

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Sequence

LOGGER = logging.getLogger(f"{MASS_LOGGER_NAME}.database")


class _UnsetType:
    """Sentinel value to indicate a field should use the database default."""

    _instance: _UnsetType | None = None

    def __new__(cls) -> _UnsetType:  # noqa: PYI034  # singleton sentinel always returns the one instance, not Self
        """Create singleton instance."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        """Return string representation."""
        return "UNSET"

    def __bool__(self) -> bool:
        """Return False for boolean context."""
        return False


UNSET: _UnsetType = _UnsetType()

ENABLE_DEBUG = os.environ.get("PYTHONDEVMODE") == "1"

SLOW_QUERY_THRESHOLD = 0.5
_STALL_SAMPLE_INTERVAL = 0.05


class _LoopStallTracker:
    """Samples how long the event loop spends unavailable, so query timings can discount it."""

    def __init__(self) -> None:
        """Initialize class."""
        self._total = 0.0
        self._last_tick = 0.0
        self._task: asyncio.Task[None] | None = None
        self._users = 0

    @property
    def total(self) -> float:
        """Return the stall time recorded so far, to pass to `stalled_since` later on."""
        return self._total

    def stalled_since(self, total: float) -> float:
        """Return how long the event loop was unavailable since the given `total`."""
        if self._task is None:
            return 0.0
        # a stall that is still in progress cannot have been sampled yet: the loop only frees
        # up at its end and hands the awaiting query its result first, so the time the sampler
        # is currently overdue by counts towards this window as well
        overdue = asyncio.get_running_loop().time() - self._last_tick - _STALL_SAMPLE_INTERVAL
        return self._total - total + max(0.0, overdue)

    def acquire(self) -> None:
        """Start sampling (if not already running) on behalf of one more user."""
        if not ENABLE_DEBUG:
            return
        self._users += 1
        if self._task is None:
            loop = asyncio.get_running_loop()
            self._last_tick = loop.time()
            self._task = loop.create_task(self._sample())

    def release(self) -> None:
        """Stop sampling once the last user has released the tracker."""
        self._users = max(0, self._users - 1)
        if self._users == 0 and self._task is not None:
            self._task.cancel()
            self._task = None

    async def _sample(self) -> None:
        """Record how late each wake-up is, which is the time the loop was unavailable."""
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(_STALL_SAMPLE_INTERVAL)
            now = loop.time()
            self._total += max(0.0, now - self._last_tick - _STALL_SAMPLE_INTERVAL)
            self._last_tick = now


_loop_stalls = _LoopStallTracker()


@asynccontextmanager
async def debug_query(
    sql_query: str, query_params: dict[str, Any] | None = None
) -> AsyncGenerator[None]:
    """Time the processing time of an sql query."""
    if not ENABLE_DEBUG:
        yield
        return
    time_start = time.monotonic()
    stalled_start = _loop_stalls.total
    try:
        yield
    except OperationalError as err:
        LOGGER.error(f"{err}\n{sql_query}")
        raise
    finally:
        # queries run on aiosqlite's connection thread, so the awaited wall time also covers any
        # stretch the loop was blocked elsewhere and could not deliver the result. Discounting
        # that keeps an unrelated blocking callback from reporting as a slow query; a stall that
        # overlaps a genuinely slow query is discounted too, so this under-reports rather than
        # points at the wrong culprit.
        process_time = time.monotonic() - time_start - _loop_stalls.stalled_since(stalled_start)
        if process_time > SLOW_QUERY_THRESHOLD:
            # log slow queries
            for key, value in (query_params or {}).items():
                sql_query = sql_query.replace(f":{key}", repr(value))
            LOGGER.warning("SQL Query took %s seconds! (\n%s\n", process_time, sql_query)


def query_params(query: str, params: dict[str, Any] | None) -> tuple[str, dict[str, Any]]:
    """Extend query parameters support."""
    if params is None:
        return (query, {})
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
            # replace the placeholder with the expanded (:_param_x, ...) list;
            # consume optional parens already around the placeholder and use a
            # word boundary so placeholders sharing the same prefix are untouched
            result_query = re.sub(
                rf"\(\s*:{re.escape(key)}\b\s*\)|:{re.escape(key)}\b",
                f"({params_str})",
                result_query,
            )
        else:
            result_params[key] = value
    return (result_query, result_params)


def get_sqlite_memory_settings() -> tuple[int, int]:
    """
    Return (cache_size_kib, mmap_size_bytes) scaled to available system memory.

    The page cache is a per-connection ceiling that is filled lazily, so a small database
    never consumes a large ceiling. Hosts with ample RAM keep the previous generous values
    (and a much bigger cache on very large hosts) so performance is unaffected — only memory-
    constrained devices are scaled down. Returns the generous defaults when memory is
    unknown (e.g. Windows), so those hosts fail open to full performance.
    """
    # imported lazily to keep this low-level helper free of the heavier util import chain
    from music_assistant.helpers.util import get_total_system_memory  # noqa: PLC0415

    # SQLite caps mmap_size at its build-time SQLITE_MAX_MMAP_SIZE (~2GiB), so the previous
    # 30GB request was already effectively ~2GiB. We keep that ceiling on capable hosts and
    # only request less on memory-constrained devices (where a large DB would otherwise map
    # most of the file into reclaimable RSS). The page cache is the only fast path for the
    # part of a database beyond that ~2GiB window, so hosts with lots of RAM get a much larger
    # cache to keep a very large library hot.
    gib = 1024**3
    total_ram_gb = get_total_system_memory()
    if total_ram_gb >= 16.0:
        # very large host: cache enough to keep a multi-GB library hot in memory
        return 1024000, 2 * gib
    if total_ram_gb >= 12.0:
        return 512000, 2 * gib
    if total_ram_gb >= 8.0:
        # plenty of RAM: favour performance with a larger page cache
        return 128000, 2 * gib
    if total_ram_gb == 0.0 or total_ram_gb >= 4.0:
        # unknown (fail open) or capable: keep the previous 64MB cache and ~2GiB mmap ceiling
        return 64000, 2 * gib
    if total_ram_gb >= 2.0:
        return 32000, gib
    # memory-constrained device: keep each connection's footprint small
    return 16000, 256 * 1024 * 1024


class DatabaseConnection:
    """Class that holds the (connection to the) database with some convenience helper functions."""

    _db: aiosqlite.Connection

    def __init__(self, db_path: str) -> None:
        """Initialize class."""
        self.db_path = db_path
        # per-instance ContextVar (instead of module level) so multiple database
        # connections (library/cache/auth) track their deferred_commit scopes
        # independently; only a handful of long-lived instances exist per process
        self._deferred_commit_depth: ContextVar[int] = ContextVar(
            "deferred_commit_depth", default=0
        )
        self._tracking_loop_stalls = False

    async def setup(
        self,
        cache_size_kib: int | None = None,
        mmap_size_bytes: int | None = None,
    ) -> None:
        """
        Perform async initialization.

        :param cache_size_kib: SQLite page-cache ceiling for this connection, in KiB.
            Defaults to a value scaled to the host's available memory.
        :param mmap_size_bytes: SQLite memory-map ceiling for this connection, in bytes.
            Defaults to a value scaled to the host's available memory.
        """
        default_cache_kib, default_mmap_bytes = get_sqlite_memory_settings()
        # coerce + clamp to non-negative ints so the values are always safe to interpolate
        cache_size_kib = max(
            0, int(default_cache_kib if cache_size_kib is None else cache_size_kib)
        )
        mmap_size_bytes = max(
            0, int(default_mmap_bytes if mmap_size_bytes is None else mmap_size_bytes)
        )
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        # setup some default settings for more performance
        await self.execute("PRAGMA analysis_limit=10000;")
        await self.execute("PRAGMA locking_mode=exclusive;")
        await self.execute("PRAGMA journal_mode=WAL;")
        await self.execute("PRAGMA journal_size_limit = 6144000;")
        await self.execute("PRAGMA synchronous=normal;")
        await self.execute("PRAGMA temp_store=memory;")
        await self.execute(f"PRAGMA mmap_size = {mmap_size_bytes};")
        await self.execute(f"PRAGMA cache_size = -{cache_size_kib};")
        await self.commit()
        _loop_stalls.acquire()
        self._tracking_loop_stalls = True

    async def close(self) -> None:
        """Close db connection on exit."""
        await self.execute("PRAGMA optimize;")
        await self.commit()
        await self._db.close()
        # mirror the acquire in setup() exactly, so a connection that failed to set up or is
        # closed twice cannot release a slot that belongs to one of the other connections
        if self._tracking_loop_stalls:
            self._tracking_loop_stalls = False
            _loop_stalls.release()

    async def get_rows(
        self,
        table: str,
        match: dict[str, Any] | None = None,
        order_by: str | None = None,
        limit: int = 500,
        offset: int = 0,
    ) -> list[Mapping[str, Any]]:
        """Get all rows for given table."""
        sql_query = f"SELECT * FROM {table}"
        if match is not None:
            sql_query += " WHERE " + " AND ".join(f"{x} = :{x}" for x in match)
        if order_by is not None:
            sql_query += f" ORDER BY {order_by}"
        if limit:
            sql_query += f" LIMIT {limit} OFFSET {offset}"
        async with debug_query(sql_query):
            return cast(
                "list[Mapping[str, Any]]", await self._db.execute_fetchall(sql_query, match)
            )

    async def get_rows_from_query(
        self,
        query: str,
        params: dict[str, Any] | None = None,
        limit: int = 500,
        offset: int = 0,
    ) -> list[Mapping[str, Any]]:
        """Get all rows for given custom query."""
        if limit:
            query += f" LIMIT {limit} OFFSET {offset}"
        _query, _params = query_params(query, params)
        async with debug_query(_query, _params):
            return cast("list[Mapping[str, Any]]", await self._db.execute_fetchall(_query, _params))

    async def iter_rows_from_query(
        self,
        query: str,
        params: dict[str, Any] | None = None,
    ) -> AsyncGenerator[Mapping[str, Any]]:
        """Stream rows for a given custom query without materializing the full result."""
        _query, _params = query_params(query, params)
        async with debug_query(_query, _params), self._db.execute(_query, _params) as cursor:
            async for row in cursor:
                yield cast("Mapping[str, Any]", row)

    async def get_count_from_query(
        self,
        query: str,
        params: dict[str, Any] | None = None,
    ) -> int:
        """Get row count for given custom query."""
        query = f"SELECT count() FROM ({query})"
        _query, _params = query_params(query, params)
        async with debug_query(_query):
            async with self._db.execute(_query, _params) as cursor:
                if result := await cursor.fetchone():
                    assert isinstance(result[0], int)  # for type checking
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
                    assert isinstance(result[0], int)  # for type checking
                    return result[0]
            return 0

    async def search(
        self, table: str, search: str, column: str = "name"
    ) -> list[Mapping[str, Any]]:
        """Search table by column."""
        sql_query = f"SELECT * FROM {table} WHERE {table}.{column} LIKE :search"
        params = {"search": f"%{search}%"}
        async with debug_query(sql_query, params):
            return cast(
                "list[Mapping[str, Any]]", await self._db.execute_fetchall(sql_query, params)
            )

    async def get_row(self, table: str, match: dict[str, Any]) -> Mapping[str, Any] | None:
        """Get single row for given table where column matches keys/values."""
        sql_query = f"SELECT * FROM {table} WHERE "
        sql_query += " AND ".join(f"{table}.{x} = :{x}" for x in match)
        async with debug_query(sql_query, match), self._db.execute(sql_query, match) as cursor:
            return cast("Mapping[str, Any] | None", await cursor.fetchone())

    async def insert(
        self,
        table: str,
        values: dict[str, Any],
        allow_replace: bool = False,
    ) -> int:
        """Insert data in given table."""
        # Filter out UNSET values so database defaults are used
        values = {k: v for k, v in values.items() if v is not UNSET}
        keys = tuple(values.keys())
        if allow_replace:
            sql_query = f"INSERT OR REPLACE INTO {table}({','.join(keys)})"
        else:
            sql_query = f"INSERT INTO {table}({','.join(keys)})"
        sql_query += f" VALUES ({','.join(f':{x}' for x in keys)})"
        row_id = await self._db.execute_insert(sql_query, values)
        await self._maybe_commit()
        assert row_id is not None  # for type checking
        assert isinstance(row_id[0], int)  # for type checking
        return row_id[0]

    async def insert_or_replace(self, table: str, values: dict[str, Any]) -> int:
        """Insert or replace data in given table."""
        return await self.insert(table=table, values=values, allow_replace=True)

    async def upsert(self, table: str, values: dict[str, Any]) -> None:
        """Upsert data in given table."""
        # Filter out UNSET values so database defaults are used
        values = {k: v for k, v in values.items() if v is not UNSET}
        keys = tuple(values.keys())
        sql_query = (
            f"INSERT INTO {table}({','.join(keys)}) VALUES ({','.join(f':{x}' for x in keys)})"
        )
        sql_query += f" ON CONFLICT DO UPDATE SET {','.join(f'{x}=:{x}' for x in keys)}"
        await self._db.execute(sql_query, values)
        await self._maybe_commit()

    async def upsert_many(self, table: str, values: Sequence[dict[str, Any]]) -> None:
        """
        Upsert multiple rows in the given table with a single commit.

        :param table: The table to upsert the rows into.
        :param values: The rows to upsert, each given as a column->value dict.
            Rows do not need to share the same set of columns.
        """
        if not values:
            return
        # rows are grouped by their column set so each group can be executed as a
        # single (prepared) statement, while omitted columns keep their existing
        # value on conflict - identical to calling upsert() per row
        rows_per_column_set: dict[tuple[str, ...], list[dict[str, Any]]] = {}
        for row in values:
            # Filter out UNSET values so database defaults are used
            filtered_row = {k: v for k, v in row.items() if v is not UNSET}
            rows_per_column_set.setdefault(tuple(sorted(filtered_row)), []).append(filtered_row)
        for keys, rows in rows_per_column_set.items():
            sql_query = (
                f"INSERT INTO {table}({','.join(keys)}) VALUES ({','.join(f':{x}' for x in keys)})"
            )
            sql_query += f" ON CONFLICT DO UPDATE SET {','.join(f'{x}=:{x}' for x in keys)}"
            await self._db.executemany(sql_query, rows)
        await self._maybe_commit()

    async def update(
        self,
        table: str,
        match: dict[str, Any],
        values: dict[str, Any],
    ) -> None:
        """Update record."""
        # Filter out UNSET values so those fields are not updated
        values = {k: v for k, v in values.items() if v is not UNSET}
        keys = tuple(values.keys())
        sql_query = f"UPDATE {table} SET {','.join(f'{x}=:{x}' for x in keys)} WHERE "
        sql_query += " AND ".join(f"{x} = :{x}" for x in match)
        await self.execute(sql_query, {**match, **values})
        await self._maybe_commit()

    async def delete(
        self, table: str, match: dict[str, Any] | None = None, query: str | None = None
    ) -> None:
        """Delete data in given table."""
        assert not (match and query), "Cannot use both match and query"
        sql_query = f"DELETE FROM {table} "
        if match:
            sql_query += " WHERE " + " AND ".join(f"{x} = :{x}" for x in match)
        elif query and "where" not in query.lower():
            sql_query += "WHERE " + query
        elif query:
            sql_query += query
        await self.execute(sql_query, match)
        await self._maybe_commit()

    async def delete_where_query(self, table: str, query: str | None = None) -> None:
        """Delete data in given table using given where clausule."""
        sql_query = f"DELETE FROM {table} WHERE {query}"
        await self.execute(sql_query)
        await self._maybe_commit()

    async def execute(self, query: str, values: dict[str, Any] | None = None) -> Any:
        """Execute command on the database."""
        return await self._db.execute(query, values)

    async def execute_write(self, query: str, values: dict[str, Any] | None = None) -> None:
        """
        Execute a hand-written write statement and commit it.

        Use instead of `execute` for anything that modifies data, so the write is durable
        even if nothing else happens to commit the shared connection afterwards. Honors
        `deferred_commit`, so a batch still commits once at the end of its scope.

        :param query: The statement to execute.
        :param values: The values to bind to the statement's named parameters.
        """
        await self._db.execute(query, values)
        await self._maybe_commit()

    async def commit(self) -> None:
        """Commit the current transaction."""
        return await self._db.commit()

    @asynccontextmanager
    async def deferred_commit(self) -> AsyncGenerator[None]:
        """
        Batch all writes of the current task into a single commit when the scope exits.

        Within the scope, the per-statement commit of the insert/upsert/update/delete
        helpers is skipped for the current task and a single commit is issued when the
        outermost scope exits (scopes may be nested). This greatly reduces the commit
        overhead of multi-statement operations such as adding a media item with all
        its relations to the library.

        Note: this is not an atomic transaction. The scope always commits on exit -
        also on error or cancellation - and never rolls back. Writes from other tasks
        are unaffected and still commit immediately.
        """
        depth = self._deferred_commit_depth.get()
        token = self._deferred_commit_depth.set(depth + 1)
        try:
            yield
        finally:
            self._deferred_commit_depth.reset(token)
            # always commit on exit, never rollback: the connection is shared by all
            # tasks, so statements from concurrent writers may interleave with this
            # scope's statements in the same underlying SQLite transaction and a
            # rollback would revert their (already acknowledged) writes as well
            if depth == 0:
                await self._db.commit()

    async def iter_items(
        self,
        table: str,
        match: dict[str, Any] | None = None,
    ) -> AsyncGenerator[Mapping[str, Any]]:
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

    async def get_reclaimable_ratio(self) -> float:
        """
        Return the fraction (0..1) of the database file that a VACUUM would reclaim.

        This is the share of pages on the free list and is a cheap way to decide
        whether a (potentially expensive) VACUUM is actually worthwhile.
        """
        page_count = await self._get_pragma_int("page_count")
        if page_count <= 0:
            return 0.0
        freelist_count = await self._get_pragma_int("freelist_count")
        return freelist_count / page_count

    async def vacuum(self) -> None:
        """Run vacuum command on database."""
        # VACUUM rebuilds the whole database in temp storage; with temp_store=memory that
        # copy lives entirely in RAM and OOMs memory constrained devices on large databases,
        # so spill it to a temp file (located at SQLITE_TMPDIR) for the duration.
        await self._db.execute("PRAGMA temp_store=FILE;")
        try:
            await self._db.execute("VACUUM")
            await self._db.commit()
        finally:
            await self._db.execute("PRAGMA temp_store=memory;")

    async def _get_pragma_int(self, pragma: str) -> int:
        """Return the integer value of a single-value sqlite PRAGMA."""
        async with self._db.execute(f"PRAGMA {pragma}") as cursor:
            row = await cursor.fetchone()
            return int(row[0]) if row else 0

    async def _maybe_commit(self) -> None:
        """Commit now, unless the current task is inside a deferred_commit scope."""
        if self._deferred_commit_depth.get() == 0:
            await self._db.commit()
