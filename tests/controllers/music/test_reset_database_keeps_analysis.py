"""Tests that a library reset leaves the audio analysis database untouched."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from music_assistant_models.enums import MediaType

from music_assistant.controllers.streams.constants import AA_TABLE_ANALYSIS
from music_assistant.mass import MusicAssistant


async def test_reset_database_keeps_analysis_rows(mass: MusicAssistant) -> None:
    """Resetting the library keeps the analysis database usable and its rows in place."""
    analysis = mass.streams.audio_analysis
    await analysis.database.insert(
        AA_TABLE_ANALYSIS,
        {
            "media_type": MediaType.TRACK.value,
            "item_id": "fs-kept",
            "provider": "filesystem_local--AbCd",
            "aa_provider_domain": "loudness_analysis",
            "header": "{}",
            "payload": b"",
            "analysis_version": 1,
        },
    )

    with patch.object(mass.music, "start_sync", AsyncMock()):
        await mass.music._reset_database()

    assert analysis.database_ready
    rows = await analysis.database.get_rows(AA_TABLE_ANALYSIS, {"item_id": "fs-kept"})
    assert len(rows) == 1
