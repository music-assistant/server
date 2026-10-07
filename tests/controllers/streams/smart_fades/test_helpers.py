"""Tests for smart fades helper functions."""

from __future__ import annotations

import itertools

import numpy as np
import pytest

from music_assistant.controllers.streams.smart_fades.helpers import (
    camelot_affinity,
    db_ramp,
    detect_effective_audio_end,
    detect_groove_entry,
)


def _rms(track_duration: float, silent_tail: float, level: float = 0.5) -> np.ndarray:
    """Build a 1800-bin peak-normalized rms array with a silent tail."""
    bins = np.full(1800, level, dtype=np.float32)
    bins[0] = 1.0  # peak normalization reference
    if silent_tail > 0:
        silent_bins = int(silent_tail / track_duration * 1800)
        bins[-silent_bins:] = 0.001
    return bins


def test_no_rms_data_returns_buffer_duration() -> None:
    """When no RMS data is provided, return the buffer duration."""
    assert detect_effective_audio_end(None, 240.0, 45.0) == 45.0


def test_no_trailing_silence_returns_buffer_duration() -> None:
    """When there is no trailing silence, return the full buffer duration."""
    end = detect_effective_audio_end(_rms(240.0, silent_tail=0.0), 240.0, 45.0)
    assert end == pytest.approx(45.0, abs=0.2)


def test_silent_tail_is_excluded() -> None:
    """Trailing silence is excluded from the effective audio end."""
    # 10s of silence at the end of a 240s track: audible content ends at 35s buffer-local
    end = detect_effective_audio_end(_rms(240.0, silent_tail=10.0), 240.0, 45.0)
    assert end == pytest.approx(35.0, abs=0.3)


def test_fully_silent_tail_returns_zero() -> None:
    """When the entire tail is silent, return 0.0."""
    end = detect_effective_audio_end(_rms(240.0, silent_tail=50.0), 240.0, 45.0)
    assert end == 0.0


def test_quiet_but_musical_outro_is_kept() -> None:
    """Quiet but intentional outros above the floor are not treated as silence."""
    # outro at 30% of sustained level stays well above the silence floor
    bins = _rms(240.0, silent_tail=0.0)
    bins[-300:] = 0.15
    end = detect_effective_audio_end(bins, 240.0, 45.0)
    assert end == pytest.approx(45.0, abs=0.2)


def test_hiss_tail_on_loud_track_counts_as_silence() -> None:
    """On a loud track the floor scales up, so a hiss tail counts as silence."""
    # sustained level 0.9 -> floor 0.045; the 0.03 hiss tail falls below it
    bins = _rms(240.0, silent_tail=0.0, level=0.9)
    bins[-75:] = 0.03
    end = detect_effective_audio_end(bins, 240.0, 45.0)
    assert end == pytest.approx(35.0, abs=0.3)


def test_absolute_floor_applies_on_quiet_track() -> None:
    """On a quiet track the absolute 0.02 floor still flags a near-silent tail."""
    # sustained level 0.2 -> relative floor 0.01, clamped to absolute 0.02
    bins = _rms(240.0, silent_tail=0.0, level=0.2)
    bins[-75:] = 0.015
    end = detect_effective_audio_end(bins, 240.0, 45.0)
    assert end == pytest.approx(35.0, abs=0.3)


def test_all_nan_rms_returns_buffer_duration() -> None:
    """RMS data without any finite values fails open to the buffer duration."""
    bins = np.full(1800, np.nan, dtype=np.float32)
    assert detect_effective_audio_end(bins, 240.0, 45.0) == 45.0


@pytest.mark.parametrize(
    ("key_a", "mode_a", "key_b", "mode_b", "expected"),
    [
        ("C", "major", "C", "major", 1.0),
        ("C", "major", "G", "major", 0.9),
        ("C", "major", "D", "major", 0.55),
        ("C", "major", "A", "minor", 0.9),
        ("C", "major", "E", "minor", 0.45),
        ("C", "major", "F#", "major", 0.1),
    ],
)
def test_camelot_affinity(
    key_a: str,
    mode_a: str,
    key_b: str,
    mode_b: str,
    expected: float,
) -> None:
    """Camelot affinity distinguishes close harmonic moves from clashes."""
    assert camelot_affinity(key_a, mode_a, key_b, mode_b) == pytest.approx(expected)


def test_camelot_affinity_unknown_key_is_neutral() -> None:
    """Unknown harmonic data is left for the caller to treat neutrally."""
    assert camelot_affinity(None, None, "C", "major") is None


class TestGrooveEntry:
    """Groove entry = first sustained per-bar energy step (the drums coming in)."""

    def test_step_intro_detected_on_bar(self) -> None:
        """A quiet 16s intro followed by full energy yields an entry at ~16s."""
        duration = 240.0
        bins = np.full(1800, 0.5, dtype=np.float32)
        t = np.linspace(0, duration, 1800)
        bins[t < 16.0] = 0.05
        downbeats = np.arange(0.0, duration, 2.0, dtype=np.float32)
        entry = detect_groove_entry(bins, duration, downbeats)
        assert entry == pytest.approx(16.0, abs=2.1)

    def test_flat_track_has_no_entry(self) -> None:
        """A track that opens at full energy has no skippable intro."""
        bins = np.full(1800, 0.5, dtype=np.float32)
        downbeats = np.arange(0.0, 240.0, 2.0, dtype=np.float32)
        assert detect_groove_entry(bins, 240.0, downbeats) == 0.0

    def test_missing_data_has_no_entry(self) -> None:
        """Without energy data or a usable grid there is no entry to detect."""
        downbeats = np.arange(0.0, 240.0, 2.0, dtype=np.float32)
        assert detect_groove_entry(None, 240.0, downbeats) == 0.0
        bins = np.full(1800, 0.5, dtype=np.float32)
        assert detect_groove_entry(bins, 240.0, downbeats[:4]) == 0.0


class TestDbRamp:
    """db_ramp builds linear-in-dB asendcmd schedules."""

    def test_linear_ramp_endpoints_and_step(self) -> None:
        """The ramp spans [start, start+duration] and steps every ~0.1s."""
        steps = db_ramp(10.0, 2.0, 0.0, -26.0)
        assert steps[0][0] == pytest.approx(10.0)
        assert steps[0][1] == pytest.approx(0.0)
        assert steps[-1][0] == pytest.approx(12.0)
        assert steps[-1][1] == pytest.approx(-26.0)
        deltas = [b[0] - a[0] for a, b in itertools.pairwise(steps)]
        assert all(d == pytest.approx(0.1, abs=0.01) for d in deltas)

    def test_short_ramp_has_at_least_two_steps(self) -> None:
        """Even a sub-interval ramp produces a start and an end point."""
        steps = db_ramp(0.0, 0.05, -26.0, 0.0)
        assert len(steps) >= 2
        assert steps[-1][1] == pytest.approx(0.0)
