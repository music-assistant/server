"""Tests for the pure numpy frame aggregation helpers."""

from __future__ import annotations

import numpy as np
import pytest

from music_assistant.providers.smart_fades.helpers import aggregate_series_to_bins


class TestAggregateSeriesToBins:
    """Anti-aliased frame-to-bin resampling (mean power per bin, not point samples)."""

    def test_beat_ripple_does_not_alias(self) -> None:
        """A beat-rate amplitude ripple averages out instead of aliasing into bins."""
        # 40k frames of RMS oscillating 0.3..0.7: the ripple period (~7 frames) is
        # much shorter than a bin (~22 frames), so the boxcar must average it out;
        # point sampling would land bins anywhere in 0.3..0.7 (up to 0.2 off the mean)
        frames = (0.5 + 0.2 * np.sin(np.arange(40_000) * 0.9)).astype(np.float32)
        bins = aggregate_series_to_bins(frames, 1800, power=True)
        assert len(bins) == 1800
        rms_of_ripple = np.sqrt(np.mean(frames.astype(np.float64) ** 2))
        assert np.all(np.abs(bins - rms_of_ripple) < 0.02)

    def test_constant_series_is_preserved(self) -> None:
        """A flat series stays flat at the same level."""
        bins = aggregate_series_to_bins(np.full(999, 0.4, dtype=np.float32), 1800)
        assert bins == pytest.approx(np.full(1800, 0.4), abs=1e-6)

    def test_step_stays_sharp(self) -> None:
        """A cliff moves by at most one bin (no smearing beyond the boxcar)."""
        frames = np.concatenate([np.ones(1000, np.float32), np.zeros(1000, np.float32)])
        bins = aggregate_series_to_bins(frames, 200, power=True)
        # exactly one transition bin may hold an intermediate value
        assert np.sum((bins > 0.01) & (bins < 0.99)) <= 1

    def test_upsampling_short_series(self) -> None:
        """Fewer frames than bins (very short track) still yields n_bins values."""
        bins = aggregate_series_to_bins(np.array([1.0, 0.0], dtype=np.float32), 10)
        assert len(bins) == 10
        assert bins[0] == pytest.approx(1.0)
        assert bins[-1] == pytest.approx(0.0)
