"""Tests for the library migration step that moves audio analysis into audio_analysis.db."""

from __future__ import annotations

import logging
import pathlib
import sqlite3
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from music_assistant.constants import (
    DB_TABLE_AUDIO_ANALYSIS,
    DB_TABLE_AUDIO_ANALYSIS_FAILURES,
    DB_TABLE_SETTINGS,
)
from music_assistant.controllers.music import migrations
from music_assistant.controllers.music.migrations import migrate_database
from music_assistant.controllers.streams.audio_analysis_database import create_analysis_tables
from music_assistant.controllers.streams.constants import AA_DB_FILENAME, AA_DB_SCHEMA_VERSION
from music_assistant.helpers.database import DatabaseConnection

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

LOGGER = logging.getLogger("test_audio_analysis_move_migration")

LEGACY_ANALYSIS_DDL = (
    f"CREATE TABLE {DB_TABLE_AUDIO_ANALYSIS}("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, media_type TEXT NOT NULL, item_id TEXT NOT NULL, "
    "provider TEXT NOT NULL, aa_provider_domain TEXT NOT NULL, analysis_data json NOT NULL, "
    "analysis_version INTEGER DEFAULT 1, "
    "timestamp_created INTEGER DEFAULT (cast(strftime('%s','now') as int)), "
    "UNIQUE(item_id,provider,aa_provider_domain,media_type))"
)
LEGACY_FAILURES_DDL = (
    f"CREATE TABLE {DB_TABLE_AUDIO_ANALYSIS_FAILURES}("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, media_type TEXT NOT NULL, item_id TEXT NOT NULL, "
    "provider TEXT NOT NULL, aa_provider_domain TEXT NOT NULL, reason TEXT NOT NULL, "
    "analysis_version INTEGER NOT NULL DEFAULT 1, next_retry INTEGER, "
    "timestamp_created INTEGER DEFAULT (cast(strftime('%s','now') as int)), "
    "UNIQUE(item_id,provider,aa_provider_domain,media_type))"
)


@pytest.fixture
async def library_db(tmp_path: pathlib.Path) -> AsyncGenerator[DatabaseConnection]:
    """Return a real on-disk library.db connection."""
    db = DatabaseConnection(str(tmp_path / "library.db"))
    await db.setup()
    yield db
    await db.close()


@pytest.fixture
def mass(tmp_path: pathlib.Path) -> MagicMock:
    """Return a stand-in MusicAssistant whose storage path is the test directory."""
    mass = MagicMock()
    mass.storage_path = str(tmp_path)
    mass.cache.clear = AsyncMock()
    return mass


async def _seed_legacy(db: DatabaseConnection, n_analysis: int, n_failures: int) -> None:
    await db.execute(LEGACY_ANALYSIS_DDL)
    await db.execute(LEGACY_FAILURES_DDL)
    for i in range(n_analysis):
        # explicit ids with a gap, and explicit timestamps, so preservation is observable
        await db.execute(
            f"INSERT INTO {DB_TABLE_AUDIO_ANALYSIS}"
            "(id, media_type, item_id, provider, aa_provider_domain, analysis_data, "
            " analysis_version, timestamp_created) VALUES "
            "(:id, 'track', :item, 'fs--a', 'loudness_analysis', :data, 2, :ts)",
            {
                "id": i * 3 + 1,
                "item": f"t{i}",
                "data": f'{{"loudness_integrated": {-i}}}',
                "ts": 1000 + i,
            },
        )
    for i in range(n_failures):
        await db.execute(
            f"INSERT INTO {DB_TABLE_AUDIO_ANALYSIS_FAILURES}"
            "(media_type, item_id, provider, aa_provider_domain, reason, analysis_version, next_retry)"
            " VALUES ('track', :item, 'fs--a', 'sonic_analysis', 'boom', 1, NULL)",
            {"item": f"f{i}"},
        )
    await db.commit()


