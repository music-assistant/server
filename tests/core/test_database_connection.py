"""Tests for the DatabaseConnection helper."""

import os
import pathlib
from collections.abc import AsyncGenerator
from sqlite3 import OperationalError
from typing import Any

import pytest

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


@pytest.fixture
async def db_connection(tmp_path: pathlib.Path) -> AsyncGenerator[DatabaseConnection]:
    """Return an initialized DatabaseConnection backed by a temp file."""
    db = DatabaseConnection(str(tmp_path / "test.db"))
    await db.setup()
    yield db
    await db.close()


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
