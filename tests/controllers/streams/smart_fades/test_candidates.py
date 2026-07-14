"""Tests for the smart fades candidate factory."""

from __future__ import annotations

import logging

import numpy as np
import pytest

from music_assistant.controllers.streams.smart_fades.models import TransitionTier
from music_assistant.controllers.streams.smart_fades.planner.candidates import (
    CandidateFactory,
    CandidateSpec,
    bars_ladder,
)
from music_assistant.controllers.streams.smart_fades.planner.context import (
    TransitionContext,
    build_transition_context,
)
from music_assistant.models.audio_analysis import AudioAnalysisData

from .conftest import _analysis_with_bands

LOGGER = logging.getLogger(__name__)


def _analysis(
    bpm: float, duration: float = 240.0, key: str | None = "A", mode: str | None = "minor"
) -> AudioAnalysisData:
    interval = 60.0 / bpm
    beats = np.arange(0.0, duration, interval, dtype=np.float32)
    return AudioAnalysisData(
        duration=duration,
        bpm=bpm,
        beats=beats.tolist(),
        downbeats=beats[::4].tolist(),
        rms_energy=np.full(1800, 0.5, dtype=np.float32).tolist(),
        key=key,
        mode=mode,
    )


def _ctx(
    out: AudioAnalysisData, inc: AudioAnalysisData, buffer_duration: float = 45.0
) -> TransitionContext:
    return build_transition_context(out, inc, buffer_duration, LOGGER)


def _spec(
    ctx: TransitionContext,
    bars: int,
    anchor_s: float | None = None,
    entry_s: float | None = None,
) -> CandidateSpec:
    return CandidateSpec(tier=ctx.tier, bars=bars, anchor_s=anchor_s, entry_s=entry_s)


def _first_fitting(ctx: TransitionContext, factory: CandidateFactory) -> object:
    """Emulate the energy ladder: largest rung that builds (the old _energy_candidate)."""
    for bars in bars_ladder(ctx, ctx.tier):
        candidate = factory.build(_spec(ctx, bars))
        if candidate is not None:
            return candidate
    raise AssertionError("the 1-bar rung must always yield a candidate")


class TestFactoryGoldenTiming:
    """
    The factory's timed fields are pinned to golden values from the old planner.

    The old monolithic planner (``_prepare_decks``/``_choose_tier``/
    ``_energy_candidate``/``_build_candidate``) was deleted at the task-9
    switchover; these values were captured from it, live, on these exact
    fixtures, before deletion.
    """

    def test_energy_candidate_timing_matches_the_old_planner(self) -> None:
        """The default-spec ladder walk reproduces the old planner's energy candidate timing."""
        out, inc = _analysis(120.0, duration=240.0), _analysis(122.0, duration=240.0)

        ctx = _ctx(out, inc)
        candidate = _first_fitting(ctx, CandidateFactory(ctx, LOGGER))

        assert candidate.plan.tier is TransitionTier.FULL_BLEND
        assert candidate.plan.fade_out_window == 45.0
        assert candidate.plan.crossfade_duration == pytest.approx(13.770492, abs=1e-5)
        assert candidate.plan.fadein_trim_start == 0.0
        assert len(candidate.plan.tempo_plan.steps) == 4
        assert candidate.plan.fadeout_trim is None

    def test_explicit_anchor_timing_matches_the_old_build_candidate(self) -> None:
        """An explicitly re-anchored spec reproduces the old ``_build_candidate`` timing."""
        out, inc = _analysis(80.0, duration=240.0), _analysis(83.2, duration=240.0)

        ctx = _ctx(out, inc)
        candidate = CandidateFactory(ctx, LOGGER).build(_spec(ctx, 8, anchor_s=20.0, entry_s=0.0))

        assert candidate is not None
        assert candidate.plan.tier is TransitionTier.QUICK_FADE
        assert candidate.plan.fade_out_window == 20.0
        assert candidate.plan.crossfade_duration == pytest.approx(14.0, abs=1e-5)
        assert candidate.plan.fadein_trim_start == 0.0