async def _main_tables(db: DatabaseConnection) -> set[str]:
    rows = await db.get_rows_from_query(
        "SELECT name FROM main.sqlite_master WHERE type = 'table'", limit=0
    )
    return {r["name"] for r in rows}


async def _analysis_rows(tmp_path: pathlib.Path, table: str) -> list[dict[str, Any]]:
    """Read every row of a table in audio_analysis.db through a connection of its own."""
    db = DatabaseConnection(str(tmp_path / AA_DB_FILENAME))
    await db.setup()
    try:
        return [dict(row) for row in await db.get_rows(table, order_by="id", limit=0)]
    finally:
        await db.close()


async def _seed_analysis_file(tmp_path: pathlib.Path, table: str, row: dict[str, Any]) -> None:
    """Create audio_analysis.db with its current tables and one row in the given table."""
    db = DatabaseConnection(str(tmp_path / AA_DB_FILENAME))
    await db.setup()
    try:
        await create_analysis_tables(db)
        await db.insert(table, row)
        await db.commit()
    finally:
        await db.close()


async def _attached(db: DatabaseConnection) -> set[str]:
    return {r["name"] for r in await db.get_rows_from_query("PRAGMA database_list", limit=0)}


@pytest.mark.asyncio
async def test_moves_legacy_rows_and_drops_legacy_tables(
    library_db: DatabaseConnection, mass: MagicMock, tmp_path: pathlib.Path
) -> None:
    """Legacy rows land in audio_analysis.db with fresh ids and timestamps preserved."""
    await _seed_legacy(library_db, n_analysis=4, n_failures=2)

    await migrations._move_audio_analysis_out(mass, library_db, LOGGER)

    moved = await _analysis_rows(tmp_path, DB_TABLE_AUDIO_ANALYSIS)
    assert [r["id"] for r in moved] == [1, 2, 3, 4]  # fresh ids, not the legacy ones
    assert [r["item_id"] for r in moved] == ["t0", "t1", "t2", "t3"]
    assert [r["timestamp_created"] for r in moved] == [1000, 1001, 1002, 1003]
    assert moved[2]["analysis_data"] == '{"loudness_integrated": -2}'
    assert moved[2]["analysis_version"] == 2
    failures = await _analysis_rows(tmp_path, DB_TABLE_AUDIO_ANALYSIS_FAILURES)
    assert {r["item_id"] for r in failures} == {"f0", "f1"}
    assert failures[0]["next_retry"] is None
    main_tables = await _main_tables(library_db)
    assert DB_TABLE_AUDIO_ANALYSIS not in main_tables
    assert DB_TABLE_AUDIO_ANALYSIS_FAILURES not in main_tables
    assert "aa" not in await _attached(library_db)


@pytest.mark.asyncio
async def test_migrate_database_moves_analysis_from_version_63(
    library_db: DatabaseConnection, mass: MagicMock, tmp_path: pathlib.Path
) -> None:
    """The library migration from schema 63 runs the move."""
    await _seed_legacy(library_db, n_analysis=1, n_failures=0)

    await migrate_database(mass, library_db, LOGGER, prev_version=63, create_tables=AsyncMock())

    assert DB_TABLE_AUDIO_ANALYSIS not in await _main_tables(library_db)
    assert [r["item_id"] for r in await _analysis_rows(tmp_path, DB_TABLE_AUDIO_ANALYSIS)] == ["t0"]


@pytest.mark.asyncio
async def test_no_legacy_tables_leaves_no_analysis_file(
    library_db: DatabaseConnection, mass: MagicMock, tmp_path: pathlib.Path
) -> None:
    """Without legacy tables there is nothing to move and no file is created."""
    await migrations._move_audio_analysis_out(mass, library_db, LOGGER)
    assert not (tmp_path / AA_DB_FILENAME).exists()


