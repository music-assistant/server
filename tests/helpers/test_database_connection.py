"""Tests for the DatabaseConnection helper."""

import asyncio
import logging
import os
import pathlib
import time
from collections.abc import AsyncGenerator
from sqlite3 import OperationalError
from typing import Any

import pytest

from music_assistant.helpers import database
from music_assistant.helpers.database import (
    DatabaseConnection,
    get_sqlite_memory_settings,
    query_params,
)
from music_assistant.mass import MusicAssistant

GIB = 1024**3

# PRAGMA temp_store integer values (sqlite docs)
TEMP_STORE_FILE = 1
TEMP_STORE_MEMORY = 2

# keeps sqlite busy for well over the threshold the slow query tests set, without touching a table
_SLOW_QUERY = (
    "WITH RECURSIVE cnt(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM cnt WHERE x < 2000000) "
    "SELECT count(*) FROM cnt"
)


@pytest.fixture
async def db_connection(tmp_path: pathlib.Path) -> AsyncGenerator[DatabaseConnection]:
    """Return an initialized DatabaseConnection backed by a temp file."""
    db = DatabaseConnection(str(tmp_path / "test.db"))
    await db.setup()
    yield db
    await db.close()


@pytest.fixture
async def debug_db(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[DatabaseConnection]:
    """Return an initialized DatabaseConnection with slow query logging enabled."""
    monkeypatch.setattr(database, "ENABLE_DEBUG", True)
    # a fresh tracker per test so neither its sampler task nor its totals outlive this loop
    monkeypatch.setattr(database, "_loop_stalls", database._LoopStallTracker())
    db = DatabaseConnection(str(tmp_path / "debug.db"))
    await db.setup()
    yield db
    await db.close()


@pytest.fixture
async def db_with_table(db_connection: DatabaseConnection) -> DatabaseConnection:
    """Return an initialized DatabaseConnection with a simple test table."""
    await db_connection.execute(
        "CREATE TABLE items(id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "name TEXT NOT NULL UNIQUE, url TEXT, plays INTEGER)"
    )
    await db_connection.commit()
    return db_connection


def _count_commits(db: DatabaseConnection) -> list[None]:
    """Wrap the raw connection commit so every commit appends to the returned list."""
    calls: list[None] = []
    original_commit = db._db.commit

    async def counting_commit() -> None:
        calls.append(None)
        await original_commit()

    db._db.commit = counting_commit  # type: ignore[method-assign]
    return calls


async def _get_temp_store(db: DatabaseConnection) -> int:
    async with db._db.execute("PRAGMA temp_store") as cursor:
        row = await cursor.fetchone()
    assert row is not None
    return int(row[0])


async def _read_pragma_int(db: DatabaseConnection, pragma: str) -> int:
    async with db._db.execute(f"PRAGMA {pragma}") as cursor:
        row = await cursor.fetchone()
    assert row is not None
    return int(row[0])


async def test_vacuum_spills_temp_storage_to_disk(db_connection: DatabaseConnection) -> None:
    """Test that vacuum runs with temp_store=FILE and restores temp_store=memory after."""
    executed: list[str] = []
    original_execute = db_connection._db.execute

    def record(sql: str, *args: Any, **kwargs: Any) -> Any:
        executed.append(sql)
        return original_execute(sql, *args, **kwargs)

    db_connection._db.execute = record  # type: ignore[method-assign]
    await db_connection.vacuum()
    db_connection._db.execute = original_execute  # type: ignore[method-assign]

    vacuum_idx = executed.index("VACUUM")
    assert any("temp_store=FILE" in sql for sql in executed[:vacuum_idx])
    assert any("temp_store=memory" in sql for sql in executed[vacuum_idx:])
    assert await _get_temp_store(db_connection) == TEMP_STORE_MEMORY


async def test_vacuum_restores_temp_store_on_failure(
    db_connection: DatabaseConnection,
) -> None:
    """Test that temp_store is restored to memory even when the vacuum itself fails."""
    original_execute = db_connection._db.execute

    def explode(sql: str, *args: Any, **kwargs: Any) -> Any:
        if sql == "VACUUM":
            raise OperationalError("database or disk is full")
        return original_execute(sql, *args, **kwargs)

    db_connection._db.execute = explode  # type: ignore[method-assign]
    with pytest.raises(OperationalError):
        await db_connection.vacuum()
    db_connection._db.execute = original_execute  # type: ignore[method-assign]

    assert await _get_temp_store(db_connection) == TEMP_STORE_MEMORY


