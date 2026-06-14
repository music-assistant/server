"""Tests for smart fades helper functions."""

from __future__ import annotations

import numpy as np
import pytest

from music_assistant.controllers.streams.smart_fades.helpers import (
    detect_effective_audio_end,
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
