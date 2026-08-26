"""Tests for SonicAnalysisProvider._finalize base-class contract."""

from __future__ import annotations

import struct
import time
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest
from music_assistant_models.enums import ContentType, MediaType
from music_assistant_models.media_items import AudioFormat

from music_assistant.models.audio_analysis import AudioAnalysisData, AudioAnalysisError
from music_assistant.providers.sonic_analysis import SonicAnalysisProvider, SonicSessionData

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_provider() -> tuple[SonicAnalysisProvider, AsyncMock, AsyncMock]:
    """
    Construct a SonicAnalysisProvider with mocked MA infrastructure.

    Uses ``__new__`` to bypass ``__init__`` (no model downloads) and manually
    sets the attributes the finalize path touches.

    :returns: ``(provider, set_audio_analysis_mock, post_analysis_mock)``
    """
    set_aa_mock = AsyncMock()
    post_analysis_mock = AsyncMock()

    mass = MagicMock()
    mass.streams.audio_analysis.set_audio_analysis = set_aa_mock
    mass.streams.audio_analysis.get_audio_analysis_version = AsyncMock(return_value=None)
    mass.streams.audio_analysis.record_analysis_failure = AsyncMock()
    mass.create_task = MagicMock(side_effect=lambda coro: coro.close() or MagicMock())

    manifest = MagicMock()
    manifest.domain = "sonic_analysis"

    p = SonicAnalysisProvider.__new__(SonicAnalysisProvider)
    p.logger = MagicMock()
    p.mass = mass
    p.manifest = manifest
    p._sessions = {}
    p._finalize_tasks = set()
    p._clap_model = None
    p._clap_prompt_order = []
    p._clap_text_embeddings = None
    p.analysis_version = 1
    p.post_analysis = post_analysis_mock  # type: ignore[method-assign]
    return p, set_aa_mock, post_analysis_mock


def _make_streamdetails(item_id: str = "track-1") -> MagicMock:
    """Return a minimal streamdetails mock."""
    sd = MagicMock()
    sd.item_id = item_id
    sd.provider = "test_provider"
    sd.media_type = MediaType.TRACK
    sd.duration = None
    return sd


def _make_audio_format(
    sample_rate: int = 22050,
    bit_depth: int = 16,
    channels: int = 1,
    content_type: ContentType = ContentType.PCM_S16LE,
) -> AudioFormat:
    """Return a real AudioFormat for 16-bit mono PCM."""
    return AudioFormat(
        content_type=content_type,
        sample_rate=sample_rate,
        bit_depth=bit_depth,
        channels=channels,
    )


def _make_pcm_sine(
    sample_rate: int = 22050,
    duration_sec: float = 12.0,
) -> bytes:
    """Generate a simple sine-wave PCM block sufficient for one 10-second feature block."""
    n = int(sample_rate * duration_sec)
    t = np.linspace(0, duration_sec, n, endpoint=False)
    wave = (np.sin(2 * np.pi * 440 * t) * 0.5 * 32767).astype(np.int16)
    return struct.pack(f"<{n}h", *wave)


