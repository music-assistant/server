"""Tests for the AudioAnalysisProvider base class lifecycle."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

from music_assistant.models.audio_analysis import AudioAnalysisData
from music_assistant.models.audio_analysis_provider import AudioAnalysisProvider

if TYPE_CHECKING:
    from music_assistant_models.media_items import AudioFormat
    from music_assistant_models.streamdetails import StreamDetails


class _StubProvider(AudioAnalysisProvider):
    """Minimal concrete provider for base-class tests."""

    async def _start_analysis(
        self, session_id: str, streamdetails: StreamDetails, audio_format: AudioFormat
    ) -> bool:
        return True

    async def process_pcm_chunk(self, session_id: str, pcm_chunk: bytes) -> None:
        return None

    async def _finalize(self, session_id: str) -> None:
        return None


def _make_provider() -> _StubProvider:
    mass = MagicMock()
    mass.streams.audio_analysis.get_audio_analysis_version = AsyncMock(return_value=None)
    manifest = MagicMock()
    manifest.domain = "test_stub_provider"
    config = MagicMock()
    config.get_value = MagicMock(return_value="GLOBAL")
    return _StubProvider(mass, manifest, config, supported_features=set())


@pytest.mark.asyncio
async def test_post_analysis_default_is_noop() -> None:
    """Default post_analysis must be a no-op that returns None."""
    provider = _make_provider()
    streamdetails = MagicMock()
    analysis = AudioAnalysisData()
    await provider.post_analysis(streamdetails, analysis)
