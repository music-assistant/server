"""Planner scenarios for the loudness segue and its drum clash check."""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Sequence

import numpy as np
import pytest

from music_assistant.controllers.streams.smart_fades.models import (
    TransitionPlan,
    TransitionStyle,
    TransitionTier,
)
from music_assistant.controllers.streams.smart_fades.planner import SmartCrossFadePlanner, planner
from music_assistant.controllers.streams.smart_fades.planner.candidates import (
    Candidate,
    CandidateFactory,
    SegueGenerator,
    default_generators,
)
from music_assistant.controllers.streams.smart_fades.planner.context import (
    TransitionContext,
    build_transition_context,
)
from music_assistant.controllers.streams.smart_fades.planner.policies import (
    Policy,
    RhythmClashPolicy,
    Verdict,
    default_policies,
)
from music_assistant.controllers.streams.smart_fades.planner.selection import (
    CandidateSelector,
    ScoredCandidate,
)
from music_assistant.models.audio_analysis import AudioAnalysisData

LOGGER = logging.getLogger(__name__)
DURATION = 240.0
# the outgoing buffer starts here for a 45s buffer on a 240s track
OFFSET = DURATION - 45.0


def _envelope(base: float, *segments: tuple[float, float, float]) -> list[float]:
    """Build an 1800-bin envelope at ``base``, set to ``value`` per ``(start, end, value)``."""
    t = np.arange(1800) * (DURATION / 1800)
    env = np.full(1800, base, dtype=np.float32)
    for start, end, value in segments:
        env[(t >= start) & (t < end)] = value
    return env.tolist()


def _vocals(*windows: tuple[float, float]) -> list[float]:
    """Build a validated vocal-activity timeline that sings only inside ``windows``."""
    t = np.arange(1800) * (DURATION / 1800)
    probabilities = np.full(1800, 0.05, dtype=np.float32)
    for start, end in windows:
        probabilities[(t >= start) & (t < end)] = 0.9
    return probabilities.tolist()


def _track(
    bpm: float,
    *,
    rms: list[float] | None = None,
    low: list[float] | None = None,
    vocals: list[float] | None = None,
    key: str = "A",
) -> AudioAnalysisData:
    """Build a 240s analysis row with a regular grid, band envelopes and optional vocals."""
    beats = np.arange(0.0, DURATION, 60.0 / bpm, dtype=np.float32)
    other = _envelope(0.3)
    return AudioAnalysisData(
        duration=DURATION,
        bpm=bpm,
        beats=beats.tolist(),
        downbeats=beats[::4].tolist(),
        beats_per_bar=4,
        rms_energy=rms if rms is not None else _envelope(0.5),
        key=key,
        mode="minor",
        band_rms_low=low if low is not None else _envelope(0.5),
        band_rms_low_mid=other,
        band_rms_mid=other,
        band_rms_high=other,
        vocal_activity=vocals,
    )


def _quiet_tail_out(*, kick_in_tail: bool = False, sings: bool = False) -> AudioAnalysisData:
    """Build an outgoing track whose last 12s sit 14 dB down; its kick stops with the loud part."""
    return _track(
        120.0,
        rms=_envelope(0.5, (228.0, DURATION, 0.1)),
        low=_envelope(0.5) if kick_in_tail else _envelope(0.5, (228.0, DURATION, 0.01)),
        vocals=_vocals((200.0, DURATION)) if sings else _vocals(),
    )


def _plan(
    fade_out: AudioAnalysisData, fade_in: AudioAnalysisData, buffer: float = 45.0
) -> TransitionPlan:
    return SmartCrossFadePlanner(LOGGER).plan(fade_out, fade_in, buffer)


def _plan_without_segue(
    monkeypatch: pytest.MonkeyPatch,
    fade_out: AudioAnalysisData,
    fade_in: AudioAnalysisData,
    buffer: float = 45.0,
) -> TransitionPlan:
    """Plan the pair as the planner did before the segue existed."""
    with monkeypatch.context() as patch:
        patch.setattr(
            planner,
            "default_generators",
            lambda: tuple(g for g in default_generators() if g.name != SegueGenerator.name),
        )
        patch.setattr(SegueGenerator, "generate", lambda _self, _ctx: iter(()))
        return _plan(fade_out, fade_in, buffer)


