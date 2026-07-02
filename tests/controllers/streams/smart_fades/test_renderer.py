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
    FrequencySweepFilter,
    GradualTimeStretchFilter,
)
from music_assistant.controllers.streams.smart_fades.models import (
    EqPlan,
    FadeOutTrim,
    SweepSpec,
    TempoPlan,
    TransitionPlan,
)
from music_assistant.controllers.streams.smart_fades.renderer import TransitionRenderer

LOGGER = logging.getLogger(__name__)
PCM = AudioFormat(content_type=ContentType.PCM_S16LE, sample_rate=44100, bit_depth=16, channels=2)


def _seconds(seconds: float) -> int:
    return int(seconds * PCM.pcm_sample_size)


def _eq_plan() -> EqPlan:
    return EqPlan(
        crossover_freq=2000,
        fadeout=SweepSpec("lowpass", 2000, 20.0, 20.0, "fade_in", 1, "logarithmic", "fadeout"),
        fadein=SweepSpec("highpass", 2000, 6.67, 0, "fade_out", 1, "linear", "fadein"),
    )


def _plan(**overrides: object) -> TransitionPlan:
    defaults: dict[str, object] = {
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
            GradualTimeStretchFilter,
            FadeInTrimFilter,
            FrequencySweepFilter,
            FrequencySweepFilter,
            CrossfadeFilter,
        ]

    def test_minimal_chain_is_sweeps_then_crossfade(self) -> None:
        """With no trims and no stretch, only the sweeps and crossfade render."""
        filters, _ = TransitionRenderer(LOGGER).render(_plan(), PCM, _seconds(45))
        assert [type(f) for f in filters] == [
            FrequencySweepFilter,
            FrequencySweepFilter,
            CrossfadeFilter,
        ]

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

    def test_stretch_savings_shorten_fadeout_accounting(self) -> None:
        """A speed-up ramp removes time from the rendered fade-out total."""
        plan = _plan(tempo_plan=TempoPlan(steps=[(30.0, 1.0), (35.0, 1.02)]))
        _, timing = TransitionRenderer(LOGGER).render(plan, PCM, _seconds(45))
        expected_fade_out = 40.0 - 5.0 * (1.0 - 1.0 / 1.02)
        assert timing.pre_crossfade_duration + timing.crossfade_duration == pytest.approx(
            expected_fade_out
        )
