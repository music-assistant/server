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

    async def _finalize(self, session_id: str) -> AudioAnalysisData | None:
        return None


def _make_provider() -> _StubProvider:
    mass = MagicMock()
    mass.streams.audio_analysis.get_audio_analysis_version = AsyncMock(return_value=None)
    mass.streams.audio_analysis.set_audio_analysis = AsyncMock()
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


@pytest.mark.asyncio
async def test_finalize_calls_post_analysis_when_finalize_returns_analysis() -> None:
    """When _finalize returns analysis, finalize must call post_analysis with it."""
    provider = _make_provider()
    streamdetails = MagicMock()
    audio_format = MagicMock()
    analysis = AudioAnalysisData(loudness_integrated=-14.0)

    provider._finalize = AsyncMock(return_value=analysis)  # type: ignore[method-assign]
    provider.post_analysis = AsyncMock(return_value=None)  # type: ignore[method-assign]

    await provider.start_analysis("session-1", streamdetails, audio_format)
    await provider.finalize("session-1")

    provider.post_analysis.assert_awaited_once_with(streamdetails, analysis)
    assert "session-1" not in provider._sessions


@pytest.mark.asyncio
async def test_finalize_skips_post_analysis_when_finalize_returns_none() -> None:
    """When _finalize returns None, post_analysis must NOT be called."""
    provider = _make_provider()
    streamdetails = MagicMock()
    audio_format = MagicMock()

    provider._finalize = AsyncMock(return_value=None)  # type: ignore[method-assign]
    provider.post_analysis = AsyncMock(return_value=None)  # type: ignore[method-assign]

    await provider.start_analysis("session-2", streamdetails, audio_format)
    await provider.finalize("session-2")

    provider.post_analysis.assert_not_awaited()
    assert "session-2" not in provider._sessions


@pytest.mark.asyncio
async def test_finalize_swallows_finalize_exception_and_skips_post_analysis() -> None:
    """If _finalize raises, post_analysis must not be called and the exception must not propagate."""
    provider = _make_provider()
    streamdetails = MagicMock()
    audio_format = MagicMock()

    provider._finalize = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]
    provider.post_analysis = AsyncMock(return_value=None)  # type: ignore[method-assign]

    await provider.start_analysis("session-3", streamdetails, audio_format)
    await provider.finalize("session-3")

    provider.post_analysis.assert_not_awaited()
    assert "session-3" not in provider._sessions


@pytest.mark.asyncio
async def test_finalize_swallows_post_analysis_exception() -> None:
    """post_analysis raising must be caught; the analysis row stays valid."""
    provider = _make_provider()
    streamdetails = MagicMock()
    audio_format = MagicMock()
    analysis = AudioAnalysisData()

    provider._finalize = AsyncMock(return_value=analysis)  # type: ignore[method-assign]
    provider.post_analysis = AsyncMock(side_effect=RuntimeError("tag write failed"))  # type: ignore[method-assign]

    await provider.start_analysis("session-4", streamdetails, audio_format)
    # Must not raise
    await provider.finalize("session-4")

    provider.post_analysis.assert_awaited_once()
    assert "session-4" not in provider._sessions