@pytest.mark.asyncio
async def test_move_walks_id_ranges_in_batches(
    library_db: DatabaseConnection,
    mass: MagicMock,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The move completes across multiple batches, not just the first one."""
    monkeypatch.setattr(migrations, "AUDIO_ANALYSIS_MOVE_BATCH_SIZE", 4)
    await _seed_legacy(library_db, n_analysis=7, n_failures=0)
    await migrations._move_audio_analysis_out(mass, library_db, LOGGER)
    assert len(await _analysis_rows(tmp_path, DB_TABLE_AUDIO_ANALYSIS)) == 7


@pytest.mark.asyncio
async def test_move_batches_follow_row_count_not_max_id(
    library_db: DatabaseConnection,
    mass: MagicMock,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sparse legacy ids move in ceil(rows / batch) batches, however high MAX(id) is."""
    monkeypatch.setattr(migrations, "AUDIO_ANALYSIS_MOVE_BATCH_SIZE", 2)
    await _seed_legacy(library_db, n_analysis=0, n_failures=0)
    for i, legacy_id in enumerate((5, 20, 47, 80, 101)):
        await library_db.execute(
            f"INSERT INTO {DB_TABLE_AUDIO_ANALYSIS}"
            "(id, media_type, item_id, provider, aa_provider_domain, analysis_data, "
            " analysis_version, timestamp_created) VALUES "
            "(:id, 'track', :item, 'fs--a', 'loudness_analysis', '{}', 2, 1000)",
            {"id": legacy_id, "item": f"t{i}"},
        )
    await library_db.commit()
    real_execute = library_db.execute
    batch_inserts: list[str] = []

    async def counting_execute(query: str, values: dict[str, Any] | None = None) -> Any:
        if query.startswith(f"INSERT INTO aa.{DB_TABLE_AUDIO_ANALYSIS} "):
            batch_inserts.append(query)
        return await real_execute(query, values)

    monkeypatch.setattr(library_db, "execute", counting_execute)
    await migrations._move_audio_analysis_out(mass, library_db, LOGGER)

    moved = await _analysis_rows(tmp_path, DB_TABLE_AUDIO_ANALYSIS)
    assert {r["item_id"] for r in moved} == {"t0", "t1", "t2", "t3", "t4"}
    assert len(batch_inserts) == 3
    assert DB_TABLE_AUDIO_ANALYSIS not in await _main_tables(library_db)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failed_table", [DB_TABLE_AUDIO_ANALYSIS, DB_TABLE_AUDIO_ANALYSIS_FAILURES]
)
async def test_move_failure_keeps_legacy_table_without_raising(
    library_db: DatabaseConnection,
    mass: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failed_table: str,
) -> None:
    """A mid-copy failure logs at ERROR, keeps that legacy table and detaches the file."""
    await _seed_legacy(library_db, n_analysis=2, n_failures=2)
    real_execute = library_db.execute

    async def failing_execute(query: str, values: dict[str, Any] | None = None) -> Any:
        if query.startswith(f"INSERT INTO aa.{failed_table} "):
            raise sqlite3.OperationalError("disk I/O error")
        return await real_execute(query, values)

    monkeypatch.setattr(library_db, "execute", failing_execute)
    with caplog.at_level(logging.ERROR):
        await migrations._move_audio_analysis_out(mass, library_db, LOGGER)

    assert len(await library_db.get_rows(f"main.{failed_table}")) == 2
    assert "disk I/O error" in caplog.text
    assert "aa" not in await _attached(library_db)


@pytest.mark.asyncio
async def test_unopenable_analysis_file_keeps_legacy_tables(
    library_db: DatabaseConnection, mass: MagicMock, tmp_path: pathlib.Path
) -> None:
    """A storage path the analysis file cannot be created in leaves library.db as it was."""
    await _seed_legacy(library_db, n_analysis=1, n_failures=1)
    mass.storage_path = str(tmp_path / "missing" / "dir")

    await migrations._move_audio_analysis_out(mass, library_db, LOGGER)

    main_tables = await _main_tables(library_db)
    assert {DB_TABLE_AUDIO_ANALYSIS, DB_TABLE_AUDIO_ANALYSIS_FAILURES} <= main_tables