class TestCandidateBuildGuards:
    """Ports of the old candidate build guards, against the factory API."""

    def test_dead_grid_before_energetic_buffer_end_does_not_inflate_quick_fade(self) -> None:
        """A beatless-but-energetic outro keeps the quick fade at its intended bar count."""
        out = _analysis(120.0, duration=240.0)
        beats = np.asarray(out.beats, dtype=np.float32)
        out.beats = beats[beats <= 230.0].tolist()
        downbeats = np.asarray(out.downbeats, dtype=np.float32)
        out.downbeats = downbeats[downbeats <= 230.0].tolist()
        inc = _analysis(156.0, duration=240.0)

        ctx = _ctx(out, inc)
        candidate = _first_fitting(ctx, CandidateFactory(ctx, LOGGER))

        assert candidate.plan.tier is TransitionTier.QUICK_FADE
        bar_out = 4 * 60.0 / 120.0
        assert candidate.plan.crossfade_duration <= 2 * bar_out + 0.1

    def test_re_anchored_tier_downgrade_caps_the_bar_count(self) -> None:
        """A re-anchor that downgrades the tier also caps the inherited bar count."""
        ctx = _ctx(_analysis(80.0), _analysis(83.2))
        candidate = CandidateFactory(ctx, LOGGER).build(_spec(ctx, 8, anchor_s=20.0, entry_s=0.0))

        assert candidate is not None
        assert candidate.plan.tier is TransitionTier.QUICK_FADE
        assert not candidate.plan.tempo_plan
        # the built spec reflects the downgrade, so policies score reality
        assert candidate.spec.tier is TransitionTier.QUICK_FADE
        assert candidate.spec.bars <= 4
        # 4-bar quick-fade cap at 80 BPM (3s bars) plus sub-bar anchor slack
        assert candidate.plan.crossfade_duration <= 15.0

    def test_protected_intro_before_late_drop_degrades_instead_of_asserting(self) -> None:
        """A sung intro ahead of a late bass drop must yield a candidate, never raise."""

        def env(value: float) -> np.ndarray:
            return np.full(1800, value, dtype=np.float32)

        t = np.linspace(0, 240.0, 1800)
        low = np.where(t < 60.0, 0.02, 1.0).astype(np.float32)
        inc = _analysis_with_bands(low, env(0.6), env(0.6), env(0.3))
        rms = np.full(1800, 0.2, dtype=np.float32)
        rms[t >= 35.0] = 1.0
        inc.rms_energy = rms.tolist()
        out = _analysis_with_bands(env(1.0), env(0.5), env(0.5), env(0.3))

        ctx = _ctx(out, inc)
        candidate = _first_fitting(ctx, CandidateFactory(ctx, LOGGER))

        assert candidate.plan.crossfade_duration > 0.0

    def test_oversized_entry_returns_none_for_multi_bar_rung(self) -> None:
        """An entry too late for any multi-bar overlap makes the spec infeasible."""
        ctx = _ctx(_analysis(120.0, duration=240.0), _analysis(122.0, duration=240.0))
        # an entry this late leaves no room for any multi-bar overlap in the 45s head
        candidate = CandidateFactory(ctx, LOGGER).build(_spec(ctx, 8, entry_s=44.0))
        assert candidate is None


class TestFactoryPurity:
    """Building candidates never leaks state between builds."""

    def test_same_spec_builds_identical_plans(self) -> None:
        """An intervening re-anchored build never contaminates the next build."""
        ctx = _ctx(_analysis(120.0, duration=240.0), _analysis(122.0, duration=240.0))
        factory = CandidateFactory(ctx, LOGGER)
        spec = _spec(ctx, 8)

        first = factory.build(spec)
        # an intervening re-anchored build must not contaminate the next one
        factory.build(_spec(ctx, 2, anchor_s=20.0))
        second = factory.build(spec)

        assert first is not None
        assert second is not None
        assert first.plan == second.plan
        assert first.metrics == second.metrics


class TestFactoryMetrics:
    """Metrics are computed per candidate, vocal fields only with vocal data."""

    def test_energy_only_metrics(self) -> None:
        """Without vocal data the vocal metrics stay at their energy-only defaults."""
        ctx = _ctx(_analysis(120.0, duration=240.0), _analysis(122.0, duration=240.0))
        candidate = _first_fitting(ctx, CandidateFactory(ctx, LOGGER))

        assert candidate.metrics.audible_outgoing_trim >= 0.0
        assert candidate.metrics.collision_seconds == 0.0
        assert candidate.metrics.weighted_collision_seconds == 0.0

    def test_ideal_bars_defaults_to_spec_bars(self) -> None:
        """A spec without an explicit ideal_bars falls back to its own bar count."""
        ctx = _ctx(_analysis(120.0, duration=240.0), _analysis(122.0, duration=240.0))
        candidate = CandidateFactory(ctx, LOGGER).build(_spec(ctx, 1))
        assert candidate is not None
        assert candidate.ideal_bars == 1
