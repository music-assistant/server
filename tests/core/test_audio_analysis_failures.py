"""Store, gate, and API tests for audio_analysis_failures against a real temp DB."""

from __future__ import annotations

import pathlib
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from music_assistant.constants import (
    DB_TABLE_AUDIO_ANALYSIS,
    DB_TABLE_AUDIO_ANALYSIS_FAILURES,
    DB_TABLE_PROVIDER_MAPPINGS,
)
from music_assistant.controllers.streams.audio_analysis import AudioAnalysisController
from music_assistant.helpers.database import DatabaseConnection
from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


@pytest.fixture
async def real_db(tmp_path: pathlib.Path) -> AsyncGenerator[DatabaseConnection, None]:
    """Create a real on-disk sqlite DB with the minimal tables the gate/store touch."""
    db = DatabaseConnection(str(tmp_path / "test.db"))
    await db.setup()
    await db.execute(
        f"CREATE TABLE {DB_TABLE_PROVIDER_MAPPINGS}("
        "provider_item_id TEXT, provider_instance TEXT, provider_domain TEXT, media_type TEXT)"
    )
    await db.execute(
        f"CREATE TABLE {DB_TABLE_AUDIO_ANALYSIS}("
        "item_id TEXT, provider TEXT, aa_provider_domain TEXT, media_type TEXT)"
    )
    await db.execute(
        f"CREATE TABLE {DB_TABLE_AUDIO_ANALYSIS_FAILURES}("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, media_type TEXT, item_id TEXT, provider TEXT, "
        "aa_provider_domain TEXT, reason TEXT, analysis_version INTEGER NOT NULL DEFAULT 1, "
        "next_retry INTEGER, "
        "timestamp_created INTEGER DEFAULT (cast(strftime('%s','now') as int)), "
        "UNIQUE(item_id,provider,aa_provider_domain,media_type))"
    )
    await db.commit()
    yield db
    await db.close()


def _make_fs_music_provider() -> MagicMock:
    """Return a fake filesystem (non-streaming) MusicProvider keyed by instance_id."""
    prov = MagicMock(spec=MusicProvider)
    prov.is_streaming_provider = False
    prov.domain = "filesystem_local"
    prov.instance_id = "filesystem_local--abc"
    prov.available = True
    return prov


def _make_controller(real_db: DatabaseConnection, music_prov: MagicMock) -> AudioAnalysisController:
    """Return an AudioAnalysisController whose mass.music.database is the real temp DB."""
    streams = MagicMock()
    mass = MagicMock()
    streams.mass = mass
    mass.music.database = real_db
    mass.get_provider = MagicMock(return_value=music_prov)
    mass.get_providers = MagicMock(return_value=[music_prov])
    return AudioAnalysisController(streams)


@pytest.mark.asyncio
async def test_table_roundtrip(real_db: DatabaseConnection) -> None:
    """A row inserted into the failures table reads back with next_retry NULL preserved."""
    await real_db.insert_or_replace(
        DB_TABLE_AUDIO_ANALYSIS_FAILURES,
        {
            "media_type": "track",
            "item_id": "t1",
            "provider": "filesystem_local--abc",
            "aa_provider_domain": "sonic_analysis",
            "reason": "boom",
            "analysis_version": 1,
            "next_retry": None,
        },
    )
    rows = await real_db.get_rows(DB_TABLE_AUDIO_ANALYSIS_FAILURES, limit=0)
    assert len(rows) == 1
    assert rows[0]["reason"] == "boom"
    assert rows[0]["next_retry"] is None