@pytest.mark.asyncio
async def test_newer_analysis_schema_keeps_legacy_tables(
    library_db: DatabaseConnection, mass: MagicMock, tmp_path: pathlib.Path
) -> None:
    """An analysis file from a newer build is never written; the legacy rows stay put."""
    with sqlite3.connect(tmp_path / AA_DB_FILENAME) as db:
        db.execute(f"CREATE TABLE {DB_TABLE_SETTINGS}(key TEXT PRIMARY KEY, value TEXT, type TEXT)")
        db.execute(
            f"INSERT INTO {DB_TABLE_SETTINGS} VALUES ('version', ?, 'str')",
            (str(AA_DB_SCHEMA_VERSION + 1),),
        )
    await _seed_legacy(library_db, n_analysis=1, n_failures=0)

    await migrations._move_audio_analysis_out(mass, library_db, LOGGER)

    assert DB_TABLE_AUDIO_ANALYSIS in await _main_tables(library_db)
    with sqlite3.connect(tmp_path / AA_DB_FILENAME) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == {DB_TABLE_SETTINGS}


@pytest.mark.asyncio
@pytest.mark.parametrize("table", [DB_TABLE_AUDIO_ANALYSIS, DB_TABLE_AUDIO_ANALYSIS_FAILURES])
@pytest.mark.parametrize("source_timestamp", [900, 1000, 1100])
async def test_move_keeps_newest_same_key_record(
    library_db: DatabaseConnection,
    mass: MagicMock,
    tmp_path: pathlib.Path,
    table: str,
    source_timestamp: int,
) -> None:
    """Newer legacy rows win, while equal or newer rows already in the file survive."""
    await _seed_legacy(library_db, n_analysis=1, n_failures=1)
    await library_db.execute(
        f"UPDATE main.{table} SET timestamp_created = :timestamp",
        {"timestamp": source_timestamp},
    )
    await library_db.commit()
    source = dict((await library_db.get_rows(f"main.{table}"))[0])
    destination = {**source, "id": 42, "timestamp_created": 1000, "analysis_version": 9}
    if table == DB_TABLE_AUDIO_ANALYSIS:
        destination["analysis_data"] = '{"loudness_integrated": -15.0}'
    else:
        destination["reason"] = "destination failure"
        destination["next_retry"] = 2000
    await _seed_analysis_file(tmp_path, table, destination)

    await migrations._move_audio_analysis_out(mass, library_db, LOGGER)

    expected = source if source_timestamp > 1000 else destination
    assert await _analysis_rows(tmp_path, table) == [{**expected, "id": 42}]
    assert table not in await _main_tables(library_db)


@pytest.mark.asyncio
async def test_unparsable_analysis_schema_version_keeps_legacy_tables(
    library_db: DatabaseConnection, mass: MagicMock, tmp_path: pathlib.Path
) -> None:
    """A version value that is no number never escapes the step; the legacy rows stay."""
    with sqlite3.connect(tmp_path / AA_DB_FILENAME) as db:
        db.execute(f"CREATE TABLE {DB_TABLE_SETTINGS}(key TEXT PRIMARY KEY, value TEXT, type TEXT)")
        db.execute(f"INSERT INTO {DB_TABLE_SETTINGS} VALUES ('version', 'garbage', 'str')")
    await _seed_legacy(library_db, n_analysis=1, n_failures=0)

    await migrations._move_audio_analysis_out(mass, library_db, LOGGER)

    assert DB_TABLE_AUDIO_ANALYSIS in await _main_tables(library_db)
    assert "aa" not in await _attached(library_db)
