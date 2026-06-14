"""Tests for the DatabaseConnection helper."""

import os
import pathlib
from collections.abc import AsyncGenerator
from sqlite3 import OperationalError
from typing import Any

import pytest

from music_assistant.helpers.database import DatabaseConnection
from music_assistant.mass import MusicAssistant

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
