"""Tests for PlanAssembler and EmergencyHandoffFactory."""

from __future__ import annotations

import logging

import numpy as np
import pytest

from music_assistant.controllers.streams.smart_fades.models import TransitionStrategy
from music_assistant.controllers.streams.smart_fades.planner import SmartCrossFadePlanner
from music_assistant.controllers.streams.smart_fades.planner.assembly import (
    EmergencyHandoffFactory,
    PlanAssembler,
)
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


def _first_fitting_candidate(ctx: TransitionContext, factory: CandidateFactory) -> object:
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


class TestFinalizeEqParityWithOldPlanner:
    """``finalize()`` must yield the same EqPlan the old planner computes for the equivalent plan."""

    def test_full_blend_bass_and_mid_swap_parity(self) -> None:
        """A bass-rich and mid-heavy pair: low, mid and high schedules all match the old planner."""
        out, inc = _rich_pair()
        old_plan = SmartCrossFadePlanner(LOGGER).plan(out, inc, 45.0)

        ctx = _ctx(out, inc)
        factory = CandidateFactory(ctx, LOGGER)
        candidate = _first_fitting_candidate(ctx, factory)
        new_plan = PlanAssembler(ctx, LOGGER).finalize(candidate)

        assert new_plan.eq_plan.swap_at == pytest.approx(old_plan.eq_plan.swap_at)
        for attr in ("low_out", "low_in", "high_out", "high_in", "mid_out", "mid_in"):
            old_sched = getattr(old_plan.eq_plan, attr)
            new_sched = getattr(new_plan.eq_plan, attr)
            assert (old_sched is None) == (new_sched is None), attr
            if old_sched is not None:
                assert new_sched.steps == pytest.approx(old_sched.steps), attr
                assert new_sched.shelf_type == old_sched.shelf_type
                assert new_sched.frequency == old_sched.frequency

    def test_bass_only_swap_parity(self) -> None:
        """A bass-rich, mid-light pair: only the low/high schedules engage, matching the old planner."""
        out, inc = _bands_pair(0.4, 0.4)
        old_plan = SmartCrossFadePlanner(LOGGER).plan(out, inc, 45.0)

        ctx = _ctx(out, inc)
        factory = CandidateFactory(ctx, LOGGER)
        candidate = _first_fitting_candidate(ctx, factory)
        new_plan = PlanAssembler(ctx, LOGGER).finalize(candidate)

        assert new_plan.eq_plan.mid_out is None
        assert new_plan.eq_plan.mid_in is None
        assert old_plan.eq_plan.low_out is not None
        assert new_plan.eq_plan.low_out is not None
        assert new_plan.eq_plan.low_out.steps == pytest.approx(old_plan.eq_plan.low_out.steps)
        assert old_plan.eq_plan.low_in is not None
        assert new_plan.eq_plan.low_in is not None
        assert new_plan.eq_plan.low_in.steps == pytest.approx(old_plan.eq_plan.low_in.steps)


class TestFinalizeDipGuardParity:
    """The dip-guard repair must match the old planner's remediation exactly."""

    def test_wash_mid_stack_remediation_parity(self) -> None:
        """A stacked wash+mid dip: the mid-depth remediation matches the old planner's shrink."""
        out, inc = _wash_mid_stack_pair()
        old_plan = SmartCrossFadePlanner(LOGGER).plan(out, inc, 45.0)
        # the fixture is only useful once remediation actually engaged
        gate_depth = -8.0
        assert old_plan.eq_plan.mid_out is None or old_plan.eq_plan.mid_out.steps[-1][
            1
        ] != pytest.approx(gate_depth, abs=0.01)

        ctx = _ctx(out, inc)
        factory = CandidateFactory(ctx, LOGGER)
        candidate = _first_fitting_candidate(ctx, factory)
        new_plan = PlanAssembler(ctx, LOGGER).finalize(candidate)

        assert (new_plan.eq_plan.mid_out is None) == (old_plan.eq_plan.mid_out is None)
        if new_plan.eq_plan.mid_out is not None:
            assert old_plan.eq_plan.mid_out is not None
            assert new_plan.eq_plan.mid_out.steps == pytest.approx(old_plan.eq_plan.mid_out.steps)
        assert (new_plan.eq_plan.low_out is None) == (old_plan.eq_plan.low_out is None)
        if new_plan.eq_plan.low_out is not None:
            assert old_plan.eq_plan.low_out is not None
            assert new_plan.eq_plan.low_out.steps == pytest.approx(old_plan.eq_plan.low_out.steps)


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