def test_sqlite_tmpdir_defaults_to_storage_path(tmp_path: pathlib.Path) -> None:
    """Test that SQLITE_TMPDIR is pointed at the storage path on server init."""
    original = os.environ.pop("SQLITE_TMPDIR", None)
    try:
        MusicAssistant(str(tmp_path / "data"), str(tmp_path / "cache"))
        assert os.environ.get("SQLITE_TMPDIR") == str(tmp_path / "data")
    finally:
        if original is None:
            os.environ.pop("SQLITE_TMPDIR", None)
        else:
            os.environ["SQLITE_TMPDIR"] = original


def test_sqlite_tmpdir_respects_existing_value(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that a user-provided SQLITE_TMPDIR is not overwritten."""
    monkeypatch.setenv("SQLITE_TMPDIR", "/custom/tmp")
    MusicAssistant(str(tmp_path / "data"), str(tmp_path / "cache"))
    assert os.environ["SQLITE_TMPDIR"] == "/custom/tmp"


@pytest.mark.parametrize(
    ("total_ram_gb", "expected_cache_kib", "expected_mmap_bytes"),
    [
        (0.0, 64000, 2 * GIB),  # unknown -> fail open to the generous "capable" tier
        (1.0, 16000, 256 * 1024 * 1024),
        (2.0, 32000, GIB),
        (4.0, 64000, 2 * GIB),  # capable: unchanged from the previous 64MB cache
        (8.0, 128000, 2 * GIB),  # plenty of RAM: larger cache for performance
        (12.0, 512000, 2 * GIB),  # large host: keep a big library hot
        (16.0, 1024000, 2 * GIB),  # very large host
        (32.0, 1024000, 2 * GIB),
    ],
)
def test_sqlite_memory_settings_scale_with_ram(
    monkeypatch: pytest.MonkeyPatch,
    total_ram_gb: float,
    expected_cache_kib: int,
    expected_mmap_bytes: int,
) -> None:
    """Test that the SQLite cache/mmap ceilings scale with available system memory."""
    monkeypatch.setattr(
        "music_assistant.helpers.util.get_total_system_memory", lambda: total_ram_gb
    )
    assert get_sqlite_memory_settings() == (expected_cache_kib, expected_mmap_bytes)


async def test_setup_applies_ram_scaled_pragmas(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that setup() applies the RAM-scaled cache_size/mmap_size pragmas by default."""
    # use the 2-4GB tier so the 1GiB mmap stays below SQLite's ~2GiB build cap and reads back
    monkeypatch.setattr("music_assistant.helpers.util.get_total_system_memory", lambda: 2.0)
    db = DatabaseConnection(str(tmp_path / "scaled.db"))
    await db.setup()
    try:
        assert await _read_pragma_int(db, "cache_size") == -32000
        assert await _read_pragma_int(db, "mmap_size") == GIB
    finally:
        await db.close()


async def test_setup_clamps_pragma_values(tmp_path: pathlib.Path) -> None:
    """Test that setup() clamps explicit cache/mmap values to non-negative integers."""
    db = DatabaseConnection(str(tmp_path / "clamped.db"))
    await db.setup(cache_size_kib=-50, mmap_size_bytes=-1)
    try:
        assert await _read_pragma_int(db, "cache_size") == 0
        assert await _read_pragma_int(db, "mmap_size") == 0
    finally:
        await db.close()


async def test_deferred_commit_batches_writes_into_single_commit(
    db_with_table: DatabaseConnection,
) -> None:
    """Test that writes within a deferred_commit scope result in one commit at scope exit."""
    commits = _count_commits(db_with_table)
    async with db_with_table.deferred_commit():
        for i in range(10):
            await db_with_table.insert("items", {"name": f"item{i}"})
        await db_with_table.update("items", {"name": "item0"}, {"url": "http://test"})
        await db_with_table.delete("items", {"name": "item9"})
        assert len(commits) == 0
    assert len(commits) == 1
    assert len(await db_with_table.get_rows("items")) == 9


async def test_deferred_commit_nested_scopes_commit_once(
    db_with_table: DatabaseConnection,
) -> None:
    """Test that nested deferred_commit scopes only commit when the outermost scope exits."""
    commits = _count_commits(db_with_table)
    async with db_with_table.deferred_commit():
        await db_with_table.insert("items", {"name": "outer"})
        async with db_with_table.deferred_commit():
            await db_with_table.insert("items", {"name": "inner"})
        assert len(commits) == 0
    assert len(commits) == 1
    assert len(await db_with_table.get_rows("items")) == 2


async def test_deferred_commit_commits_pending_writes_on_error(
    db_with_table: DatabaseConnection,
) -> None:
    """Test that a deferred_commit scope commits (not rolls back) already executed writes."""

    async def write_and_fail() -> None:
        async with db_with_table.deferred_commit():
            await db_with_table.insert("items", {"name": "kept"})
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await write_and_fail()
    assert await db_with_table.get_row("items", {"name": "kept"}) is not None


async def test_deferred_commit_commits_on_cancellation(
    db_with_table: DatabaseConnection,
) -> None:
    """Test that cancelling a task inside a scope commits its writes and keeps the db usable."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def writer() -> None:
        async with db_with_table.deferred_commit():
            await db_with_table.insert("items", {"name": "written-before-cancel"})
            started.set()
            await release.wait()

    task = asyncio.get_running_loop().create_task(writer())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not db_with_table._db.in_transaction
    assert await db_with_table.get_row("items", {"name": "written-before-cancel"}) is not None
    # the connection remains fully usable for subsequent writes
    await db_with_table.insert("items", {"name": "after-cancel"})
    assert await db_with_table.get_row("items", {"name": "after-cancel"}) is not None


async def test_deferred_commit_scope_only_defers_own_task(
    db_with_table: DatabaseConnection,
) -> None:
    """Test that writes from tasks outside a scope still commit immediately."""
    commits = _count_commits(db_with_table)
    in_scope = asyncio.Event()
    release = asyncio.Event()

    async def scoped_writer() -> None:
        async with db_with_table.deferred_commit():
            await db_with_table.insert("items", {"name": "scoped"})
            in_scope.set()
            await release.wait()

    task = asyncio.get_running_loop().create_task(scoped_writer())
    await in_scope.wait()
    # this write happens on the test task (no scope) while the writer's scope is open
    commits_before = len(commits)
    await db_with_table.insert("items", {"name": "plain"})
    assert len(commits) == commits_before + 1
    release.set()
    await task
    assert len(await db_with_table.get_rows("items")) == 2


async def test_deferred_commit_concurrent_scopes_do_not_interfere(
    db_with_table: DatabaseConnection,
) -> None:
    """Test that concurrent tasks (e.g. parallel syncs) each batch their own writes."""
    commits = _count_commits(db_with_table)
    barrier = asyncio.Barrier(2)

    async def sync_task(name: str) -> None:
        async with db_with_table.deferred_commit():
            await db_with_table.insert("items", {"name": f"{name}-1"})
            # force both scopes to be open at the same time
            await barrier.wait()
            await db_with_table.insert("items", {"name": f"{name}-2"})

    async with asyncio.TaskGroup() as tg:
        tg.create_task(sync_task("a"))
        tg.create_task(sync_task("b"))

    # all writes from both tasks landed, with one commit per scope exit
    assert len(await db_with_table.get_rows("items")) == 4
    assert len(commits) == 2


async def test_update_returns_none(db_with_table: DatabaseConnection) -> None:
    """Test that update() no longer fetches and returns the updated row."""
    row_id = await db_with_table.insert("items", {"name": "old"})
    result = await db_with_table.update("items", {"id": row_id}, {"name": "new"})  # type: ignore[func-returns-value]
    assert result is None
    row = await db_with_table.get_row("items", {"id": row_id})
    assert row is not None
    assert row["name"] == "new"


async def test_upsert_many(db_with_table: DatabaseConnection) -> None:
    """Test that upsert_many inserts/updates multiple rows with a single commit."""
    commits = _count_commits(db_with_table)
    # rows with different column sets are handled in a single call
    await db_with_table.upsert_many(
        "items",
        [
            {"name": "a", "url": "http://a", "plays": 1},
            {"name": "b", "plays": 2},
            {"name": "c", "plays": 3},
        ],
    )
    assert len(commits) == 1
    rows = {row["name"]: row for row in await db_with_table.get_rows("items")}
    assert len(rows) == 3
    assert rows["a"]["url"] == "http://a"
    # upserting again updates on conflict; omitted columns keep their existing value
    await db_with_table.upsert_many("items", [{"name": "a", "plays": 10}])
    row = await db_with_table.get_row("items", {"name": "a"})
    assert row is not None
    assert row["plays"] == 10
    assert row["url"] == "http://a"


async def test_upsert_many_empty_is_noop(db_with_table: DatabaseConnection) -> None:
    """Test that upsert_many with no rows does nothing."""
    commits = _count_commits(db_with_table)
    await db_with_table.upsert_many("items", [])
    assert len(commits) == 0


def test_query_params_expands_list_values() -> None:
    """Test that list params are expanded into placeholders in all placeholder notations."""
    query, params = query_params(
        "SELECT * FROM items WHERE id IN :ids AND name = :name",
        {"ids": [1, 2], "name": "foo"},
    )
    assert query == "SELECT * FROM items WHERE id IN (:_param_0,:_param_1) AND name = :name"
    assert params == {"_param_0": 1, "_param_1": 2, "name": "foo"}
    # placeholder already wrapped in parens must not end up double-wrapped
    query, params = query_params("SELECT * FROM items WHERE id IN(:ids)", {"ids": [1, 2]})
    assert query == "SELECT * FROM items WHERE id IN(:_param_0,:_param_1)"
    assert params == {"_param_0": 1, "_param_1": 2}


def test_query_params_leaves_prefixed_placeholders_untouched() -> None:
    """Test that expanding a list param does not corrupt placeholders sharing its prefix."""
    query, params = query_params(
        "SELECT * FROM items WHERE id IN :ids AND other = :ids_extra",
        {"ids": [1], "ids_extra": 2},
    )
    assert query == "SELECT * FROM items WHERE id IN (:_param_0) AND other = :ids_extra"
    assert params == {"_param_0": 1, "ids_extra": 2}


async def test_slow_query_warning_ignores_event_loop_stalls(
    debug_db: DatabaseConnection, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Test that a query awaited across a blocked event loop is not reported as slow."""
    monkeypatch.setattr(database, "SLOW_QUERY_THRESHOLD", 0.2)
    with caplog.at_level(logging.WARNING, logger="music_assistant.database"):
        query = asyncio.create_task(debug_db.get_rows_from_query("SELECT 1", limit=0))
        # let the statement reach the connection thread, then hog the loop so its result
        # cannot be delivered - exactly what a CPU-bound callback elsewhere would do
        await asyncio.sleep(0)
        time.sleep(0.5)  # noqa: ASYNC251  # blocking the loop is what is under test here
        await query
    assert "SQL Query took" not in caplog.text


async def test_slow_query_warning_still_reports_a_slow_query(
    debug_db: DatabaseConnection, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Test that a query which genuinely keeps sqlite busy is still reported as slow."""
    monkeypatch.setattr(database, "SLOW_QUERY_THRESHOLD", 0.02)
    with caplog.at_level(logging.WARNING, logger="music_assistant.database"):
        await debug_db.get_rows_from_query(_SLOW_QUERY, limit=0)
    assert "SQL Query took" in caplog.text


async def test_slow_query_warning_survives_a_stall_that_precedes_it(
    debug_db: DatabaseConnection, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Test that a stall ending before a query starts is not discounted from that query."""
    monkeypatch.setattr(database, "SLOW_QUERY_THRESHOLD", 0.02)
    with caplog.at_level(logging.WARNING, logger="music_assistant.database"):
        # the sampler only books this stall once it next wakes, which is after the query below
        # has already started and taken its own reading
        time.sleep(0.5)  # noqa: ASYNC251  # stalling the loop is what is under test here
        await debug_db.get_rows_from_query(_SLOW_QUERY, limit=0)
    assert "SQL Query took" in caplog.text
