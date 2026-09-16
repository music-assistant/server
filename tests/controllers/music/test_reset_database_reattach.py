"""Tests that a library reset keeps the audio analysis database attached and intact."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from music_assistant_models.enums import MediaType

from music_assistant.controllers.streams.audio_analysis import AA_DB_SCHEMA, AA_TABLE_ANALYSIS
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
