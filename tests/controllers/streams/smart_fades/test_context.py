"""Tests for build_transition_context — the immutable per-transition fact extraction."""

from __future__ import annotations

import dataclasses
import logging

import numpy as np
import pytest

from music_assistant.controllers.streams.smart_fades.models import TransitionTier
from music_assistant.controllers.streams.smart_fades.planner import SmartCrossFadePlanner
from music_assistant.controllers.streams.smart_fades.planner.context import (
    TransitionContext,
    build_transition_context,
)
from music_assistant.models.audio_analysis import AudioAnalysisData
from tests.controllers.streams.smart_fades.conftest import _analysis_with_bands

LOGGER = logging.getLogger(__name__)


def _beats(start: float, count: int, interval: float) -> np.ndarray:
    return np.arange(count, dtype=np.float32) * interval + start


def _analysis(
    bpm: float,
    duration: float = 240.0,
    rms_energy: np.ndarray | None = None,
    key: str | None = "A",
    mode: str | None = "minor",
) -> AudioAnalysisData:
    interval = 60.0 / bpm
    count = int(duration / interval) + 1
    beats = _beats(0.0, count, interval)
    # a normal (non-silent) track with a known key earns the full-blend tier;
    # tests that need something else override these
    energy = rms_energy if rms_energy is not None else np.full(1800, 0.5, dtype=np.float32)
    return AudioAnalysisData(
        duration=duration,
        bpm=bpm,
        beats=beats.tolist(),
        downbeats=beats[::4].tolist(),
        rms_energy=energy.tolist(),
        key=key,
        mode=mode,
    )


def _context(
    fade_out: AudioAnalysisData, fade_in: AudioAnalysisData, buffer: float = 45.0
) -> TransitionContext:
    return build_transition_context(fade_out, fade_in, buffer, LOGGER)


def _vocal_probabilities(duration: float, active_windows: list[tuple[float, float]]) -> list[float]:
    """Build a probability timeline that is quiet except inside ``active_windows``."""
    n_frames = 1800
    frame_duration = duration / n_frames
    probabilities = [0.05] * n_frames
    for start, end in active_windows:
        start_index = max(0, int(start / frame_duration))
        end_index = min(n_frames, int(end / frame_duration) + 1)
        for i in range(start_index, end_index):
            probabilities[i] = 0.9
    return probabilities


def _with_vocal_activity(
    analysis: AudioAnalysisData, active_windows: list[tuple[float, float]]
) -> AudioAnalysisData:
    """Attach a valid vocal_activity list, active only inside ``active_windows``."""
    assert analysis.duration is not None
    analysis.extra_data = {
        "vocal_activity": _vocal_probabilities(analysis.duration, active_windows)
    }
    return analysis


def test_context_energy_only_when_vocal_data_missing() -> None:
    """Analyses without a stored vocal-activity timeline disable all vocal-aware masks."""
    context = _context(_analysis(120.0), _analysis(120.0))

    assert context.vocal_out_placement is None
    assert context.vocal_in_placement is None
    assert context.vocal_out_scoring is None
    assert context.vocal_in_scoring is None


def test_context_builds_outgoing_masks_when_only_outgoing_timeline_is_valid() -> None:
    """A validated outgoing-only timeline builds the outgoing masks, leaving incoming ones None."""
    out = _with_vocal_activity(_analysis(120.0), [(220.0, 226.0)])
    context = _context(out, _analysis(120.0))

    assert context.vocal_out_placement is not None
    assert context.vocal_out_scoring is not None
    assert context.vocal_in_placement is None
    assert context.vocal_in_scoring is None


def test_context_builds_incoming_masks_when_only_incoming_timeline_is_valid() -> None:
    """A validated incoming-only timeline builds the incoming masks, leaving outgoing ones None."""
    inc = _with_vocal_activity(_analysis(120.0), [(10.0, 16.0)])
    context = _context(_analysis(120.0), inc)

    assert context.vocal_in_placement is not None
    assert context.vocal_in_scoring is not None
    assert context.vocal_out_placement is None
    assert context.vocal_out_scoring is None