def _main_pass(
    monkeypatch: pytest.MonkeyPatch, fade_out: AudioAnalysisData, fade_in: AudioAnalysisData
) -> tuple[list[ScoredCandidate], ScoredCandidate | None]:
    """Plan a pair and return the main pass's scoreboard and winner."""
    passes: list[tuple[list[ScoredCandidate], ScoredCandidate | None]] = []
    select, score = CandidateSelector.select, CandidateSelector._score

    def keep_select(
        selector: CandidateSelector, built: Sequence[Candidate], ctx: TransitionContext
    ) -> ScoredCandidate | None:
        passes.append(([], None))
        winner = select(selector, built, ctx)
        passes[-1] = (passes[-1][0], winner)
        return winner

    def keep_score(
        selector: CandidateSelector, candidate: Candidate, ctx: TransitionContext
    ) -> ScoredCandidate:
        entry = score(selector, candidate, ctx)
        passes[-1][0].append(entry)
        return entry

    monkeypatch.setattr(CandidateSelector, "select", keep_select)
    monkeypatch.setattr(CandidateSelector, "_score", keep_score)
    _plan(fade_out, fade_in)
    return passes[0]


class TestSegueScenarios:
    """A pair that can't be beatmatched overlaps what is quiet, unless vocals or kicks clash."""

    def test_one_sided_vocals_get_a_long_segue(self) -> None:
        """A sung quiet tail into an instrumental head 20% apart segues over the whole tail."""
        plan = _plan(_quiet_tail_out(sings=True), _track(144.0, vocals=_vocals()))

        assert plan.style is TransitionStyle.SEGUE
        assert plan.tier is TransitionTier.QUICK_FADE
        assert plan.crossfade_duration >= 10.0
        assert plan.fade_out_window == pytest.approx(45.0)
        assert plan.fadeout_curve == "nofade"
        assert plan.fadein_curve == "qsin"
        assert not plan.tempo_plan
        assert plan.fadein_trim_start is None

    def test_beatless_intro_gets_a_long_segue_over_loud_ends(self) -> None:
        """A kickless incoming head 30% apart rides a 15s equal-power segue with its handover EQ."""
        out = _track(120.0, vocals=_vocals((100.0, DURATION)))
        inc = _track(156.0, low=_envelope(0.5, (0.0, 20.0, 0.01)), vocals=_vocals())

        plan = _plan(out, inc)

        assert plan.style is TransitionStyle.SEGUE
        assert plan.crossfade_duration == pytest.approx(15.0)
        assert (plan.fadeout_curve, plan.fadein_curve) == ("qsin", "qsin")
        assert plan.eq_plan.low_out is not None

    def test_kick_against_kick_with_loud_ends_keeps_todays_cut(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two loud kicked ends 25% apart emit no segue and ship today's cut, as long as today."""
        out, inc = _track(120.0), _track(150.0)
        ctx = build_transition_context(out, inc, 45.0, LOGGER)
        assert list(SegueGenerator().generate(ctx)) == []

        plan = _plan(out, inc)

        assert plan.style is TransitionStyle.CUT
        assert (
            plan.crossfade_duration == _plan_without_segue(monkeypatch, out, inc).crossfade_duration
        )

    def test_a_kicked_quiet_tail_shrinks_below_the_drum_limit_or_loses(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A quiet tail that keeps its kick over a kicked head never ships above 2 clash bars."""
        out, inc = _quiet_tail_out(kick_in_tail=True), _track(150.0)

        scored, winner = _main_pass(monkeypatch, out, inc)

        segues = [e for e in scored if e.candidate.plan.style is TransitionStyle.SEGUE]
        longest = max(segues, key=lambda e: e.candidate.plan.crossfade_duration)
        assert longest.rejected
        assert any(v.reason == "kick clash exceeds the guard limit" for v in longest.verdicts)
        assert winner is not None
        if winner.candidate.plan.style is TransitionStyle.SEGUE:
            assert winner.candidate.metrics.rhythm_clash_bars <= 2.0

    def test_a_long_quiet_tail_caps_the_segue_at_the_audible_end(self) -> None:
        """A 30s quiet tail segues over its last 15s, the next track entering 15s before the end."""
        out = _track(
            120.0,
            rms=_envelope(0.5, (210.0, DURATION, 0.1)),
            low=_envelope(0.5, (210.0, DURATION, 0.01)),
        )

        plan = _plan(out, _track(150.0))

        assert plan.style is TransitionStyle.SEGUE
        assert plan.crossfade_duration == pytest.approx(15.0)
        assert plan.fade_out_window - plan.crossfade_duration == pytest.approx(30.0)

    def test_a_short_buffer_keeps_the_segue_within_its_room(self) -> None:
        """With 20s of outgoing room the segue still fits the buffer."""
        plan = _plan(_quiet_tail_out(), _track(150.0), buffer=20.0)

        assert plan.style is TransitionStyle.SEGUE
        assert plan.fade_out_window <= 20.0
        assert plan.crossfade_duration <= plan.fade_out_window

    def test_a_quiet_musical_outro_segues_instead_of_giving_up(self) -> None:
        """An outro under the mix-out floor for the whole buffer segues over its last 15s."""
        out = _track(
            120.0,
            rms=_envelope(0.5, (190.0, DURATION, 0.15)),
            low=_envelope(0.5, (190.0, DURATION, 0.01)),
        )

        plan = _plan(out, _track(150.0))

        assert plan.style is TransitionStyle.SEGUE
        assert plan.fade_out_window == pytest.approx(45.0)
        assert plan.crossfade_duration == pytest.approx(15.0)
        assert plan.fadeout_curve == "nofade"

    def test_a_twelve_second_buffer_still_plans_within_its_room(self) -> None:
        """With only 12s of outgoing room the quiet tail fills it and the plan fits."""
        plan = _plan(_quiet_tail_out(), _track(150.0), buffer=12.0)

        assert plan.style is TransitionStyle.SEGUE
        assert plan.fade_out_window <= 12.0
        assert plan.crossfade_duration <= plan.fade_out_window

    def test_a_segue_with_no_rival_in_the_main_pass_wins_the_rescue_pass(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When every other main-pass candidate is rejected, the rescue pass measures the segue."""
        out = _track(
            120.0,
            rms=_envelope(0.5, (236.0, DURATION, 0.1)),
            low=_envelope(0.5, (236.0, DURATION, 0.01)),
        )
        winners: list[ScoredCandidate | None] = []
        select = CandidateSelector.select

        def keep_winner(
            selector: CandidateSelector, built: Sequence[Candidate], ctx: TransitionContext
        ) -> ScoredCandidate | None:
            winners.append(select(selector, built, ctx))
            return winners[-1]

        monkeypatch.setattr(CandidateSelector, "select", keep_winner)

        plan = _plan(out, _track(150.0))

        assert winners[0] is None
        assert plan.style is TransitionStyle.SEGUE
        assert plan.crossfade_duration == pytest.approx(4.0, abs=0.15)

    def test_saturated_vocals_on_both_decks_keep_todays_plan(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Wall-to-wall vocals on both decks get no segue: today's plan ships unchanged."""
        out = _quiet_tail_out()
        out.vocal_activity = _vocals((0.0, DURATION))
        inc = _track(150.0, vocals=_vocals((0.0, DURATION)))
        ctx = build_transition_context(out, inc, 45.0, LOGGER)
        assert not ctx.vocal_collision_reliable
        # the quiet tail alone would make segue material
        assert list(
            SegueGenerator().generate(dataclasses.replace(ctx, vocal_collision_reliable=True))
        )
        assert list(SegueGenerator().generate(ctx)) == []

        plan = _plan(out, inc)

        assert plan.style is not TransitionStyle.SEGUE
        assert plan == _plan_without_segue(monkeypatch, out, inc)

    def test_simultaneous_vocals_never_outlast_todays_plan(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both decks singing into each other keep today's (short) handover."""
        out = _quiet_tail_out(sings=True)
        inc = _track(150.0, vocals=_vocals((0.0, 30.0)))

        plan = _plan(out, inc)

        today = _plan_without_segue(monkeypatch, out, inc)
        assert plan.crossfade_duration <= today.crossfade_duration
        assert plan.style is not TransitionStyle.SEGUE


class TestBeatmatchablePairsKeepTheirBlend:
    """A pair the planner can beatmatch ships today's blend; the segue only joins the rescue."""

    @pytest.mark.parametrize("kickless_in", [False, True])
    def test_a_rhythmic_pair_stays_a_blend(
        self, monkeypatch: pytest.MonkeyPatch, kickless_in: bool
    ) -> None:
        """A same-tempo pair blends exactly as today, also when one side has no kick."""
        out = _quiet_tail_out()
        inc = _track(120.0, low=_envelope(0.5, (0.0, 20.0, 0.01)) if kickless_in else None)
        ctx = build_transition_context(out, inc, 45.0, LOGGER)
        assert ctx.tier is TransitionTier.FULL_BLEND
        assert ctx.in_kickless is kickless_in
        assert list(SegueGenerator().generate(ctx)) == []

        plan = _plan(out, inc)

        assert plan.style is TransitionStyle.BLEND
        assert plan == _plan_without_segue(monkeypatch, out, inc)

    def test_a_grid_that_blends_only_later_keeps_its_blend(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A grid that only blends at the audible end keeps that blend over a longer segue."""
        # the kick dies too early to blend at the kick anchor; the 12.7s blend at the
        # audible end is shorter than the 15s segue the kickless tail allows
        out = _track(
            170.0,
            rms=_envelope(0.5, (225.0, DURATION, 0.2)),
            low=_envelope(1.0, (204.5, DURATION, 0.05)),
            vocals=_vocals(),
        )
        inc = _track(170.0, vocals=_vocals(), key="F#")
        ctx = build_transition_context(out, inc, 45.0, LOGGER)
        assert ctx.tier is TransitionTier.QUICK_FADE
        assert next(iter(SegueGenerator().generate(ctx))).overlap_s == pytest.approx(15.0)

        plan = _plan(out, inc)

        assert plan.style is TransitionStyle.BLEND
        assert plan.crossfade_duration < 15.0
        assert plan == _plan_without_segue(monkeypatch, out, inc)

    def test_a_segue_wins_the_rescue_pass_when_every_blend_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With every blend rejected, the segue outscores the fallback crossfade."""

        class _RejectBlends(Policy):
            def evaluate(self, candidate: Candidate, ctx: TransitionContext) -> Verdict:
                if candidate.plan.style is TransitionStyle.BLEND:
                    return Verdict.reject("blend rejected")
                return Verdict.ok()

        monkeypatch.setattr(
            planner, "default_policies", lambda: (*default_policies(), _RejectBlends())
        )

        plan = _plan(_quiet_tail_out(), _track(120.0))

        assert plan.style is TransitionStyle.SEGUE
        assert plan.tier is TransitionTier.FULL_BLEND


class TestSegueOrdering:
    """The documented order between segues and the other styles."""

    def test_a_clean_segue_beats_a_clean_top_rung_cut(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """In a quick fade context the clash-free segue wins, and no tie decides it."""
        scored, winner = _main_pass(monkeypatch, _quiet_tail_out(), _track(150.0))

        assert winner is not None
        assert winner.candidate.plan.style is TransitionStyle.SEGUE
        assert winner.total_penalty == pytest.approx(0.0)
        others = [e.total_penalty for e in scored if not e.rejected and e is not winner]
        assert min(others) > winner.total_penalty
        cuts = [e for e in scored if e.candidate.plan.style is TransitionStyle.CUT]
        assert min(e.total_penalty for e in cuts if not e.rejected) >= 15.0

    def test_a_halving_costs_four(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Between two clash-free segues, the one half as long costs 4 more."""
        scored, _winner = _main_pass(monkeypatch, _quiet_tail_out(), _track(150.0))

        segues = {
            round(e.candidate.plan.crossfade_duration, 2): e.total_penalty
            for e in scored
            if e.candidate.plan.style is TransitionStyle.SEGUE and not e.rejected
        }
        assert segues[4.0] - segues[8.0] == pytest.approx(4.0)


def test_rhythm_clash_judges_the_full_length_segue_of_a_kicked_tail() -> None:
    """The drum check rejects the longest segue over two kicks and keeps the shortest."""
    out, inc = _quiet_tail_out(kick_in_tail=True), _track(150.0)
    ctx = build_transition_context(out, inc, 45.0, LOGGER)
    factory = CandidateFactory(ctx, LOGGER)
    built = [factory.build(spec) for spec in SegueGenerator().generate(ctx)]
    policy = RhythmClashPolicy()

    verdicts = [policy.evaluate(c, ctx) for c in built if c is not None]

    assert verdicts[0].rejected
    assert not verdicts[-1].rejected


def test_the_plan_line_names_the_segue_and_its_reason(caplog: pytest.LogCaptureFixture) -> None:
    """A shipped segue logs its style and what it overlapped."""
    with caplog.at_level(logging.DEBUG, logger=LOGGER.name):
        _plan(_quiet_tail_out(sings=True), _track(144.0, vocals=_vocals()))

    line = next(
        r.getMessage() for r in caplog.records if r.getMessage().startswith("planned transition: ")
    )
    assert line.startswith("planned transition: style=segue tier=quick_fade trigger=tempo ")
    assert " bars=" not in line
    assert line.endswith(
        ' reason="quiet tail 12.0s + head 0.0s, curves nofade/qsin, vocals out-only, kick in-only"'
    )
