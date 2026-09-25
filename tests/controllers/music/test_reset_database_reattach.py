"""Tests that a library reset keeps the audio analysis database attached and intact."""

from __future__ import annotations

import sqlite3
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import ProviderUnavailableError

from music_assistant.constants import DB_TABLE_AUDIO_ANALYSIS_FAILURES
from music_assistant.controllers.streams.constants import (
    AA_DB_SCHEMA,
    AA_TABLE_ANALYSIS,
    AA_TABLE_FAILURES,
)
from music_assistant.mass import MusicAssistant


async def test_reset_database_keeps_analysis_rows(mass: MusicAssistant) -> None:
    """Resetting the library re-attaches the analysis database and keeps its rows."""
    await mass.music.database.insert(
        AA_TABLE_ANALYSIS,
        {
            "media_type": MediaType.TRACK.value,
            "item_id": "fs-kept",
            "provider": "filesystem_local--AbCd",
            "aa_provider_domain": "loudness_analysis",
            "analysis_data": "{}",
            "analysis_version": 1,
        },
    )

    with patch.object(mass.music, "start_sync", AsyncMock()):
        await mass.music._reset_database()

    attached = await mass.music.database.get_rows_from_query("PRAGMA database_list", limit=0)
    assert AA_DB_SCHEMA in {row["name"] for row in attached}
    rows = await mass.music.database.get_rows(AA_TABLE_ANALYSIS, {"item_id": "fs-kept"})
    assert len(rows) == 1


async def test_reset_rejects_incomplete_analysis_relocation(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed relocation must complete before reset can delete its remaining source."""
    db = mass.music.database
    source = f"main.{DB_TABLE_AUDIO_ANALYSIS_FAILURES}"
    await db.execute(f"CREATE TABLE {source} AS SELECT * FROM {AA_TABLE_FAILURES}")
    await db.insert(
        source,
        {
            "id": 1,
            "media_type": MediaType.TRACK.value,
            "item_id": "fs-unmigrated",
            "provider": "filesystem_local--AbCd",
            "aa_provider_domain": "loudness_analysis",
            "reason": "never retry",
            "analysis_version": 1,
            "timestamp_created": 1,
        },
    )
    real_execute = db.execute

    async def failing_execute(query: str, values: dict[str, Any] | None = None) -> Any:
        if query.upper().startswith(f"INSERT INTO {AA_TABLE_FAILURES.upper()}"):
            raise sqlite3.OperationalError("disk I/O error")
        return await real_execute(query, values)

    with monkeypatch.context() as failing:
        failing.setattr(db, "execute", failing_execute)
        await mass.streams.audio_analysis.setup_database()
        assert not mass.streams.audio_analysis.database_ready
        with patch.object(mass.music, "close", AsyncMock(wraps=mass.music.close)) as close:
            with pytest.raises(ProviderUnavailableError, match="Cannot reset the library"):
                await mass.music._reset_database()
            close.assert_not_awaited()
        assert mass.music.database is db
        assert await db.get_row(source, {"item_id": "fs-unmigrated"})

    await mass.streams.audio_analysis.setup_database()
    with patch.object(mass.music, "start_sync", AsyncMock()):
        await mass.music._reset_database()

    row = await mass.music.database.get_row(AA_TABLE_FAILURES, {"item_id": "fs-unmigrated"})
    assert row is not None
    assert row["reason"] == "never retry"
