"""Tests for TransitionRenderer — plan to filter-chain + timing."""

from __future__ import annotations

import logging

import pytest
from music_assistant_models.enums import ContentType
from music_assistant_models.media_items import AudioFormat

from music_assistant.controllers.streams.smart_fades.filters import (
    CrossfadeFilter,
    FadeInTrimFilter,
    FadeOutTrimFilter,
    GradualTimeStretchFilter,
    PeakFilter,
    ShelfFilter,
    ShelfType,
)
from music_assistant.controllers.streams.smart_fades.models import (
    EqPlan,
    FadeOutTrim,
    ShelfSchedule,
    TempoPlan,
    TransitionPlan,
    TransitionTier,
)
from music_assistant.controllers.streams.smart_fades.renderer import TransitionRenderer

LOGGER = logging.getLogger(__name__)
PCM = AudioFormat(content_type=ContentType.PCM_S16LE, sample_rate=44100, bit_depth=16, channels=2)


def _seconds(seconds: float) -> int:
    return int(seconds * PCM.pcm_sample_size)


def _eq_plan() -> EqPlan:
    return EqPlan(
        swap_at=6.0,
        low_out=ShelfSchedule(ShelfType.LOW, 100, [(0.0, 0.0), (36.0, -26.0)]),
        low_in=ShelfSchedule(ShelfType.LOW, 100, [(0.0, -26.0), (7.0, 0.0)]),
        high_out=ShelfSchedule(ShelfType.HIGH, 13000, [(0.0, 0.0), (38.0, -20.0)]),
        high_in=ShelfSchedule(ShelfType.HIGH, 13000, [(0.0, -20.0), (5.0, 0.0)]),
    )


def _plan(**overrides: object) -> TransitionPlan:
    defaults: dict[str, object] = {
        "tier": TransitionTier.FULL_BLEND,
        "fade_out_window": 40.0,
        "crossfade_duration": 10.0,
        "eq_plan": _eq_plan(),
    }
    defaults.update(overrides)
    return TransitionPlan(**defaults)  # type: ignore[arg-type]


