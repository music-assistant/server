"""Tests for the smart fades candidate factory."""

from __future__ import annotations

import dataclasses
import logging

import numpy as np
import pytest

from music_assistant.controllers.streams.smart_fades.models import (
    TransitionStyle,
    TransitionTier,
)
from music_assistant.controllers.streams.smart_fades.planner.candidates import (
    Candidate,
    CandidateFactory,
    CandidateSpec,
    SegueGenerator,
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


def _first_fitting(ctx: TransitionContext, factory: CandidateFactory) -> Candidate:
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


class TestBuiltStyle:
    """The factory stamps the style it built on both the spec and the plan."""

    def test_a_blend_tier_builds_a_blend(self) -> None:
        """A beatmatched tier builds the BLEND style."""
        ctx = _ctx(_analysis(120.0), _analysis(122.0))
        candidate = _first_fitting(ctx, CandidateFactory(ctx, LOGGER))

        assert candidate.plan.tier is TransitionTier.FULL_BLEND
        assert candidate.plan.style is TransitionStyle.BLEND
        assert candidate.spec.style is TransitionStyle.BLEND

    def test_a_quick_fade_tier_builds_a_cut(self) -> None:
        """An unsynced quick fade builds the CUT style."""
        ctx = _ctx(_analysis(120.0), _analysis(150.0))
        candidate = _first_fitting(ctx, CandidateFactory(ctx, LOGGER))

        assert candidate.plan.tier is TransitionTier.QUICK_FADE
        assert candidate.plan.style is TransitionStyle.CUT
        assert candidate.spec.style is TransitionStyle.CUT


class TestRhythmClashMetric:
    """The kick clash metric integrates both decks' kick bars under the fade's gain."""

    def test_a_blend_never_clashes(self) -> None:
        """A beatmatched blend scores no clash, however many kicks overlap."""
        out, inc = (
            _analysis_with_bands(0.5, 0.3, 0.3, 0.3),
            _analysis_with_bands(0.5, 0.3, 0.3, 0.3),
        )
        ctx = _ctx(out, inc)
        assert ctx.tier is TransitionTier.FULL_BLEND

        candidate = _first_fitting(ctx, CandidateFactory(ctx, LOGGER))

        assert candidate.plan.style is TransitionStyle.BLEND
        assert candidate.metrics.rhythm_clash_bars == 0.0

    def test_four_bars_of_two_kicks_weigh_two_thirds_of_their_length(self) -> None:
        """Kicks on both decks across a 4-bar cut weigh 2/3 of 4 bars."""
        out, inc = (
            _analysis_with_bands(0.5, 0.3, 0.3, 0.3),
            _analysis_with_bands(0.5, 0.3, 0.3, 0.3),
        )
        inc.bpm = 132.0
        ctx = _ctx(out, inc)
        assert ctx.tier is TransitionTier.QUICK_FADE

        candidate = CandidateFactory(ctx, LOGGER).build(_spec(ctx, 4))

        assert candidate is not None
        assert candidate.plan.style is TransitionStyle.CUT
        assert candidate.plan.crossfade_duration == pytest.approx(8.0)
        assert candidate.metrics.rhythm_clash_bars == pytest.approx(4 * 2 / 3)


class TestTempoRampKickGate:
    """The tempo ramp runs only where both decks kick: the stretch window and the overlap."""

    @staticmethod
    def _ramped_blend() -> tuple[TransitionContext, Candidate]:
        """Build an 8-bar blend 1.7 % apart without band data, which ramps as it always did."""
        ctx = _ctx(_analysis(120.0), _analysis(122.0))
        candidate = CandidateFactory(ctx, LOGGER).build(_spec(ctx, 8))
        assert candidate is not None
        assert candidate.plan.tempo_plan
        return ctx, candidate

    @pytest.mark.parametrize(
        ("kicks_out", "kicks_in"),
        [(None, None), (((0.0, 45.0),), None), (None, ((0.0, 45.0),))],
        ids=["no band data", "outgoing only", "incoming only"],
    )
    def test_missing_band_data_keeps_the_ramp(
        self,
        kicks_out: tuple[tuple[float, float], ...] | None,
        kicks_in: tuple[tuple[float, float], ...] | None,
    ) -> None:
        """
        A deck without band data never blocks the ramp.

        :param kicks_out: Outgoing kick runs, None without a band profile.
        :param kicks_in: Incoming kick runs, None without a band profile.
        """
        ctx, ramped = self._ramped_blend()
        partial = dataclasses.replace(ctx, kick_out=kicks_out, kick_in=kicks_in)

        candidate = CandidateFactory(partial, LOGGER).build(_spec(ctx, 8))

        assert candidate == ramped

    def test_a_blend_with_both_decks_kicked_keeps_its_ramp(self) -> None:
        """Kicks on both decks throughout leave the ramped plan as it was."""
        ctx, ramped = self._ramped_blend()
        kicked = dataclasses.replace(ctx, kick_out=((0.0, 45.0),), kick_in=((0.0, 45.0),))

        candidate = CandidateFactory(kicked, LOGGER).build(_spec(ctx, 8))

        assert candidate == ramped

    @pytest.mark.parametrize(
        "kickless",
        ["outgoing stretch window", "outgoing overlap", "incoming overlap", "incoming graze"],
    )
    def test_a_kickless_side_blends_unstretched(self, kickless: str) -> None:
        """
        A deck without a kick where the ramp plays drops the ramp; a graze under a beat counts.

        :param kickless: Where the kick is missing.
        """
        ctx, ramped = self._ramped_blend()
        plan = ramped.plan
        overlap_start = plan.fade_out_window - plan.crossfade_duration * 122.0 / 120.0
        everywhere = ((0.0, 45.0),)
        kicks_out, kicks_in = {
            "outgoing stretch window": (((overlap_start, 45.0),), everywhere),
            "outgoing overlap": (((0.0, overlap_start),), everywhere),
            "incoming overlap": (everywhere, ((30.0, 45.0),)),
            "incoming graze": (everywhere, ((plan.crossfade_duration - 0.2, 45.0),)),
        }[kickless]
        gated = dataclasses.replace(ctx, kick_out=kicks_out, kick_in=kicks_in)

        candidate = CandidateFactory(gated, LOGGER).build(_spec(ctx, 8))

        assert candidate is not None
        assert candidate.plan.style is TransitionStyle.BLEND
        assert not candidate.plan.tempo_plan

    @pytest.mark.parametrize(("intro_kick", "ramps"), [(0.5, True), (0.02, False)])
    def test_a_beatless_intro_from_band_data_blends_unstretched(
        self, intro_kick: float, ramps: bool
    ) -> None:
        """
        An incoming head without a kick for its first 40 s blends unstretched.

        :param intro_kick: Low-band level of the incoming head.
        :param ramps: Whether the blend ramps its tempo.
        """
        t = np.arange(1800) * (240.0 / 1800)
        inc = _analysis_with_bands(np.where(t < 40.0, intro_kick, 0.5), 0.3, 0.3, 0.3)
        inc.bpm = 122.0
        ctx = _ctx(_analysis_with_bands(0.5, 0.3, 0.3, 0.3), inc)

        candidate = _first_fitting(ctx, CandidateFactory(ctx, LOGGER))

        assert candidate.plan.style is TransitionStyle.BLEND
        assert bool(candidate.plan.tempo_plan) is ramps


def _rms(*segments: tuple[float, float, float]) -> list[float]:
    """Build a flat 0.5 rms envelope over 240s, set to ``value`` per ``(start, end, value)``."""
    t = np.arange(1800) * (240.0 / 1800)
    env = np.full(1800, 0.5, dtype=np.float32)
    for start, end, value in segments:
        env[(t >= start) & (t < end)] = value
    return env.tolist()


def _segue_spec(ctx: TransitionContext, overlap: float) -> CandidateSpec:
    return CandidateSpec(
        tier=ctx.tier,
        bars=1,
        anchor_s=ctx.audio_end,
        entry_s=None,
        style=TransitionStyle.SEGUE,
        overlap_s=overlap,
        ideal_overlap_s=overlap,
    )


class TestBuildSegue:
    """A segue enters the next track the overlap before the audible end, unsynced."""

    def test_a_quiet_tail_plays_as_recorded_under_a_loud_head(self) -> None:
        """A 10s quiet tail ends at the audible end with no stretch, no trim, nofade/qsin."""
        out = _analysis(120.0)
        out.rms_energy = _rms((230.0, 240.0, 0.1))
        ctx = _ctx(out, _analysis(150.0))
        spec = next(iter(SegueGenerator().generate(ctx)))

        candidate = CandidateFactory(ctx, LOGGER).build(spec)

        assert candidate is not None
        plan = candidate.plan
        assert plan.style is TransitionStyle.SEGUE
        assert plan.tier is TransitionTier.QUICK_FADE
        assert plan.fade_out_window == pytest.approx(45.0)
        assert plan.crossfade_duration == pytest.approx(10.0, abs=0.15)
        assert not plan.tempo_plan
        assert plan.fadein_trim_start is None
        assert plan.fadeout_curve == "nofade"
        assert plan.fadein_curve == "qsin"

    def test_a_quiet_head_plays_as_recorded(self) -> None:
        """An incoming head quiet over its first bar fades in with no curve."""
        out = _analysis(120.0)
        out.rms_energy = _rms((230.0, 240.0, 0.1))
        inc = _analysis(150.0)
        inc.rms_energy = _rms((0.0, 4.0, 0.05))
        ctx = _ctx(out, inc)

        candidate = CandidateFactory(ctx, LOGGER).build(_segue_spec(ctx, 14.0))

        assert candidate is not None
        assert candidate.plan.fadein_curve == "nofade"

    def test_an_overlap_past_the_quiet_material_fades_both_sides(self) -> None:
        """Quiet edges play as recorded within the 14s of quiet material, not one step past it."""
        out = _analysis(120.0)
        out.rms_energy = _rms((230.0, 240.0, 0.1))
        inc = _analysis(150.0)
        inc.rms_energy = _rms((0.0, 4.0, 0.05))
        ctx = _ctx(out, inc)
        factory = CandidateFactory(ctx, LOGGER)

        within = factory.build(_segue_spec(ctx, 14.0))
        past = factory.build(_segue_spec(ctx, 16.0))

        assert within is not None
        assert past is not None
        assert (within.plan.fadeout_curve, within.plan.fadein_curve) == ("nofade", "nofade")
        assert (past.plan.fadeout_curve, past.plan.fadein_curve) == ("qsin", "qsin")

    def test_a_long_tail_enters_the_overlap_before_the_audible_end(self) -> None:
        """A 30s quiet tail starts the next track 15s before the audible end."""
        out = _analysis(120.0)
        out.rms_energy = _rms((210.0, 240.0, 0.1))
        ctx = _ctx(out, _analysis(150.0))
        assert ctx.segue is not None

        candidate = CandidateFactory(ctx, LOGGER).build(_segue_spec(ctx, ctx.segue.overlap))

        assert candidate is not None
        plan = candidate.plan
        assert plan.crossfade_duration == pytest.approx(15.0)
        assert plan.fade_out_window - plan.crossfade_duration == pytest.approx(30.0)

    def test_a_loud_tail_inside_a_mastered_fade_is_not_faded_twice(self) -> None:
        """A loud edge fades equal-power, unless the record already fades itself there."""
        ctx = _ctx(_analysis(120.0), _analysis(150.0))
        factory = CandidateFactory(ctx, LOGGER)
        faded = CandidateFactory(dataclasses.replace(ctx, fade_onset=20.0), LOGGER)

        loud = factory.build(_segue_spec(ctx, 15.0))
        mastered = faded.build(_segue_spec(ctx, 15.0))

        assert loud is not None
        assert mastered is not None
        assert loud.plan.fadeout_curve == "qsin"
        assert mastered.plan.fadeout_curve == "nofade"


class TestUnheardIntroClamp:
    """A fade-in trim is bounded by what the overlap plays under the outgoing track."""

    def test_a_trim_deeper_than_the_overlap_is_stripped(self) -> None:
        """A cut skipping more of the incoming track than the blend plays is dropped."""
        out, inc = _analysis(80.0, duration=240.0), _analysis(83.2, duration=240.0)
        ctx = _ctx(out, inc)
        deep = CandidateFactory(ctx, LOGGER).build(_spec(ctx, 8, anchor_s=20.0, entry_s=30.0))
        assert deep is not None
        assert deep.plan.fadein_trim_start is None

    def test_a_trim_the_overlap_covers_is_kept(self) -> None:
        """An entry the blend actually plays under the outgoing track survives."""
        out, inc = _analysis(80.0, duration=240.0), _analysis(83.2, duration=240.0)
        ctx = _ctx(out, inc)
        shallow = CandidateFactory(ctx, LOGGER).build(_spec(ctx, 8, anchor_s=20.0, entry_s=10.0))
        assert shallow is not None
        assert shallow.plan.fadein_trim_start == pytest.approx(10.0, abs=2.0)


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


class TestQuickFadeBarsLadder:
    """The quick-fade overlap length follows the tempo gap, capped short across meters."""

    @pytest.mark.parametrize(
        ("incoming_bpm", "same_meter_bars", "cross_meter_bars"),
        [(126.0, 4, 2), (138.0, 2, 2), (150.0, 1, 1)],
    )
    def test_ladder_top_by_tempo_gap_and_meter(
        self, incoming_bpm: float, same_meter_bars: int, cross_meter_bars: int
    ) -> None:
        """
        A meter mismatch caps the tempo ladder at 2 bars instead of replacing it.

        :param incoming_bpm: Incoming tempo against a 120 BPM outgoing track (5/15/25 % gap).
        :param same_meter_bars: Expected top rung when both tracks are in 4/4.
        :param cross_meter_bars: Expected top rung when the incoming track is in 3/4.
        """
        out = _analysis(120.0)
        same_meter = _ctx(out, _analysis(incoming_bpm))
        inc_three_four = _analysis(incoming_bpm)
        inc_three_four.beats_per_bar = 3
        cross_meter = _ctx(out, inc_three_four)

        assert not same_meter.cross_meter
        assert cross_meter.cross_meter
        assert bars_ladder(same_meter, TransitionTier.QUICK_FADE)[0] == same_meter_bars
        assert bars_ladder(cross_meter, TransitionTier.QUICK_FADE)[0] == cross_meter_bars
