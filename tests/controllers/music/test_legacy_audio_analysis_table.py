"""Tests for the legacy audio_analysis table migration helper."""

from __future__ import annotations

import pathlib
from typing import TYPE_CHECKING

import pytest

from music_assistant.constants import DB_TABLE_AUDIO_ANALYSIS
from music_assistant.controllers.music.migrations import ensure_legacy_audio_analysis_table
from music_assistant.helpers.database import DatabaseConnection

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


@pytest.fixture
async def db(tmp_path: pathlib.Path) -> AsyncGenerator[DatabaseConnection]:
    """Return a DatabaseConnection backed by a temp-path library.db."""
    conn = DatabaseConnection(str(tmp_path / "library.db"))
    await conn.setup()
    yield conn
    await conn.close()


@pytest.mark.asyncio
async def test_creates_legacy_table_with_expected_columns(db: DatabaseConnection) -> None:
    """The helper must create the legacy table so the v38 migration can insert into it."""
    await ensure_legacy_audio_analysis_table(db)
    await ensure_legacy_audio_analysis_table(db)  # idempotent
    cols = await db.get_rows_from_query(
        f"PRAGMA main.table_info({DB_TABLE_AUDIO_ANALYSIS})", limit=0
    )
    assert [c["name"] for c in cols] == [
        "id",
        "media_type",
        "item_id",
        "provider",
        "aa_provider_domain",
        "analysis_data",
        "analysis_version",
        "timestamp_created",
    ]
