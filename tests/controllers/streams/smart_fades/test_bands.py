"""Tests for the band-power signal module (bar fractions on the deck's own grid)."""

from __future__ import annotations

import pytest

from music_assistant.controllers.streams.smart_fades.bands import (
    build_band_profile,
    loudness_referenced_level,
    smoothstep,
    window_duty,
    window_fraction,
)
from tests.controllers.streams.smart_fades.conftest import _analysis_with_bands


class TestBandProfile:
    """Power fractions on the bar grid, invariant to the peak normalization."""

    def test_fractions_are_power_not_amplitude(self) -> None:
        """Equal amplitudes in two bands give 0.5 power fraction each."""
        a = _analysis_with_bands(0.3, 0.3, 0.0, 0.0)
        p = build_band_profile(a)
        assert p is not None
        assert window_fraction(p, "low", 0.0, 60.0) == pytest.approx(0.5, abs=1e-6)

    def test_normalization_cancels(self) -> None:
        """Scaling all envelopes by a constant leaves fractions unchanged."""
        a1 = _analysis_with_bands(0.4, 0.1, 0.2, 0.05)
        a2 = _analysis_with_bands(0.2, 0.05, 0.1, 0.025)
        p1, p2 = build_band_profile(a1), build_band_profile(a2)
        assert p1 is not None
        assert p2 is not None
        f1 = window_fraction(p1, "mid", 0.0, 60.0)
        f2 = window_fraction(p2, "mid", 0.0, 60.0)
        assert f1 == pytest.approx(f2, abs=1e-6)

    def test_missing_band_rms_returns_none(self) -> None:
        """v1 rows (no extra_data) yield None so policies bypass."""
        a = _analysis_with_bands(0.3, 0.1, 0.1, 0.05)
        a.extra_data = None
        assert build_band_profile(a) is None

    def test_all_silent_track_returns_none(self) -> None:
        """All-zero envelopes have no active bars, so the profile is None."""
        assert build_band_profile(_analysis_with_bands(0.0, 0.0, 0.0, 0.0)) is None

    def test_too_few_downbeats_returns_none(self) -> None:
        """Fewer than 8 downbeats leaves no usable bar grid, so the profile is None."""
        a = _analysis_with_bands(0.3, 0.1, 0.1, 0.05)
        assert a.downbeats is not None
        a.downbeats = a.downbeats[:7]
        assert build_band_profile(a) is None

    def test_falsy_duration_returns_none(self) -> None:
        """A zero/missing duration leaves no bin-to-second mapping, so the profile is None."""
        a = _analysis_with_bands(0.3, 0.1, 0.1, 0.05)
        a.duration = 0.0
        assert build_band_profile(a) is None

    def test_empty_window_is_zero(self) -> None:
        """A window past the track end returns 0.0, never NaN."""
        p = build_band_profile(_analysis_with_bands(0.3, 0.1, 0.1, 0.05))
        assert p is not None
        assert window_fraction(p, "low", 500.0, 600.0) == 0.0
        assert window_duty(p, "low", 500.0, 600.0) == 0.0

    def test_duty_counts_bars_above_reference_fraction(self) -> None:
        """Duty = share of window bars at >= k x the track's sustained band power."""
        a = _analysis_with_bands(0.3, 0.1, 0.1, 0.05)
        assert a.extra_data is not None
        bands = a.extra_data["band_rms"]
        bands["mid"] = ([0.0] * 900) + ([0.4] * 900)  # mid silent first half
        p = build_band_profile(a)
        assert p is not None
        assert window_duty(p, "mid", 0.0, 120.0) == pytest.approx(0.0, abs=0.05)
        assert window_duty(p, "mid", 120.0, 240.0) == pytest.approx(1.0, abs=0.05)


class TestSmoothstep:
    """The only depth-shaping primitive: zero at threshold, saturating, continuous."""

    def test_endpoints_and_midpoint(self) -> None:
        """Exactly 0 below lo, 1 above hi, 0.5 at the midpoint."""
        assert smoothstep(0.09, 0.10, 0.25) == 0.0
        assert smoothstep(0.30, 0.10, 0.25) == 1.0
        assert smoothstep(0.175, 0.10, 0.25) == pytest.approx(0.5)


class TestLoudnessReferencedLevel:
    """Level normalized by the track's own active-bar total power."""

    def test_normalization_cancels(self) -> None:
        """Scaling all envelopes by a constant leaves the referenced level unchanged."""
        a1 = _analysis_with_bands(0.4, 0.1, 0.2, 0.05)
        a2 = _analysis_with_bands(0.2, 0.05, 0.1, 0.025)
        p1, p2 = build_band_profile(a1), build_band_profile(a2)
        assert p1 is not None
        assert p2 is not None
        r1 = loudness_referenced_level(p1, "mid", 0.0, 60.0)
        r2 = loudness_referenced_level(p2, "mid", 0.0, 60.0)
        assert r1 == pytest.approx(r2, abs=1e-6)

    def test_louder_window_scores_higher(self) -> None:
        """A window with more low-band power than the track average scores > 1."""
        a = _analysis_with_bands(0.1, 0.1, 0.1, 0.1)
        assert a.extra_data is not None
        bands = a.extra_data["band_rms"]
        bands["low"] = ([0.1] * 900) + ([0.5] * 900)  # louder low band, second half
        p = build_band_profile(a)
        assert p is not None
        quiet = loudness_referenced_level(p, "low", 0.0, 120.0)
        loud = loudness_referenced_level(p, "low", 120.0, 240.0)
        assert loud > quiet
