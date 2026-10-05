"""Tests for the smart-fades transition plan value objects."""

from __future__ import annotations

import pytest

from music_assistant.controllers.streams.smart_fades.models import (
    EqPlan,
    TempoPlan,
    TransitionPlan,
    TransitionTier,
)


class TestTempoPlan:
    """Cover the TempoPlan stretch-savings integral and truthiness."""

    def test_empty_plan_is_falsy_and_saves_nothing(self) -> None:
        """An empty ramp stretches no time."""
        plan = TempoPlan()
        assert not plan
        assert plan.savings_until(45.0) == 0.0

    def test_non_empty_plan_is_truthy(self) -> None:
        """A plan with steps is truthy so renderers gate on it directly."""
        assert TempoPlan(steps=[(0.0, 1.05)])

    def test_speed_up_saves_positive_time(self) -> None:
        """A ratio > 1 (faster) removes time from the rendered stream."""
        plan = TempoPlan(steps=[(35.0, 1.0), (40.0, 1.05)])
        # ratio-1.0 segment [35,40] saves nothing; [40,45] runs at 1.05
        assert plan.savings_until(45.0) == pytest.approx(5.0 * (1.0 - 1.0 / 1.05))
        assert plan.savings_until(40.0) == 0.0

    def test_slow_down_lengthens_stream(self) -> None:
        """A ratio < 1 (slower) yields negative savings (stream lengthened)."""
        plan = TempoPlan(steps=[(35.0, 1.0), (40.0, 0.95)])
        assert plan.savings_until(45.0) == pytest.approx(5.0 * (1.0 - 1.0 / 0.95))

    def test_first_step_after_zero_stretches_from_start(self) -> None:
        """Rubberband starts at the first step's ratio, so the pre-step span is stretched."""
        plan = TempoPlan(steps=[(20.0, 1.004)])
        assert plan.savings_until(10.0) == pytest.approx(10.0 * (1.0 - 1.0 / 1.004))
        assert plan.savings_until(45.0) == pytest.approx(45.0 * (1.0 - 1.0 / 1.004))


def test_transition_plan_defaults_to_neutral_eq() -> None:
    """TransitionPlan can be created without eq_plan and defaults to neutral."""
    plan = TransitionPlan(
        tier=TransitionTier.QUICK_FADE, fade_out_window=10.0, crossfade_duration=5.0
    )
    assert plan.eq_plan.low_out is None
    assert plan.eq_plan.mid_out is None


def test_eq_plan_neutral_factory() -> None:
    """EqPlan.neutral() factory creates a plan with all schedules None."""
    eq = EqPlan.neutral(swap_at=2.5)
    assert eq.swap_at == 2.5
    assert all(
        s is None for s in (eq.low_out, eq.low_in, eq.high_out, eq.high_in, eq.mid_out, eq.mid_in)
    )
