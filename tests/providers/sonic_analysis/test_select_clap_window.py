"""Unit tests for the deterministic CLAP window selector + target-start planner."""

from __future__ import annotations

import numpy as np

from music_assistant.providers.sonic_analysis import (
    CLAP_SKIP_SECONDS,
    CLAP_WINDOW_SECONDS,
    compute_clap_target_starts,
    select_clap_window,
    select_clap_windows,
)

SR = 22050


def _ramp(n: int) -> np.ndarray:
    """Build a monotonically increasing float32 array so slice positions are visible."""
    return np.arange(n, dtype=np.float32)


def test_long_track_takes_skip_plus_window_offset() -> None:
    """Audio longer than skip+window should yield samples [45s, 52s)."""
    audio = _ramp(120 * SR)
    win = select_clap_window(audio, SR)
    assert win is not None
    assert len(win) == CLAP_WINDOW_SECONDS * SR
    # First sample of the window should equal CLAP_SKIP_SECONDS * SR
    assert float(win[0]) == float(CLAP_SKIP_SECONDS * SR)
    # Last sample matches too
    assert float(win[-1]) == float((CLAP_SKIP_SECONDS + CLAP_WINDOW_SECONDS) * SR - 1)


def test_exactly_skip_plus_window_still_hits_preferred_branch() -> None:
    """Boundary: a track exactly skip+window long should still slice [45s, 52s)."""
    audio = _ramp((CLAP_SKIP_SECONDS + CLAP_WINDOW_SECONDS) * SR)
    win = select_clap_window(audio, SR)
    assert win is not None
    assert len(win) == CLAP_WINDOW_SECONDS * SR
    assert float(win[0]) == float(CLAP_SKIP_SECONDS * SR)


def test_short_track_falls_back_to_middle_seven_seconds() -> None:
    """Tracks shorter than 52s fall back to the middle 7s of available audio."""
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




def test_multi_window_n1_matches_single_window() -> None:
    """N=1 must delegate to the single-window selector so Fast preset is unchanged."""
    audio = _ramp(60 * SR)
    single = select_clap_window(audio, SR)
    multi = select_clap_windows(audio, SR, 1)
    assert single is not None
    assert len(multi) == 1
    assert np.array_equal(multi[0], single)


def test_multi_window_n3_evenly_spans_past_intro() -> None:
    """N=3 on a long track spaces 3 windows from the 45s mark to the track tail."""
    track_seconds = 180
    audio = _ramp(track_seconds * SR)
    wins = select_clap_windows(audio, SR, 3)
    assert len(wins) == 3
    for w in wins:
        assert len(w) == CLAP_WINDOW_SECONDS * SR
    # First window starts at the 45s mark
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
    fallback = select_clap_window(audio, SR)
    assert fallback is not None
    assert np.array_equal(wins[0], fallback)


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




def test_target_starts_below_one_second_returns_empty() -> None:
    """Tracks under 1s of audio are unusable; caller must skip CLAP entirely."""
    assert compute_clap_target_starts(0.5, 8, SR) == []
    assert compute_clap_target_starts(0.99, 1, SR) == []


def test_target_starts_under_window_seconds_returns_whole_clip() -> None:
    """Tracks shorter than one window feed the whole clip (CLAP wrapper pads)."""
    assert compute_clap_target_starts(3.0, 8, SR) == [0]
    assert compute_clap_target_starts(1.0, 1, SR) == [0]


def test_target_starts_under_skip_plus_window_uses_middle_seven() -> None:
    """Tracks shorter than skip+window fall back to middle-7s placement."""
    duration = 30.0
    expected_start_seconds = (duration - CLAP_WINDOW_SECONDS) / 2.0
    assert compute_clap_target_starts(duration, 8, SR) == [int(expected_start_seconds * SR)]


def test_target_starts_long_track_preset_one() -> None:
    """Long track + Fast preset → single window at the skip mark."""
    assert compute_clap_target_starts(120.0, 1, SR) == [CLAP_SKIP_SECONDS * SR]


def test_target_starts_long_track_thorough_evenly_spaced() -> None:
    """Long track + Thorough → 8 windows from skip mark to track tail."""
    duration = 240.0
    result = compute_clap_target_starts(duration, 8, SR)
    assert len(result) == 8
    assert result[0] == CLAP_SKIP_SECONDS * SR
    assert result[-1] == int((duration - CLAP_WINDOW_SECONDS) * SR)
    assert all(result[i] < result[i + 1] for i in range(7))


def test_target_starts_short_track_caps_effective_n() -> None:
    """A 60s track can only fit 2 non-overlapping 7s windows past the 45s skip."""
    sr = SR
    result = compute_clap_target_starts(60.0, 8, sr)
    assert len(result) == 2
    assert result[0] == CLAP_SKIP_SECONDS * sr
    assert result[1] == int((60.0 - CLAP_WINDOW_SECONDS) * sr)


def test_target_starts_balanced_preset_three_windows_long_track() -> None:
    """Balanced preset (N=3) returns 3 evenly spaced starts on a long track."""
    duration = 120.0
    result = compute_clap_target_starts(duration, 3, SR)
    assert len(result) == 3
    assert result[0] == CLAP_SKIP_SECONDS * SR
    assert result[-1] == int((duration - CLAP_WINDOW_SECONDS) * SR)


def test_target_starts_exactly_skip_plus_window_collapses_to_single() -> None:
    """A track of exactly skip+window seconds has zero usable spread → single window."""
    duration = float(CLAP_SKIP_SECONDS + CLAP_WINDOW_SECONDS)
    assert compute_clap_target_starts(duration, 8, SR) == [CLAP_SKIP_SECONDS * SR]


def test_target_starts_just_below_skip_plus_window_uses_middle() -> None:
    """A 51.5s track is just under skip+window — middle-7s fallback."""
    duration = 51.5
    expected_start = int(((duration - CLAP_WINDOW_SECONDS) / 2.0) * SR)
    assert compute_clap_target_starts(duration, 8, SR) == [expected_start]


def test_target_starts_deterministic() -> None:
    """Same inputs return identical lists across repeated calls."""
    a = compute_clap_target_starts(180.0, 5, SR)
    b = compute_clap_target_starts(180.0, 5, SR)
    assert a == b
