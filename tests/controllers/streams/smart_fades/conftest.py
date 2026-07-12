"""Shared test helpers for the smart_fades test suite."""

from __future__ import annotations

from typing import cast

import numpy as np

from music_assistant.models.audio_analysis import AudioAnalysisData


def _envelope(value: float | list[float] | np.ndarray) -> list[float]:
    """Broadcast a scalar to a flat 1800-bin envelope, or pass an array through."""
    if isinstance(value, (list, np.ndarray)):
        arr = np.asarray(value, dtype=np.float32)
        if len(arr) != 1800:
            raise ValueError(f"band envelope arrays must have 1800 bins, got {len(arr)}")
        return cast("list[float]", arr.tolist())
    return np.full(1800, value, dtype=np.float32).tolist()


def _analysis_with_bands(
    low: float | list[float] | np.ndarray,
    low_mid: float | list[float] | np.ndarray,
    mid: float | list[float] | np.ndarray,
    high: float | list[float] | np.ndarray,
    duration: float = 240.0,
) -> AudioAnalysisData:
    """
    Build an analysis row with v2 ``band_rms`` envelopes for band-profile tests.

    Each band accepts either a constant level or a 1800-bin array, so callers
    can vary a band's envelope over time (e.g. a kick that drops out midway).

    :param low: Low-band envelope, constant level or 1800-bin array.
    :param low_mid: Low-mid-band envelope, constant level or 1800-bin array.
    :param mid: Mid-band envelope, constant level or 1800-bin array.
    :param high: High-band envelope, constant level or 1800-bin array.
    :param duration: Track duration in seconds.
    """
    beats = np.arange(0.0, duration, 0.5, dtype=np.float32)
    return AudioAnalysisData(
        duration=duration,
        bpm=120.0,
        beats=beats.tolist(),
        downbeats=beats[::4].tolist(),
        rms_energy=np.full(1800, 0.5, dtype=np.float32).tolist(),
        extra_data={
            "band_rms": {
                "low": _envelope(low),
                "low_mid": _envelope(low_mid),
                "mid": _envelope(mid),
                "high": _envelope(high),
            }
        },
    )
