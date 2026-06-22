"""Tests for the _decode_resample_extract helper and its asyncio.to_thread offload (T2.3)."""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest
from music_assistant_models.enums import ContentType, MediaType
from music_assistant_models.media_items import AudioFormat

import music_assistant.providers.sonic_analysis as sonic_mod
from music_assistant.providers.sonic_analysis import (
    ANALYSIS_SAMPLE_RATE,
    SonicAnalysisProvider,
)

# ---------------------------------------------------------------------------
# Helpers shared across tests
# ---------------------------------------------------------------------------


def _make_provider() -> SonicAnalysisProvider:
    """Construct a SonicAnalysisProvider with mocked MA infrastructure."""
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


def _make_audio_format(sample_rate: int = 22050) -> AudioFormat:
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
    sample_rate: int = 22050,
) -> None:
    """
    Seed _sessions and call _start_analysis.

    :param provider: The provider instance to register the session on.
    :param session_id: The session ID to register.
    :param sample_rate: PCM sample rate in Hz.
    """
    from music_assistant.models.audio_analysis_provider import AnalysisSessionData  # noqa: PLC0415

    af = _make_audio_format(sample_rate)
    sd = MagicMock()
    sd.item_id = "track-t23"
    sd.provider = "test_provider"
    sd.media_type = MediaType.TRACK
    sd.duration = 60.0
    provider._sessions[session_id] = AnalysisSessionData(streamdetails=sd, audio_format=af)
    await provider._start_analysis(session_id, sd, af)


def _make_one_block_pcm(sample_rate: int = 22050) -> bytes:
    """Build exactly one 10-second block of 16-bit mono PCM at the given sample rate."""
    n_samples = sample_rate * 10
    audio_f32 = (np.sin(2 * np.pi * 440 * np.arange(n_samples) / sample_rate) * 0.5).astype(
        np.float32
    )
    return (audio_f32 * 32767).astype(np.int16).tobytes()


# ---------------------------------------------------------------------------
# Test 1: _decode_resample_extract exists and is callable
# ---------------------------------------------------------------------------


def test_decode_resample_extract_is_callable() -> None:
    """_decode_resample_extract must be a callable exported from the module."""
    fn = getattr(sonic_mod, "_decode_resample_extract", None)
    assert callable(fn), "_decode_resample_extract must be a module-level callable"


# ---------------------------------------------------------------------------
# Test 2: _decode_resample_extract returns the expected 3-tuple
# ---------------------------------------------------------------------------


def test_decode_resample_extract_returns_three_tuple() -> None:
    """_decode_resample_extract must return (pre_resample, post_resample, BlockFeatures|None)."""
    from music_assistant.providers.sonic_analysis import _decode_resample_extract  # noqa: PLC0415

    n_samples = ANALYSIS_SAMPLE_RATE * 10
    audio_f32 = np.zeros(n_samples, dtype=np.float32)
    pcm_bytes = (audio_f32 * 32767).astype(np.int16).tobytes()
    af = _make_audio_format(ANALYSIS_SAMPLE_RATE)

    result = _decode_resample_extract(af, pcm_bytes, None, ANALYSIS_SAMPLE_RATE, None)

    assert isinstance(result, tuple), "Return value must be a tuple"
    assert len(result) == 3, "Return tuple must have exactly 3 elements"
    pre, post, bf = result
    assert isinstance(pre, np.ndarray), "pre_resample must be np.ndarray"
    assert isinstance(post, np.ndarray), "post_resample must be np.ndarray"
    # bf may be None for silent/short audio, but type must be correct
    from music_assistant.providers.sonic_analysis.helpers import BlockFeatures  # noqa: PLC0415

    assert bf is None or isinstance(bf, BlockFeatures)


# ---------------------------------------------------------------------------
# Test 3: process_pcm_chunk uses asyncio.to_thread for decode+extract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_pcm_chunk_offloads_via_run_offloaded() -> None:
    """
    process_pcm_chunk must call _decode_resample_extract off the event loop.

    Verified by:
    1. Patching _decode_resample_extract with a MagicMock that returns a valid 3-tuple.
    2. Asserting the mock was called once per block.
    3. Asserting the source of process_pcm_chunk routes through the offload seam.
    """
    provider = _make_provider()
    session_id = "sess-t23-offload"
    await _start_session(provider, session_id, ANALYSIS_SAMPLE_RATE)

    pcm_bytes = _make_one_block_pcm(ANALYSIS_SAMPLE_RATE)
    n_samples = ANALYSIS_SAMPLE_RATE * 10
    dummy_audio = np.zeros(n_samples, dtype=np.float32)

    mock_helper = MagicMock(return_value=(dummy_audio, dummy_audio, None))

    with patch.object(sonic_mod, "_decode_resample_extract", mock_helper):
        await provider.process_pcm_chunk(session_id, pcm_bytes)

    assert mock_helper.call_count == 1, (
        f"_decode_resample_extract must be called once per block; got {mock_helper.call_count}"
    )

    # Structural check: process_pcm_chunk must offload decode+extract off the event loop
    # (via the concurrency-bounded _run_offloaded seam rather than running it inline).
    source = inspect.getsource(provider.process_pcm_chunk)
    assert "_run_offloaded" in source, (
        "process_pcm_chunk must offload decode+extract via _run_offloaded (CPU offload)"
    )
    assert "_decode_resample_extract" in source, (
        "process_pcm_chunk must reference _decode_resample_extract"
    )
