"""Unit tests for the deterministic CLAP window selector.

select_clap_window is the single source of truth for which 7-second slice
of a track CLAP sees. Pinning its behavior so callers can rely on it being
repeatable across both analyze_file and the live-playback path.
"""

from __future__ import annotations

import numpy as np

from music_assistant.providers.sonic_analysis import (
    CLAP_SKIP_SECONDS,
    CLAP_WINDOW_SECONDS,
    select_clap_window,
    select_clap_windows,
)

SR = 22050


def _ramp(n: int) -> np.ndarray:
    """Build a monotonically increasing float32 array so slice positions are visible."""
    return np.arange(n, dtype=np.float32)


def test_long_track_takes_skip_plus_window_offset() -> None:
    """Audio longer than skip+window should yield samples [30s, 37s)."""
    audio = _ramp(60 * SR)
    win = select_clap_window(audio, SR)
    assert win is not None
    assert len(win) == CLAP_WINDOW_SECONDS * SR
    # First sample of the window should equal CLAP_SKIP_SECONDS * SR
    assert float(win[0]) == float(CLAP_SKIP_SECONDS * SR)
    # Last sample matches too
    assert float(win[-1]) == float((CLAP_SKIP_SECONDS + CLAP_WINDOW_SECONDS) * SR - 1)


def test_exactly_37s_still_hits_preferred_branch() -> None:
    """Boundary: a track exactly skip+window long should still slice [30s, 37s)."""
    audio = _ramp((CLAP_SKIP_SECONDS + CLAP_WINDOW_SECONDS) * SR)
    win = select_clap_window(audio, SR)
    assert win is not None
    assert len(win) == CLAP_WINDOW_SECONDS * SR
    assert float(win[0]) == float(CLAP_SKIP_SECONDS * SR)


def test_short_track_falls_back_to_middle_seven_seconds() -> None:
    """Tracks shorter than 37s fall back to the middle 7s of available audio."""
    audio_len_seconds = 20
    audio = _ramp(audio_len_seconds * SR)
    win = select_clap_window(audio, SR)
    assert win is not None
    assert len(win) == CLAP_WINDOW_SECONDS * SR
    expected_start = (audio_len_seconds * SR - CLAP_WINDOW_SECONDS * SR) // 2
    assert float(win[0]) == float(expected_start)


def test_very_short_track_returns_raw_audio() -> None:
    """Tracks >=1s but <7s should be returned whole (CLAP pads by repeat)."""
    audio = _ramp(3 * SR)  # 3 seconds
    win = select_clap_window(audio, SR)
    assert win is not None
    assert len(win) == 3 * SR
    assert float(win[0]) == 0.0


def test_sub_one_second_returns_none() -> None:
    """Audio under 1 second is unusable; caller should skip CLAP entirely."""
    audio = _ramp(SR // 2)  # 500ms
    assert select_clap_window(audio, SR) is None


def test_deterministic_across_calls() -> None:
    """Same inputs yield byte-identical output across repeated calls."""
    audio = _ramp(45 * SR)
    w1 = select_clap_window(audio, SR)
    w2 = select_clap_window(audio, SR)
    assert w1 is not None
    assert w2 is not None
    assert np.array_equal(w1, w2)


# --------------------------------------------------------------------------- #
#  select_clap_windows (multi-window)                                          #
# --------------------------------------------------------------------------- #


def test_multi_window_n1_matches_single_window() -> None:
    """N=1 must delegate to the single-window selector so Fast preset is unchanged."""
    audio = _ramp(60 * SR)
    single = select_clap_window(audio, SR)
    multi = select_clap_windows(audio, SR, 1)
    assert single is not None
    assert len(multi) == 1
    assert np.array_equal(multi[0], single)


def test_multi_window_n3_evenly_spans_past_intro() -> None:
    """N=3 on a long track spaces 3 windows from the 30s mark to the track tail."""
    track_seconds = 180
    audio = _ramp(track_seconds * SR)
    wins = select_clap_windows(audio, SR, 3)
    assert len(wins) == 3
    for w in wins:
        assert len(w) == CLAP_WINDOW_SECONDS * SR
    # First window starts at the 30s mark
    assert float(wins[0][0]) == float(CLAP_SKIP_SECONDS * SR)
    # Last window ends right at the track tail
    assert float(wins[-1][-1]) == float(track_seconds * SR - 1)
    # Middle window is somewhere between the first and last
    assert float(wins[0][0]) < float(wins[1][0]) < float(wins[-1][0])


def test_multi_window_n8_on_long_track() -> None:
    """N=8 on a 4-minute track produces 8 non-overlapping 7s windows."""
    audio = _ramp(240 * SR)
    wins = select_clap_windows(audio, SR, 8)
    assert len(wins) == 8
    # Each window has the right length
    for w in wins:
        assert len(w) == CLAP_WINDOW_SECONDS * SR
    # Windows are monotonically ordered
    starts = [int(w[0]) for w in wins]
    assert starts == sorted(starts)
    # First and last anchored as documented
    assert starts[0] == CLAP_SKIP_SECONDS * SR
    assert starts[-1] == 240 * SR - CLAP_WINDOW_SECONDS * SR


def test_multi_window_short_track_falls_back_to_single() -> None:
    """Tracks too short for multi-window spacing degrade to the single-window rule."""
    audio = _ramp(20 * SR)  # 20s — not enough past-intro region for multi-window
    wins = select_clap_windows(audio, SR, 5)
    assert len(wins) == 1
    # Should match the single-window middle-7s fallback exactly
    assert np.array_equal(wins[0], select_clap_window(audio, SR))


def test_multi_window_too_short_returns_empty() -> None:
    """Under 1s of audio returns empty list — caller must skip CLAP entirely."""
    audio = _ramp(SR // 2)
    assert select_clap_windows(audio, SR, 3) == []


def test_multi_window_deterministic() -> None:
    """Same input yields byte-identical windows across repeated calls."""
    audio = _ramp(120 * SR)
    a = select_clap_windows(audio, SR, 5)
    b = select_clap_windows(audio, SR, 5)
    assert len(a) == len(b) == 5
    for wa, wb in zip(a, b, strict=True):
        assert np.array_equal(wa, wb)
