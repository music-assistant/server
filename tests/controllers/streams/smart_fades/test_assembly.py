"""Tests for PlanAssembler and EmergencyHandoffFactory."""

from __future__ import annotations

import logging
from dataclasses import replace

import numpy as np
import pytest

from music_assistant.controllers.streams.smart_fades.models import (
    TransitionStrategy,
    TransitionTier,
)
from music_assistant.controllers.streams.smart_fades.planner import SmartCrossFadePlanner
from music_assistant.controllers.streams.smart_fades.planner.assembly import (
    EmergencyHandoffFactory,
    FallbackCrossfadeFactory,
    PlanAssembler,
)
from music_assistant.controllers.streams.smart_fades.planner.candidates import (
    Candidate,
    CandidateFactory,
    CandidateSpec,
    bars_ladder,
)
from music_assistant.controllers.streams.smart_fades.planner.context import (
    TransitionContext,
    build_transition_context,
)
from music_assistant.controllers.streams.smart_fades.vocal import COLLISION_SECONDS_LIMIT
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


def _first_fitting_candidate(ctx: TransitionContext, factory: CandidateFactory) -> Candidate:
    """Emulate the energy ladder: largest rung that builds (mirrors test_candidates.py)."""
    for bars in bars_ladder(ctx, ctx.tier):
        candidate = factory.build(
            CandidateSpec(tier=ctx.tier, bars=bars, anchor_s=None, entry_s=None)
        )
        if candidate is not None:
            return candidate
    raise AssertionError("the 1-bar rung must always yield a candidate")


def _bands_pair(f_low_out: float, f_low_in: float) -> tuple[AudioAnalysisData, AudioAnalysisData]:
    """Mirrors test_planner.py's ``_bands_pair``: an out/in pair with the given low-band fractions."""

    def _levels(f_low: float) -> tuple[float, float, float, float]:
        low = 1.0
        rest = ((1.0 - f_low) / (3.0 * f_low)) ** 0.5 * low
        return low, rest, rest, rest

    out = _analysis_with_bands(*_levels(f_low_out), duration=240.0)
    inc = _analysis_with_bands(*_levels(f_low_in), duration=240.0)
    return out, inc


def _mastered_fade_pair() -> tuple[AudioAnalysisData, AudioAnalysisData]:
    """Build a -14dB frozen-spectrum ramp from media 210s, mirroring test_generators.py's fixture."""
    t = np.linspace(0.0, 240.0, 1800)
    gain_db = np.where(t < 210.0, 0.0, -(t - 210.0) / 30.0 * 14.0)
    band = (0.3 * 10.0 ** (gain_db / 20.0)).astype(np.float32)
    out = _analysis_with_bands(band, band, band, band, duration=240.0)
    inc = _analysis(120.0, duration=240.0)
    return out, inc


def _rich_pair() -> tuple[AudioAnalysisData, AudioAnalysisData]:
    """Mirrors test_planner.py's ``_rich_pair``: bass-rich AND mid-heavy on both sides."""
    out = _analysis_with_bands(1.0, 0.3, 1.0, 0.3, duration=240.0)
    inc = _analysis_with_bands(1.0, 0.3, 1.0, 0.3, duration=240.0)
    return out, inc


def _wash_mid_stack_pair() -> tuple[AudioAnalysisData, AudioAnalysisData]:
    """Mirrors test_planner.py's ``_wash_mid_stack_pair``: triggers mid-depth dip-guard remediation."""
    amps = (0.065**0.5, 0.065**0.5, 0.42**0.5, 0.45**0.5)
    out = _analysis_with_bands(*amps, duration=240.0)
    inc = _analysis_with_bands(*amps, duration=240.0)
    t = np.linspace(0, 240.0, 1800)
    inc.rms_energy = np.where(t < 14.0, 0.05, 0.5).astype(np.float32).tolist()
    return out, inc


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


