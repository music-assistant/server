"""Unit tests for the finalize-time flush of partially filled CLAP windows."""

from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np

from music_assistant.providers.sonic_analysis import (
    CLAP_WINDOW_SECONDS,
    SonicAnalysisProvider,
    SonicSessionData,
)

SR = 22050
WINDOW_SAMPLES = CLAP_WINDOW_SECONDS * SR


def _make_provider() -> tuple[SonicAnalysisProvider, list[np.ndarray]]:
    """
    Stub provider whose create_task records the window each dispatch was handed.

    :returns: ``(provider, dispatched_windows)``.
    """
    dispatched: list[np.ndarray] = []
    p = SonicAnalysisProvider.__new__(SonicAnalysisProvider)
    p.logger = MagicMock()
    p.mass = MagicMock()
    p.mass.create_task = MagicMock(side_effect=lambda _coro: MagicMock())

    def _record(_session: SonicSessionData, window_audio: np.ndarray, _source_sr: int) -> MagicMock:
        """Capture the window instead of running inference on it."""
        dispatched.append(window_audio)
        return MagicMock()

    p._run_single_clap_window = _record  # type: ignore[method-assign,assignment]
    return p, dispatched


def _make_session(target_starts: list[int]) -> SonicSessionData:
    """Build a SonicSessionData with the given target starts and matching buffer lists."""
    return SonicSessionData(
        streamdetails=MagicMock(),
        audio_format=MagicMock(),
        clap_target_starts=list(target_starts),
        clap_target_buffers=[[] for _ in target_starts],
        clap_target_complete=[False] * len(target_starts),
    )


def test_partial_window_is_flushed() -> None:
    """A window holding under 7s of audio is dispatched and marked complete."""
    p, dispatched = _make_provider()
    session = _make_session([0])
    partial = np.ones(SR * 3, dtype=np.float32)
    session.clap_target_buffers[0] = [partial]

    p._flush_incomplete_clap_windows(session, SR)

    assert len(dispatched) == 1
    assert len(dispatched[0]) == SR * 3
    assert session.clap_target_complete == [True]
    assert session.clap_target_buffers[0] == []
    assert len(session.clap_inference_tasks) == 1


def test_multiple_buffered_chunks_are_concatenated() -> None:
    """Chunks accumulated across dispatches are joined in order for the flush."""
    p, dispatched = _make_provider()
    session = _make_session([0])
    session.clap_target_buffers[0] = [
        np.arange(0, SR, dtype=np.float32),
        np.arange(SR, 2 * SR, dtype=np.float32),
    ]

    p._flush_incomplete_clap_windows(session, SR)

    np.testing.assert_array_equal(dispatched[0], np.arange(0, 2 * SR, dtype=np.float32))


def test_already_complete_window_is_not_redispatched() -> None:
    """Windows that already reached the 7s gate are left alone."""
    p, dispatched = _make_provider()
    session = _make_session([0, WINDOW_SAMPLES])
    session.clap_target_complete = [True, False]
    session.clap_target_buffers = [[], [np.ones(SR, dtype=np.float32)]]

    p._flush_incomplete_clap_windows(session, SR)

    assert len(dispatched) == 1
    assert len(dispatched[0]) == SR


def test_window_with_no_audio_stays_incomplete() -> None:
    """A planned window the stream never reached is not faked as complete."""
    p, dispatched = _make_provider()
    session = _make_session([0, WINDOW_SAMPLES])
    session.clap_target_buffers = [[np.ones(SR, dtype=np.float32)], []]

    p._flush_incomplete_clap_windows(session, SR)

    assert len(dispatched) == 1
    assert session.clap_target_complete == [True, False]


def test_no_targets_is_a_no_op() -> None:
    """A session with no planned windows dispatches nothing."""
    p, dispatched = _make_provider()
    session = _make_session([])

    p._flush_incomplete_clap_windows(session, SR)

    assert dispatched == []
    assert session.clap_inference_tasks == []
