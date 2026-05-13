"""Unit tests for the deterministic CLAP target-start planner."""

from __future__ import annotations

from music_assistant.providers.sonic_analysis import (
    CLAP_SKIP_SECONDS,
    CLAP_WINDOW_SECONDS,
    compute_clap_target_starts,
)

SR = 22050


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