class TestFinalizeEqSelfConsistency:
    """``finalize()`` must yield the same EqPlan the old planner computes for the equivalent plan."""

    def test_full_blend_bass_and_mid_swap_parity(self) -> None:
        """A bass-rich and mid-heavy pair: low, mid and high schedules all match the old planner."""
        out, inc = _rich_pair()
        reference_plan = SmartCrossFadePlanner(LOGGER).plan(out, inc, 45.0)

        ctx = _ctx(out, inc)
        factory = CandidateFactory(ctx, LOGGER)
        candidate = _first_fitting_candidate(ctx, factory)
        new_plan = PlanAssembler(ctx, LOGGER).finalize(candidate)

        assert new_plan.eq_plan.swap_at == pytest.approx(reference_plan.eq_plan.swap_at)
        for attr in ("low_out", "low_in", "high_out", "high_in", "mid_out", "mid_in"):
            old_sched = getattr(reference_plan.eq_plan, attr)
            new_sched = getattr(new_plan.eq_plan, attr)
            assert (old_sched is None) == (new_sched is None), attr
            if old_sched is not None:
                assert new_sched.steps == pytest.approx(old_sched.steps), attr
                assert new_sched.shelf_type == old_sched.shelf_type
                assert new_sched.frequency == old_sched.frequency

    def test_bass_only_swap_parity(self) -> None:
        """A bass-rich, mid-light pair: only the low/high schedules engage, matching the old planner."""
        out, inc = _bands_pair(0.4, 0.4)
        reference_plan = SmartCrossFadePlanner(LOGGER).plan(out, inc, 45.0)

        ctx = _ctx(out, inc)
        factory = CandidateFactory(ctx, LOGGER)
        candidate = _first_fitting_candidate(ctx, factory)
        new_plan = PlanAssembler(ctx, LOGGER).finalize(candidate)

        assert new_plan.eq_plan.mid_out is None
        assert new_plan.eq_plan.mid_in is None
        assert reference_plan.eq_plan.low_out is not None
        assert new_plan.eq_plan.low_out is not None
        assert new_plan.eq_plan.low_out.steps == pytest.approx(reference_plan.eq_plan.low_out.steps)
        assert reference_plan.eq_plan.low_in is not None
        assert new_plan.eq_plan.low_in is not None
        assert new_plan.eq_plan.low_in.steps == pytest.approx(reference_plan.eq_plan.low_in.steps)


class TestFinalizeCarriesMetrics:
    """The finalized plan exposes the winning candidate's metrics, not defaults."""

    def test_finalized_plan_carries_the_candidate_metrics(self) -> None:
        """finalize() must copy the scored metrics onto the returned plan."""
        out, inc = _rich_pair()
        ctx = build_transition_context(out, inc, 45.0, LOGGER)
        factory = CandidateFactory(ctx, LOGGER)
        candidate = factory.build(CandidateSpec(tier=ctx.tier, bars=8, anchor_s=None, entry_s=None))
        assert candidate is not None

        plan = PlanAssembler(ctx, LOGGER).finalize(candidate)

        assert plan.metrics == candidate.metrics


class TestFinalizeChoosesFadeoutCurve:
    """The crossfade degrades to ``nofade`` only when the overlap sits fully inside a mastered fade."""

    def _candidate(self, ctx: TransitionContext) -> Candidate:
        factory = CandidateFactory(ctx, LOGGER)
        candidate = factory.build(CandidateSpec(tier=ctx.tier, bars=1, anchor_s=None, entry_s=None))
        assert candidate is not None  # the 1-bar rung always yields a candidate
        return candidate

    def test_overlap_entirely_inside_the_fade_uses_nofade(self) -> None:
        """A crossfade that starts after the fade onset and runs to the audible end gets nofade."""
        out, inc = _mastered_fade_pair()
        ctx = _ctx(out, inc)
        assert ctx.fade_onset is not None
        candidate = self._candidate(ctx)
        plan = replace(candidate.plan, fade_out_window=44.0, crossfade_duration=20.0)
        candidate = replace(candidate, plan=plan)

        new_plan = PlanAssembler(ctx, LOGGER).finalize(candidate)

        assert new_plan.fadeout_curve == "nofade"

    def test_no_detected_fade_keeps_qsin(self) -> None:
        """Without a detected mastered fade, the crossfade curve stays the qsin default."""
        out, inc = _rich_pair()
        ctx = _ctx(out, inc)
        assert ctx.fade_onset is None
        candidate = self._candidate(ctx)

        new_plan = PlanAssembler(ctx, LOGGER).finalize(candidate)

        assert new_plan.fadeout_curve == "qsin"

    def test_overlap_straddling_the_fade_onset_keeps_qsin(self) -> None:
        """A crossfade that starts before the fade onset (flat-then-fade) cannot use nofade."""
        out, inc = _mastered_fade_pair()
        ctx = _ctx(out, inc)
        assert ctx.fade_onset is not None
        candidate = self._candidate(ctx)
        plan = replace(candidate.plan, fade_out_window=44.0, crossfade_duration=30.0)
        candidate = replace(candidate, plan=plan)

        new_plan = PlanAssembler(ctx, LOGGER).finalize(candidate)

        assert new_plan.fadeout_curve == "qsin"