class TestTransitionRenderer:
    """The renderer assembles the filter chain and timing from a plan."""

    def test_full_chain_order(self) -> None:
        """Every optional stage present renders in the fixed chain order."""
        plan = _plan(
            tempo_plan=TempoPlan(steps=[(30.0, 1.0), (35.0, 1.02)]),
            fadeout_trim=FadeOutTrim(end_pos=40.0, trimmed_seconds=5.0),
            fadein_trim_start=1.0,
        )
        filters, _ = TransitionRenderer(LOGGER).render(plan, PCM, _seconds(45))
        assert [type(f) for f in filters] == [
            FadeOutTrimFilter,
            ShelfFilter,  # A low (pre-stretch: schedules stay in musical input time)
            ShelfFilter,  # A high
            GradualTimeStretchFilter,
            FadeInTrimFilter,
            ShelfFilter,  # B low
            ShelfFilter,  # B high
            CrossfadeFilter,
        ]

    def test_minimal_chain_is_shelves_then_crossfade(self) -> None:
        """With no trims and no stretch, only the four shelves and crossfade render."""
        filters, _ = TransitionRenderer(LOGGER).render(_plan(), PCM, _seconds(45))
        assert [type(f) for f in filters] == [
            ShelfFilter,
            ShelfFilter,
            ShelfFilter,
            ShelfFilter,
            CrossfadeFilter,
        ]

    def test_bypassed_low_shelves_are_skipped(self) -> None:
        """A bypassed (None) low shelf on either side emits no ShelfFilter for it."""
        eq_plan = _eq_plan()
        eq_plan.low_out = None
        eq_plan.low_in = None
        filters, _ = TransitionRenderer(LOGGER).render(_plan(eq_plan=eq_plan), PCM, _seconds(45))
        assert [type(f) for f in filters] == [
            ShelfFilter,  # A high
            ShelfFilter,  # B high
            CrossfadeFilter,
        ]

    def test_all_shelves_bypassed_leaves_only_crossfade(self) -> None:
        """When every shelf is bypassed the chain is a pure crossfade."""
        eq_plan = _eq_plan()
        eq_plan.low_out = None
        eq_plan.low_in = None
        eq_plan.high_out = None
        eq_plan.high_in = None
        filters, _ = TransitionRenderer(LOGGER).render(_plan(eq_plan=eq_plan), PCM, _seconds(45))
        assert [type(f) for f in filters] == [CrossfadeFilter]

    def test_timing_accounts_for_both_tracks(self) -> None:
        """PRE+CF spans the fade-out, TRIM+CF+POST spans the fade-in."""
        plan = _plan(fadein_trim_start=1.0)
        _, timing = TransitionRenderer(LOGGER).render(plan, PCM, _seconds(45))
        # no stretch -> fade_out_seconds == fade_out_window
        assert timing.pre_crossfade_duration + timing.crossfade_duration == pytest.approx(40.0)
        assert (
            timing.fadein_trimmed_duration
            + timing.crossfade_duration
            + timing.post_crossfade_duration
            == pytest.approx(45.0)
        )
        assert timing.fadein_trimmed_duration == pytest.approx(1.0)

    def test_timing_clamps_crossfade_to_short_fadein(self) -> None:
        """A short incoming buffer clamps the crossfade so POST never goes negative."""
        plan = _plan(crossfade_duration=10.0, fadein_trim_start=1.0)
        _, timing = TransitionRenderer(LOGGER).render(plan, PCM, _seconds(5))
        assert timing.crossfade_duration == pytest.approx(4.0)
        assert timing.post_crossfade_duration == 0.0

    def test_short_fadein_clamps_acrossfade_overlap(self) -> None:
        """
        Regression: the acrossfade overlap must never exceed the audio it is fed.

        acrossfade silently emits nothing (a hard cut) when its requested overlap
        exceeds the incoming buffer, so the filter must use the same clamped,
        frame-exact overlap as the timing info.
        """
        plan = _plan(crossfade_duration=10.0, fadein_trim_start=1.0)
        filters, timing = TransitionRenderer(LOGGER).render(plan, PCM, _seconds(5))
        crossfade = filters[-1]
        assert isinstance(crossfade, CrossfadeFilter)
        # 5s buffer minus the 1s trim leaves 4s of incoming audio
        assert crossfade.crossfade_samples == 4 * PCM.sample_rate
        assert timing.crossfade_duration == pytest.approx(4.0)

    def test_full_buffers_render_plan_duration_as_samples(self) -> None:
        """With full buffers the filter carries the plan duration, frame-exact."""
        filters, timing = TransitionRenderer(LOGGER).render(_plan(), PCM, _seconds(45))
        crossfade = filters[-1]
        assert isinstance(crossfade, CrossfadeFilter)
        assert crossfade.crossfade_samples == 10 * PCM.sample_rate
        assert timing.crossfade_duration == pytest.approx(10.0)

    def test_fadeout_curve_flows_into_the_crossfade_filter(self) -> None:
        """The plan's fadeout_curve becomes the acrossfade filter's c1 curve."""
        plan = _plan(fadeout_curve="nofade")
        filters, _ = TransitionRenderer(LOGGER).render(plan, PCM, _seconds(45))
        crossfade = filters[-1]
        assert isinstance(crossfade, CrossfadeFilter)
        assert "c1=nofade" in crossfade.apply("[fadein]", "[fadeout]")[0]

    def test_stretch_savings_shorten_fadeout_accounting(self) -> None:
        """A speed-up ramp removes time from the rendered fade-out total."""
        plan = _plan(tempo_plan=TempoPlan(steps=[(30.0, 1.0), (35.0, 1.02)]))
        _, timing = TransitionRenderer(LOGGER).render(plan, PCM, _seconds(45))
        expected_fade_out = 40.0 - 5.0 * (1.0 - 1.0 / 1.02)
        assert timing.pre_crossfade_duration + timing.crossfade_duration == pytest.approx(
            expected_fade_out
        )


class TestMidSwapRendering:
    """A PEAK ShelfSchedule (mid swap) renders as a PeakFilter; None is skipped."""

    def test_peak_schedule_renders_as_peak_filter(self) -> None:
        """Populated mid_out/mid_in schedules render as PeakFilter instances."""
        eq_plan = _eq_plan()
        eq_plan.mid_out = ShelfSchedule(ShelfType.PEAK, 1200, [(0.0, 0.0), (36.0, -8.0)])
        eq_plan.mid_in = ShelfSchedule(ShelfType.PEAK, 1200, [(0.0, -8.0), (7.0, 0.0)])
        filters, _ = TransitionRenderer(LOGGER).render(_plan(eq_plan=eq_plan), PCM, _seconds(45))
        assert [type(f) for f in filters] == [
            ShelfFilter,  # A low
            ShelfFilter,  # A high
            PeakFilter,  # A mid
            ShelfFilter,  # B low
            ShelfFilter,  # B high
            PeakFilter,  # B mid
            CrossfadeFilter,
        ]

    def test_mid_none_is_skipped(self) -> None:
        """A bypassed (None) mid schedule emits no PeakFilter."""
        filters, _ = TransitionRenderer(LOGGER).render(_plan(), PCM, _seconds(45))
        assert not any(isinstance(f, PeakFilter) for f in filters)
