"""Tests for the pure numpy frame aggregation helpers."""

from __future__ import annotations

import numpy as np
import pytest

from music_assistant.controllers.streams.smart_fades.models import BAND_RMS_BANDS
from music_assistant.models.audio_analysis import AudioAnalysisData
from music_assistant.providers.smart_fades.helpers import (
    aggregate_series_to_bins,
    compute_band_rms_frames,
)


class TestAggregateSeriesToBins:
    """Anti-aliased frame-to-bin resampling (mean power per bin, not point samples)."""

    def test_beat_ripple_does_not_alias(self) -> None:
        """A beat-rate amplitude ripple averages out instead of aliasing into bins."""
        # ripple period (~7 frames) << bin (~22 frames): the boxcar must flatten it
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


class TestBandRmsFrames:
    """Band-limited RMS frames: a pure tone lands in exactly one band."""

    def _tone(self, freq: float, seconds: float = 2.0, sr: int = 22050) -> np.ndarray:
        t = np.arange(int(sr * seconds)) / sr
        return (0.5 * np.sin(2 * np.pi * freq * t)).astype(np.float32)

    @pytest.mark.parametrize(
        ("freq", "band"),
        [(60.0, "low"), (250.0, "low_mid"), (1000.0, "mid"), (8000.0, "high")],
    )
    def test_tone_lands_in_its_band(self, freq: float, band: str) -> None:
        """Each band's tone dominates its own envelope by an order of magnitude."""
        frames = compute_band_rms_frames(self._tone(freq), 22050, 2205)
        assert set(frames) == set(BAND_RMS_BANDS)
        target = float(np.mean(frames[band]))
        others = max(float(np.mean(v)) for k, v in frames.items() if k != band)
        assert target > 10 * others

    def test_band_sum_tracks_full_rms(self) -> None:
        """Total band power approximates the plain time-domain RMS (Parseval)."""
        pcm = self._tone(60.0) + self._tone(1000.0)
        frames = compute_band_rms_frames(pcm, 22050, 2205)
        total = np.sqrt(sum(np.mean(v.astype(np.float64) ** 2) for v in frames.values()))
        expected = np.sqrt(np.mean(pcm.astype(np.float64) ** 2))
        assert total == pytest.approx(expected, rel=0.05)

    def test_partial_tail_window_included(self) -> None:
        """A trailing partial window still yields a frame (parity with full-band RMS)."""
        frames = compute_band_rms_frames(self._tone(1000.0, seconds=1.05), 22050, 2205)
        assert len(frames["mid"]) == 11  # 10 full + 1 partial


def test_extra_data_roundtrips_through_dict() -> None:
    """Band envelopes survive to_dict/from_dict (plain lists, no ndarrays)."""
    analysis = AudioAnalysisData(
        duration=10.0, extra_data={"band_rms": {"low": [0.1, 0.2], "mid": [0.3, 0.4]}}
    )
    restored = AudioAnalysisData.from_dict(analysis.to_dict())
    assert restored.extra_data == analysis.extra_data