def _make_session(
    provider: SonicAnalysisProvider,
    session_id: str,
    *,
    sample_rate: int = 22050,
    bit_depth: int = 16,
    channels: int = 1,
) -> SonicSessionData:
    """
    Register a fresh SonicSessionData for session_id in the provider.

    :param provider: The provider instance to register the session on.
    :param session_id: The session ID to register.
    :param sample_rate: PCM sample rate in Hz.
    :param bit_depth: PCM bit depth.
    :param channels: PCM channel count.
    :returns: The created SonicSessionData.
    """
    af = _make_audio_format(sample_rate=sample_rate, bit_depth=bit_depth, channels=channels)
    sd = _make_streamdetails()
    block_bytes = sample_rate * (bit_depth // 8) * channels * 10
    session = SonicSessionData(
        streamdetails=sd,
        audio_format=af,
        block_bytes=block_bytes,
        start_time=time.monotonic(),
        clap_target_starts=[],
        clap_target_buffers=[],
        clap_target_complete=[],
    )
    provider._sessions[session_id] = session
    return session


# ---------------------------------------------------------------------------
# Test 1: happy path — _finalize returns AudioAnalysisData, base class persists
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_finalize_calls_set_audio_analysis_and_post_analysis() -> None:
    """
    finalize() must call set_audio_analysis and post_analysis exactly once each.

    Before the fix, _finalize returns None so the base class skips both.
    After the fix, _finalize returns AudioAnalysisData and the base class
    calls set_audio_analysis + post_analysis exactly once.
    """
    provider, set_aa, post_analysis = _make_provider()
    session_id = "test-session-happy"

    session = _make_session(provider, session_id)
    af = session.audio_format
    pcm = _make_pcm_sine(sample_rate=af.sample_rate, duration_sec=12.0)

    # Feed PCM so we have real feature blocks
    await provider.process_pcm_chunk(session_id, pcm)

    # Call the BASE CLASS finalize (not _finalize directly)
    await provider.finalize(session_id)

    # set_audio_analysis called once by the base class
    set_aa.assert_called_once()
    call_kwargs = set_aa.call_args.kwargs
    assert call_kwargs["item_id"] == session.streamdetails.item_id
    assert call_kwargs["aa_provider_domain"] == provider.domain
    analysis_arg = call_kwargs["analysis"]
    assert isinstance(analysis_arg, AudioAnalysisData)
    assert analysis_arg.duration is not None
    assert analysis_arg.duration > 0

    # post_analysis called once with the persisted analysis
    post_analysis.assert_called_once()
    _, post_analysis_arg = post_analysis.call_args.args
    assert post_analysis_arg is analysis_arg

    # Session cleaned up by base class
    assert session_id not in provider._sessions


# ---------------------------------------------------------------------------
# Test 2: short-audio early-return — set_audio_analysis and post_analysis not called
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_finalize_skips_persist_when_no_feature_blocks() -> None:
    """
    finalize() must not persist or fire post_analysis when no feature blocks exist.

    This covers the early-return path when accumulated.rms_frames is empty
    (e.g. audio too short to produce a single 10-second block).
    """
    provider, set_aa, post_analysis = _make_provider()
    session_id = "test-session-empty"

    # Do NOT call process_pcm_chunk — accumulated.rms_frames stays empty
    _make_session(provider, session_id)

    await provider.finalize(session_id)

    set_aa.assert_not_called()
    post_analysis.assert_not_called()
    # Session is still cleaned up by base class
    assert session_id not in provider._sessions


# ---------------------------------------------------------------------------
# Test 3: unknown session — no exception, no persist, debug log emitted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_finalize_unknown_session_is_silent() -> None:
    """
    finalize() on an unknown session_id must not raise and must log at debug level.

    This verifies the guard at the top of _finalize for sessions that were
    already cancelled or never started.
    """
    provider, set_aa, post_analysis = _make_provider()

    await provider.finalize("nonexistent-session-id")

    set_aa.assert_not_called()
    post_analysis.assert_not_called()
    # A debug log should have been emitted for the unknown session
    provider.logger.debug.assert_called()  # type: ignore[attr-defined]
    debug_calls = [str(c) for c in provider.logger.debug.call_args_list]  # type: ignore[attr-defined]
    assert any("nonexistent-session-id" in c for c in debug_calls)


# ---------------------------------------------------------------------------
# Test 4: empty accumulated — _finalize raises AudioAnalysisError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_finalize_raises_when_no_feature_blocks() -> None:
    """_finalize must raise AudioAnalysisError when no feature blocks were accumulated."""
    provider, _set_aa, _post_analysis = _make_provider()
    session_id = "test-session-empty-raise"
    _make_session(provider, session_id)

    with pytest.raises(AudioAnalysisError, match="no usable audio"):
        await provider._finalize(session_id)