def test_context_all_masks_none_when_both_timelines_missing() -> None:
    """Neither side carrying a timeline still yields all four masks as None (unchanged)."""
    context = _context(_analysis(120.0), _analysis(120.0))

    assert context.vocal_out_placement is None
    assert context.vocal_in_placement is None
    assert context.vocal_out_scoring is None
    assert context.vocal_in_scoring is None


def test_context_tier_full_blend_for_compatible_pair() -> None:
    """A clean, key-compatible, same-tempo 4/4 pair earns the full-blend tier."""
    # A minor and C major are relative major/minor: the same Camelot slot
    context = _context(
        _analysis(120.0, key="A", mode="minor"), _analysis(120.0, key="C", mode="major")
    )

    assert context.tier is TransitionTier.FULL_BLEND


def test_context_mix_out_anchor_is_downbeat_snapped() -> None:
    """An outro whose energy decays below the mix-out floor anchors at that (downbeat) point."""
    bins = np.full(1800, 0.5, dtype=np.float32)
    t = np.linspace(0, 240.0, 1800)
    bins[t >= 220.0] = 0.2  # audible but below 0.7*sustained: mixed out, not silent

    context = _context(_analysis(120.0, rms_energy=bins), _analysis(120.0))

    # anchored at the mix-out point (media 220 -> buffer-local 25), well before the
    # RMS-audible boundary, and snapped to a real downbeat
    anchor = context.mix_out_anchor
    assert anchor is not None
    assert anchor == pytest.approx(25.0, abs=0.3)
    assert context.audio_end > anchor + 1.0
    assert float(np.min(np.abs(context.outgoing.downbeats - anchor))) < 0.05


def test_context_is_frozen() -> None:
    """TransitionContext is immutable: attribute assignment raises."""
    context = _context(_analysis(120.0), _analysis(120.0))

    with pytest.raises(dataclasses.FrozenInstanceError):
        context.buffer_duration = 999.0  # type: ignore[misc]


def test_context_tier_folds_kick_anchor_like_the_old_planner() -> None:
    """
    A kick drop that shortens the anchored tail demotes the tier, matching the old planner.

    The old planner folded the kick anchor into effective_end before masking
    the downbeat grid its blendability check reads; the context must reproduce
    that even though it keeps mix_out_anchor and kick_anchor as separate facts.
    """

    def env(value: float) -> np.ndarray:
        return np.full(1800, value, dtype=np.float32)

    t = np.linspace(0, 240.0, 1800)
    low = env(1.0)
    low[t >= 208.0] = 0.05  # kick dies at media 208 -> kick anchor at buffer-local 11
    out = _analysis_with_bands(low, env(0.5), env(0.5), env(0.3))
    rms = np.full(1800, 0.5, dtype=np.float32)
    rms[t >= 225.0] = 0.2  # full-band mix-out at buffer-local ~30, audible to the end
    out.rms_energy = rms.tolist()
    inc = _analysis_with_bands(env(1.0), env(0.5), env(0.5), env(0.3))

    context = _context(out, inc)

    # both anchors are separate facts on the context...
    assert context.kick_anchor == pytest.approx(11.0, abs=0.1)
    assert context.mix_out_anchor == pytest.approx(29.0, abs=2.0)
    # ...but the tier keys on the kick-folded window: only 6 downbeats fit
    # before the kick anchor, so the tail is not blendable
    assert context.tier is TransitionTier.QUICK_FADE
    # the early anchor's rungs overtrim the audible tail and get rejected; the
    # rescue pass re-anchors near the audible end, re-deriving the tier there
    plan = SmartCrossFadePlanner(LOGGER).plan(out, inc, 45.0)
    assert plan.tier is TransitionTier.FULL_BLEND
    assert plan.fade_out_window == pytest.approx(43.0, abs=1.0)
    assert plan.metrics.audible_outgoing_trim <= plan.crossfade_duration
    assert plan.metrics.anchor_on_downbeat