class TestFinalizeDipGuardBehavior:
    """The dip-guard repair must match the old planner's remediation exactly."""

    def test_wash_mid_stack_remediation_parity(self) -> None:
        """A stacked wash+mid dip: the mid-depth remediation matches the old planner's shrink."""
        out, inc = _wash_mid_stack_pair()
        reference_plan = SmartCrossFadePlanner(LOGGER).plan(out, inc, 45.0)
        # the fixture is only useful once remediation actually engaged
        gate_depth = -8.0
        assert reference_plan.eq_plan.mid_out is None or reference_plan.eq_plan.mid_out.steps[-1][
            1
        ] != pytest.approx(gate_depth, abs=0.01)

        ctx = _ctx(out, inc)
        factory = CandidateFactory(ctx, LOGGER)
        candidate = _first_fitting_candidate(ctx, factory)
        new_plan = PlanAssembler(ctx, LOGGER).finalize(candidate)

        assert (new_plan.eq_plan.mid_out is None) == (reference_plan.eq_plan.mid_out is None)
        if new_plan.eq_plan.mid_out is not None:
            assert reference_plan.eq_plan.mid_out is not None
            assert new_plan.eq_plan.mid_out.steps == pytest.approx(
                reference_plan.eq_plan.mid_out.steps
            )
        assert (new_plan.eq_plan.low_out is None) == (reference_plan.eq_plan.low_out is None)
        if new_plan.eq_plan.low_out is not None:
            assert reference_plan.eq_plan.low_out is not None
            assert new_plan.eq_plan.low_out.steps == pytest.approx(
                reference_plan.eq_plan.low_out.steps
            )


class TestIncomingAudibilityFloor:
    """A bass-dominant incoming intro keeps enough broadband level under its entry shelf."""

    def test_bass_dominant_incoming_shallows_its_entry_shelf(self) -> None:
        """An 85% low-band incoming entry window floors the kill near the analytic -9.2dB."""
        out, inc = _bands_pair(0.4, 0.85)

        plan = SmartCrossFadePlanner(LOGGER).plan(out, inc, 45.0)

        assert plan.eq_plan.low_in is not None
        # analytic floor: 10*log10((10**-0.6 - 0.15) / 0.85)
        assert plan.eq_plan.low_in.steps[0][1] == pytest.approx(-9.24, abs=0.05)
        assert plan.eq_plan.low_in.steps[-1][1] == pytest.approx(0.0)
        # the outgoing deck's post-swap kill is the handover gesture: untouched
        assert plan.eq_plan.low_out is not None
        assert plan.eq_plan.low_out.steps[-1][1] == pytest.approx(-26.0)

    def test_bass_balanced_incoming_keeps_the_gated_depth(self) -> None:
        """A ~30% low-band incoming window can never fall below the floor: full kill kept."""
        out, inc = _bands_pair(0.4, 0.3)

        plan = SmartCrossFadePlanner(LOGGER).plan(out, inc, 45.0)

        assert plan.eq_plan.low_in is not None
        assert plan.eq_plan.low_in.steps[0][1] == pytest.approx(-26.0)


class TestQuickFadeSkipsEq:
    """A quick fade is a pure volume fade: no bass/high/mid handover shelves at all."""

    def test_quick_fade_plans_a_neutral_eq(self) -> None:
        """A tempo-incompatible bass-heavy pair ships a QUICK_FADE without any shelf."""
        out, inc = _bands_pair(0.6, 0.6)
        inc.bpm = 150.0

        plan = SmartCrossFadePlanner(LOGGER).plan(out, inc, 45.0)

        assert plan.tier is TransitionTier.QUICK_FADE
        eq = plan.eq_plan
        assert eq.low_out is None
        assert eq.low_in is None
        assert eq.high_out is None
        assert eq.high_in is None
        assert eq.mid_out is None
        assert eq.mid_in is None


