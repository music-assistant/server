"""Unit tests for the soxr resampling path in SonicAnalysisProvider (T1.2)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest
import soxr
from music_assistant_models.enums import ContentType, MediaType
from music_assistant_models.media_items import AudioFormat

import music_assistant.providers.sonic_analysis as sonic_mod
from music_assistant.providers.sonic_analysis import (
    ANALYSIS_SAMPLE_RATE,
    SonicAnalysisProvider,
    SonicSessionData,
)
from music_assistant.providers.sonic_analysis.helpers import extract_block_features as _real_extract


def _make_provider() -> SonicAnalysisProvider:
    """Construct a SonicAnalysisProvider with mocked MA infrastructure, bypassing __init__."""
    mass = MagicMock()
    mass.streams.audio_analysis.set_audio_analysis = AsyncMock()
    mass.streams.audio_analysis.get_audio_analysis_version = AsyncMock(return_value=None)
    mass.create_task = MagicMock(side_effect=lambda coro: coro.close() or MagicMock())

    manifest = MagicMock()
    manifest.domain = "sonic_analysis"

    p = SonicAnalysisProvider.__new__(SonicAnalysisProvider)
    p.logger = MagicMock()
    p.mass = mass
    p.manifest = manifest
    p._sessions = {}
    p._clap_model = MagicMock()
    p._clap_prompt_order = []
    p._clap_text_embeddings = None
    p.analysis_version = 1
    p.post_analysis = AsyncMock()  # type: ignore[method-assign]
    p.config = MagicMock()
    p.config.get_value = MagicMock(return_value="fast")
    return p


def _make_streamdetails(duration: float | None = 60.0) -> MagicMock:
    """Return a minimal streamdetails mock."""
    sd = MagicMock()
    sd.item_id = "track-1"
    sd.provider = "test_provider"
    sd.media_type = MediaType.TRACK
    sd.duration = duration
    return sd


def _make_audio_format(sample_rate: int) -> AudioFormat:
    """Return a 16-bit mono PCM AudioFormat at the given sample rate."""
    return AudioFormat(
        content_type=ContentType.PCM_S16LE,
        sample_rate=sample_rate,
        bit_depth=16,
        channels=1,
    )


async def _start_session(
    provider: SonicAnalysisProvider,
    session_id: str,
    sample_rate: int,
) -> None:
    """
    Seed _sessions with a base AnalysisSessionData then call _start_analysis.

    :param provider: The provider instance to register the session on.
    :param session_id: The session ID to register.
    :param sample_rate: PCM sample rate in Hz.
    """
    from music_assistant.models.audio_analysis_provider import AnalysisSessionData  # noqa: PLC0415

    af = _make_audio_format(sample_rate)
    sd = _make_streamdetails()
    provider._sessions[session_id] = AnalysisSessionData(streamdetails=sd, audio_format=af)
    await provider._start_analysis(session_id, sd, af)


# ---------------------------------------------------------------------------
# Test 1: no resampler at 22050 Hz
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_analysis_no_resampler_at_native_rate() -> None:
    """_start_analysis with 22050 Hz must leave resampler as None."""
    provider = _make_provider()
    await _start_session(provider, "sess-22k", 22050)
    session = provider._sessions["sess-22k"]
    assert isinstance(session, SonicSessionData)
    assert session.resampler is None


# ---------------------------------------------------------------------------
# Test 2: resampler instantiated at 44100 Hz
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_analysis_creates_resampler_at_44100() -> None:
    """_start_analysis with 44100 Hz must instantiate a soxr.ResampleStream."""
    provider = _make_provider()
    await _start_session(provider, "sess-44k", 44100)
    session = provider._sessions["sess-44k"]
    assert isinstance(session, SonicSessionData)
    assert isinstance(session.resampler, soxr.ResampleStream)


# ---------------------------------------------------------------------------
# Test 3: process_pcm_chunk passes resampled audio (22050 Hz length) to extract_block_features
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_pcm_chunk_resamples_before_feature_extraction() -> None:
    """
    process_pcm_chunk must resample 44100 Hz audio to 22050 Hz before calling extract_block_features.

    Feeds exactly one 10-second block at 44100 Hz (block_bytes = 44100*2*1*10 bytes).
    extract_block_features must be called with audio whose length is approximately
    ANALYSIS_SAMPLE_RATE * 10 samples, not 44100 * 10 samples.
    """
    provider = _make_provider()
    session_id = "sess-resample-check"
    await _start_session(provider, session_id, 44100)

    # One full 10s block at 44100 Hz, 16-bit mono
    n_samples = 44100 * 10
    audio_f32 = (np.sin(2 * np.pi * 440 * np.arange(n_samples) / 44100) * 0.5).astype(np.float32)
    pcm_bytes = (audio_f32 * 32767).astype(np.int16).tobytes()

    captured_lengths: list[int] = []

    def _capture(audio: np.ndarray, sample_rate: int) -> object:
        captured_lengths.append(len(audio))
        return _real_extract(audio, sample_rate)

    with patch.object(sonic_mod, "extract_block_features", side_effect=_capture):
        await provider.process_pcm_chunk(session_id, pcm_bytes)

    assert len(captured_lengths) == 1, (
        f"Expected exactly one extract_block_features call, got {len(captured_lengths)}"
    )
    # Allow 5% tolerance for soxr's internal delay buffering at block boundaries
    expected = ANALYSIS_SAMPLE_RATE * 10
    tolerance = int(expected * 0.05)
    assert abs(captured_lengths[0] - expected) <= tolerance, (
        f"Expected audio length ~{expected} (22050 Hz), got {captured_lengths[0]} (44100 Hz input)"
    )


# ---------------------------------------------------------------------------
# Test 4: extract_block_features assertion guard rejects non-22050 input
# ---------------------------------------------------------------------------


def test_extract_block_features_rejects_wrong_sample_rate() -> None:
    """extract_block_features must raise AssertionError when called with sample_rate != 22050."""
    audio = np.zeros(8192, dtype=np.float32)
    with pytest.raises(AssertionError):
        _real_extract(audio, 44100)
