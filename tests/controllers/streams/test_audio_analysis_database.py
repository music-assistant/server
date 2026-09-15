"""Tests for the controller-owned audio_analysis.db (attach, schema version, relocation)."""

from __future__ import annotations

import pathlib
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from music_assistant.constants import (
    DB_TABLE_AUDIO_ANALYSIS,
    DB_TABLE_AUDIO_ANALYSIS_FAILURES,
    DB_TABLE_SETTINGS,
)
from music_assistant.controllers.streams.audio_analysis import (
    AA_DB_FILENAME,
    AA_DB_SCHEMA,
    AA_DB_SCHEMA_VERSION,
    AudioAnalysisController,
)
from music_assistant.helpers.database import DatabaseConnection

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


@pytest.fixture
async def library_db(tmp_path: pathlib.Path) -> AsyncGenerator[DatabaseConnection]:
    """Return a real on-disk library.db connection with nothing but a provider_mappings table."""
    db = DatabaseConnection(str(tmp_path / "library.db"))
    await db.setup()
    await db.execute(
        "CREATE TABLE provider_mappings("
        "provider_item_id TEXT, provider_instance TEXT, provider_domain TEXT, media_type TEXT)"
    )
    await db.commit()
    yield db
    await db.close()


def _make_controller(
    library_db: DatabaseConnection, tmp_path: pathlib.Path
) -> AudioAnalysisController:
    streams = MagicMock()
    mass = MagicMock()
    streams.mass = mass
    mass.music.database = library_db
    mass.storage_path = str(tmp_path)
    return AudioAnalysisController(streams)


async def _table_names(db: DatabaseConnection, schema: str) -> set[str]:
    rows = await db.get_rows_from_query(
        f"SELECT name FROM {schema}.sqlite_master WHERE type = 'table'", limit=0
    )
    return {r["name"] for r in rows}


@pytest.mark.asyncio
async def test_setup_database_attaches_file_and_creates_tables(
    library_db: DatabaseConnection, tmp_path: pathlib.Path
) -> None:
    """Attaching creates the db file, its tables under the aa schema, and a version row."""
    ctrl = _make_controller(library_db, tmp_path)
    await ctrl.setup_database()

    assert (tmp_path / AA_DB_FILENAME).exists()
    tables = await _table_names(library_db, AA_DB_SCHEMA)
    assert {DB_TABLE_AUDIO_ANALYSIS, DB_TABLE_AUDIO_ANALYSIS_FAILURES, DB_TABLE_SETTINGS} <= tables
    # nothing was created in the library database itself
    assert DB_TABLE_AUDIO_ANALYSIS not in await _table_names(library_db, "main")
    version = await library_db.get_row(f"{AA_DB_SCHEMA}.{DB_TABLE_SETTINGS}", {"key": "version"})
    assert version is not None
    assert int(version["value"]) == AA_DB_SCHEMA_VERSION


@pytest.mark.asyncio
async def test_setup_database_is_idempotent(
    library_db: DatabaseConnection, tmp_path: pathlib.Path
) -> None:
    """Calling setup_database twice does not re-attach or duplicate the version row."""
    ctrl = _make_controller(library_db, tmp_path)
    await ctrl.setup_database()
    await ctrl.setup_database()
    rows = await library_db.get_rows(f"{AA_DB_SCHEMA}.{DB_TABLE_SETTINGS}", {"key": "version"})
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_attached_db_uses_wal_and_normal_locking(
    library_db: DatabaseConnection, tmp_path: pathlib.Path
) -> None:
    """The attached analysis file uses WAL journaling and normal (non-exclusive) locking."""
    ctrl = _make_controller(library_db, tmp_path)
    await ctrl.setup_database()
    journal = await library_db.get_rows_from_query(f"PRAGMA {AA_DB_SCHEMA}.journal_mode", limit=0)
    assert journal[0]["journal_mode"] == "wal"
    locking = await library_db.get_rows_from_query(f"PRAGMA {AA_DB_SCHEMA}.locking_mode", limit=0)
    assert locking[0]["locking_mode"] == "normal"
