"""Shared test helpers for the smart_fades test suite."""

from __future__ import annotations

from typing import cast

import numpy as np

from music_assistant.controllers.streams.smart_fades.models import (
    PlanMetrics,
    TransitionPlan,
    TransitionTier,
)
from music_assistant.controllers.streams.smart_fades.planner.candidates import (
    Candidate,
    CandidateSpec,
)
from music_assistant.models.audio_analysis import AudioAnalysisData


def build_test_candidate(  # noqa: PLR0913
    *,
    bars: int = 8,
    ideal: int = 8,
    collision: float = 0.0,
    weighted: float = 0.0,
    trim: float = 0.0,
    vocal_fade: float = 0.0,
    on_downbeat: bool = True,
    duration: float = 20.0,
    one_sided: str | None = None,
    tier: TransitionTier = TransitionTier.FULL_BLEND,
    fadein_trim: float | None = None,
    fade_end: float | None = None,
) -> Candidate:
    """Build a minimal ``Candidate`` with only the fields a policy under test reads."""
    spec = CandidateSpec(
        tier=tier, bars=bars, anchor_s=None, entry_s=None, one_sided_vocal=one_sided
    )
    plan = TransitionPlan(
        tier=tier,
        fade_out_window=fade_end if fade_end is not None else duration,
        crossfade_duration=duration,
        fadein_trim_start=fadein_trim,
    )
    metrics = PlanMetrics(
        audible_outgoing_trim=trim,
        outgoing_vocal_fade_seconds=vocal_fade,
        anchor_on_downbeat=on_downbeat,
        collision_seconds=collision,
        weighted_collision_seconds=weighted,
    )
    return Candidate(spec=spec, plan=plan, metrics=metrics, ideal_bars=ideal)


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
    key: str | None = "A",
    mode: str | None = "minor",
) -> AudioAnalysisData:
    """
    Build an analysis row with v2 ``band_rms`` envelopes for band-profile tests.

    Each band accepts either a constant level or a 1800-bin array, so callers
    can vary a band's envelope over time (e.g. a kick that drops out midway).
    Defaults to a self-compatible key so a clean pair earns the full-blend tier.

    :param low: Low-band envelope, constant level or 1800-bin array.
    :param low_mid: Low-mid-band envelope, constant level or 1800-bin array.
    :param mid: Mid-band envelope, constant level or 1800-bin array.
    :param high: High-band envelope, constant level or 1800-bin array.
    :param duration: Track duration in seconds.
    :param key: Detected key pitch class (Camelot key gating).
    :param mode: Detected mode, "major" or "minor".
    """
    beats = np.arange(0.0, duration, 0.5, dtype=np.float32)
    return AudioAnalysisData(
        duration=duration,
        bpm=120.0,
        beats=beats.tolist(),
        downbeats=beats[::4].tolist(),
        rms_energy=np.full(1800, 0.5, dtype=np.float32).tolist(),
        key=key,
        mode=mode,
        extra_data={
            "band_rms": {
                "low": _envelope(low),
                "low_mid": _envelope(low_mid),
                "mid": _envelope(mid),
                "high": _envelope(high),
            }
        },
    )
