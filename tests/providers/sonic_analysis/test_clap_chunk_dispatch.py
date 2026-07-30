"""Unit tests for the CLAP chunk dispatch state machine."""

from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np

from music_assistant.providers.sonic_analysis import (
    CLAP_WINDOW_SECONDS,
    SonicSessionData,
    _dispatch_clap_chunk,
)

SR = 22050
WINDOW_SAMPLES = CLAP_WINDOW_SECONDS * SR


def _make_session(target_starts: list[int]) -> SonicSessionData:
    """Build a SonicSessionData with the given target starts and matching buffer lists."""
    return SonicSessionData(
        streamdetails=MagicMock(),
        audio_format=MagicMock(),
        clap_target_starts=list(target_starts),
        clap_target_buffers=[[] for _ in target_starts],
        clap_target_complete=[False] * len(target_starts),
    )


def _ramp(n: int) -> np.ndarray:
    """Build a monotonically increasing float32 array so slice positions are visible."""
    return np.arange(n, dtype=np.float32)


def test_returns_empty_when_no_targets() -> None:
    """Sessions with no planned target windows skip dispatch entirely."""
    session = _make_session([])
    completed = _dispatch_clap_chunk(session, _ramp(SR), SR)
    assert completed == []


def test_chunk_before_first_target_buffers_nothing() -> None:
    """Chunks that arrive before the first target start don't touch any buffer."""
    session = _make_session([10 * SR])
    completed = _dispatch_clap_chunk(session, _ramp(5 * SR), SR)
    assert completed == []
    assert session.clap_target_buffers[0] == []
    assert session.clap_position_samples == 5 * SR


def test_chunk_inside_window_buffers_partial() -> None:
    """A chunk landing mid-window appends but doesn't trigger completion."""
    session = _make_session([0])  # target at sample 0, ends at WINDOW_SAMPLES
    chunk = _ramp(3 * SR)  # 3s — less than 7s window
    completed = _dispatch_clap_chunk(session, chunk, SR)
    assert completed == []
    assert session.clap_target_complete[0] is False
    assert sum(len(a) for a in session.clap_target_buffers[0]) == 3 * SR


def test_window_completes_in_single_chunk() -> None:
    """A chunk that fully covers a target window emits the completed window."""
    session = _make_session([0])
    chunk = _ramp(WINDOW_SAMPLES)
    completed = _dispatch_clap_chunk(session, chunk, SR)
    assert len(completed) == 1
    assert len(completed[0]) == WINDOW_SAMPLES
    assert session.clap_target_complete[0] is True
    assert session.clap_target_buffers[0] == []  # freed on completion


def test_window_completes_across_multiple_chunks() -> None:
    """Three 3s chunks that span a 7s window complete it on the third."""
    session = _make_session([0])
    chunk_size = 3 * SR
    completed_first = _dispatch_clap_chunk(session, _ramp(chunk_size), SR)
    completed_second = _dispatch_clap_chunk(session, _ramp(chunk_size), SR)
    completed_third = _dispatch_clap_chunk(session, _ramp(chunk_size), SR)
    assert completed_first == []
    assert completed_second == []
    assert len(completed_third) == 1
    assert len(completed_third[0]) == WINDOW_SAMPLES


def test_chunk_overlapping_two_windows() -> None:
    """A long chunk that spans the boundary between two adjacent windows fills both."""
    target_a = 0
    target_b = WINDOW_SAMPLES  # back-to-back, no gap
    session = _make_session([target_a, target_b])
    chunk = _ramp(2 * WINDOW_SAMPLES)
    completed = _dispatch_clap_chunk(session, chunk, SR)
    assert len(completed) == 2
    assert all(len(w) == WINDOW_SAMPLES for w in completed)
    assert session.clap_target_complete == [True, True]


def test_chunk_after_all_windows_complete_is_no_op_for_buffers() -> None:
    """Once every target is complete, further chunks update position but don't buffer."""
    session = _make_session([0])
    _dispatch_clap_chunk(session, _ramp(WINDOW_SAMPLES), SR)
    pre_buffers = [list(b) for b in session.clap_target_buffers]

    completed = _dispatch_clap_chunk(session, _ramp(SR), SR)
    assert completed == []
    assert [list(b) for b in session.clap_target_buffers] == pre_buffers
    assert session.clap_position_samples == WINDOW_SAMPLES + SR


def test_completed_window_is_exactly_window_samples_long() -> None:
    """If the buffer briefly accumulates more than 7s, the emitted window is trimmed."""
    session = _make_session([0])
    # one chunk slightly larger than the window
    chunk = _ramp(WINDOW_SAMPLES + 1024)
    completed = _dispatch_clap_chunk(session, chunk, SR)
    assert len(completed) == 1
    assert len(completed[0]) == WINDOW_SAMPLES


def test_position_samples_advances_monotonically() -> None:
    """clap_position_samples grows by exactly len(decoded_audio) per call."""
    session = _make_session([0])
    _dispatch_clap_chunk(session, _ramp(SR), SR)
    _dispatch_clap_chunk(session, _ramp(2 * SR), SR)
    _dispatch_clap_chunk(session, _ramp(3 * SR), SR)
    assert session.clap_position_samples == 6 * SR


def test_target_buffer_freed_on_completion() -> None:
    """clap_target_buffers[i] is reset to [] the moment window i completes."""
    session = _make_session([0])
    # Fill mostly, then finish
    _dispatch_clap_chunk(session, _ramp(WINDOW_SAMPLES - SR), SR)
    assert session.clap_target_buffers[0] != []
    _dispatch_clap_chunk(session, _ramp(SR), SR)
    assert session.clap_target_buffers[0] == []


def test_independent_targets_complete_independently() -> None:
    """Two non-adjacent targets each complete on their own boundary chunks."""
    session = _make_session([0, 10 * SR])
    # Chunk 1: fills target 0 fully, lands inside [0, 7s]
    completed_1 = _dispatch_clap_chunk(session, _ramp(WINDOW_SAMPLES), SR)
    assert len(completed_1) == 1
    assert session.clap_target_complete == [True, False]

    # Chunk 2: lands at 7s..10s — between targets, fills nothing
    completed_2 = _dispatch_clap_chunk(session, _ramp(3 * SR), SR)
    assert completed_2 == []
    assert session.clap_target_complete == [True, False]

    # Chunk 3: lands at 10s..17s — fills target 1 exactly
    completed_3 = _dispatch_clap_chunk(session, _ramp(WINDOW_SAMPLES), SR)
    assert len(completed_3) == 1
    assert session.clap_target_complete == [True, True]