class TestFallbackCrossfadeOnUnreliableMasks:
    """Saturated (unreliable) masks never push the fallback into deferral or a duck."""

    def test_unreliable_masks_ship_the_fallback_without_a_duck(self) -> None:
        """Wall-to-wall masks read as severe collision, yet the fallback ships duck-free."""
        out = _with_vocal_activity(_analysis(120.0, duration=240.0), [(196.0, 239.9)])
        inc = _with_vocal_activity(_analysis(120.0, duration=240.0), [(0.0, 41.0)])
        ctx = _ctx(out, inc)
        assert not ctx.vocal_collision_reliable
        factory = CandidateFactory(ctx, LOGGER)

        plan = FallbackCrossfadeFactory(ctx, factory, LOGGER).build()

        assert plan is not None
        assert plan.metrics.strategy is TransitionStrategy.FALLBACK_CROSSFADE
        # the metrics really read as severe: only the unreliable flag kept the
        # fallback from deferring to the handoff
        assert plan.metrics.collision_seconds >= 2 * COLLISION_SECONDS_LIMIT
        assert plan.eq_plan.mid_out is None
        assert plan.eq_plan.mid_in is None
        assert plan.fadeout_curve == "qsin"

    def test_fallback_inside_a_mastered_fade_uses_nofade(self) -> None:
        """A fallback overlap sitting entirely inside a detected mastered fade skips the qsin curve."""
        out = _with_vocal_activity(_analysis(120.0, duration=240.0), [(196.0, 239.9)])
        inc = _with_vocal_activity(_analysis(120.0, duration=240.0), [(0.0, 41.0)])
        ctx = replace(_ctx(out, inc), fade_onset=30.0)
        factory = CandidateFactory(ctx, LOGGER)

        plan = FallbackCrossfadeFactory(ctx, factory, LOGGER).build()

        assert plan is not None
        assert plan.fadeout_curve == "nofade"


class TestEmergencyHandoff:
    """The emergency handoff never fails and always lands in the incoming track's vocal gap."""

    def test_duration_lands_in_the_incoming_vocal_gap(self) -> None:
        """A mid-range incoming vocal onset yields a duration just short of it, with neutral EQ."""
        out = _with_vocal_activity(_analysis(120.0, duration=240.0), [(200.0, 239.9)])
        inc = _with_vocal_activity(_analysis(120.0, duration=240.0), [(1.05, 40.0)])
        ctx = _ctx(out, inc)
        factory = CandidateFactory(ctx, LOGGER)
        assert ctx.vocal_in_placement is not None
        assert ctx.vocal_in_placement.windows
        incoming_onset = ctx.vocal_in_placement.windows[0][0]

        plan = EmergencyHandoffFactory(ctx, factory, LOGGER).build()

        assert 0.4 <= plan.crossfade_duration <= 1.0
        # the handoff ends just short of the incoming track's own vocal onset
        assert plan.crossfade_duration == pytest.approx(incoming_onset - 0.1, abs=1e-6)
        assert plan.metrics.strategy is TransitionStrategy.SHORT_VOCAL_HANDOFF
        assert not plan.tempo_plan.steps
        assert plan.fadein_trim_start is None
        eq = plan.eq_plan
        assert eq.low_out is None
        assert eq.low_in is None
        assert eq.high_out is None
        assert eq.high_in is None
        assert eq.mid_out is None
        assert eq.mid_in is None
        assert eq.swap_at == pytest.approx(plan.crossfade_duration / 2.0)

    def test_duration_floors_at_min_handoff_seconds(self) -> None:
        """An incoming vocal onset right at the head clamps the handoff to the 0.4s floor."""
        out = _with_vocal_activity(_analysis(120.0, duration=240.0), [(200.0, 239.9)])
        inc = _with_vocal_activity(_analysis(120.0, duration=240.0), [(0.0, 40.0)])
        ctx = _ctx(out, inc)
        factory = CandidateFactory(ctx, LOGGER)

        plan = EmergencyHandoffFactory(ctx, factory, LOGGER).build()

        assert plan.crossfade_duration == pytest.approx(0.4)
        assert plan.metrics.strategy is TransitionStrategy.SHORT_VOCAL_HANDOFF

    def test_duration_caps_at_max_handoff_seconds(self) -> None:
        """A comfortably late incoming vocal onset clamps the handoff to the 1.0s ceiling."""
        out = _with_vocal_activity(_analysis(120.0, duration=240.0), [(200.0, 239.9)])
        inc = _with_vocal_activity(_analysis(120.0, duration=240.0), [(5.0, 40.0)])
        ctx = _ctx(out, inc)
        factory = CandidateFactory(ctx, LOGGER)

        plan = EmergencyHandoffFactory(ctx, factory, LOGGER).build()

        assert plan.crossfade_duration == pytest.approx(1.0)
        assert plan.metrics.strategy is TransitionStrategy.SHORT_VOCAL_HANDOFF
