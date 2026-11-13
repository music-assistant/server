"""Planner scenarios for the tempo ramp: a blend stretches only where both decks kick."""

from __future__ import annotations

import dataclasses
import logging

import numpy as np
import pytest

from music_assistant.controllers.streams.smart_fades.models import (
    TempoPlan,
    TransitionPlan,
    TransitionStyle,
    TransitionTier,
)
from music_assistant.controllers.streams.smart_fades.planner import SmartCrossFadePlanner, planner
from music_assistant.controllers.streams.smart_fades.planner.candidates import (
    Candidate,
    CandidateFactory,
    CandidateSpec,
)
from music_assistant.controllers.streams.smart_fades.planner.context import (
    TransitionContext,
    build_transition_context,
)
from music_assistant.controllers.streams.smart_fades.planner.policies import (
    Policy,
    Verdict,
    default_policies,
)
from music_assistant.models.audio_analysis import AudioAnalysisData
from tests.controllers.streams.smart_fades.conftest import _analysis_with_bands

LOGGER = logging.getLogger(__name__)
# the outgoing buffer starts here for a 45 s buffer on a 240 s track
OFFSET = 195.0
_T = np.arange(1800) * (240.0 / 1800)


def _kicked(bpm: float = 120.0) -> AudioAnalysisData:
    """Build a 240 s track that kicks throughout."""
    return _analysis_with_bands(0.5, 0.3, 0.3, 0.3, bpm=bpm)


def _plain(bpm: float) -> AudioAnalysisData:
    """Build a 240 s track with a regular 4/4 grid and flat energy, without band data."""
    beats = np.arange(0.0, 240.0, 60.0 / bpm, dtype=np.float32)
    return AudioAnalysisData(
        duration=240.0,
        bpm=bpm,
        beats=beats.tolist(),
        downbeats=beats[::4].tolist(),
        rms_energy=np.full(1800, 0.5, dtype=np.float32).tolist(),
        key="A",
        mode="minor",
    )


def _beatless_intro() -> AudioAnalysisData:
    """Build a 122 BPM track without a kick for its first 40 s."""
    return _analysis_with_bands(np.where(_T < 40.0, 0.02, 0.5), 0.3, 0.3, 0.3, bpm=122.0)


def _plan(fade_out: AudioAnalysisData, fade_in: AudioAnalysisData) -> TransitionPlan:
    return SmartCrossFadePlanner(LOGGER).plan(fade_out, fade_in, 45.0)


def _keep_winner(winner: Candidate, *_: object) -> Candidate:
    return winner


def _plan_ramped_only(
    monkeypatch: pytest.MonkeyPatch, fade_out: AudioAnalysisData, fade_in: AudioAnalysisData
) -> TransitionPlan:
    """Plan the pair with the ramped winner always shipping."""
    with monkeypatch.context() as patch:
        patch.setattr(planner, "_drop_unneeded_stretch", _keep_winner)
        return _plan(fade_out, fade_in)


def _blend(
    *, ratio: float | None, crossfade: float = 16.0, trim: float | None = 0.0
) -> TransitionPlan:
    """Build a blend ending at 45 s, ramped to ``ratio`` or unstretched when None."""
    return TransitionPlan(
        tier=TransitionTier.FULL_BLEND,
        fade_out_window=45.0,
        crossfade_duration=crossfade,
        style=TransitionStyle.BLEND,
        tempo_plan=TempoPlan(steps=[(20.0, 1.0), (28.0, ratio)]) if ratio else TempoPlan(),
        fadein_trim_start=trim,
    )


def _kick_ctx(
    kick_out: tuple[tuple[float, float], ...] | None,
    kick_in: tuple[tuple[float, float], ...] | None,
    out_bpm: float = 120.0,
    in_bpm: float = 120.0,
) -> TransitionContext:
    """Build a context for two plain grids with the given kick runs."""
    ctx = build_transition_context(_plain(out_bpm), _plain(in_bpm), 45.0, LOGGER)
    return dataclasses.replace(ctx, kick_out=kick_out, kick_in=kick_in)


class _RejectUnstretched(Policy):
    """Reject every unstretched blend, the way a vocal collision would."""

    def evaluate(self, candidate: Candidate, ctx: TransitionContext) -> Verdict:
        """Judge one candidate against the shared per-transition context."""
        if candidate.plan.style is TransitionStyle.BLEND and not candidate.plan.tempo_plan:
            return Verdict.reject("stand-in for a vocal collision")
        return Verdict.ok()


class TestStretchScenarios:
    """A ramped winner ships unstretched when a deck has no kick for the ramp to match."""

    def test_a_beatless_intro_blends_unstretched(self) -> None:
        """Without a kick in the incoming overlap, the winner ships unstretched."""
        plan = _plan(_kicked(), _beatless_intro())

        assert plan.style is TransitionStyle.BLEND
        assert not plan.tempo_plan

    def test_a_rejected_unstretched_variant_ships_the_ramped_blend(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """With its unstretched build rejected by a policy, the ramped winner ships as before."""
        out, inc = _kicked(), _beatless_intro()
        before = _plan_ramped_only(monkeypatch, out, inc)
        monkeypatch.setattr(
            planner, "default_policies", lambda: (*default_policies(), _RejectUnstretched())
        )

        with caplog.at_level(logging.DEBUG, logger=LOGGER.name):
            plan = _plan(out, inc)

        assert plan.tempo_plan
        assert plan == before
        # the policy check on the unstretched build logs no rejected selection pass
        assert not any("candidates rejected" in r.getMessage() for r in caplog.records)

    def test_an_unstretched_overlap_reaching_the_kick_keeps_the_ramp(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ramped overlap ends where the incoming kick starts; unstretched it reaches it."""
        out = _kicked()
        inc = _analysis_with_bands(np.where(_T < 15.0, 0.02, 0.5), 0.3, 0.3, 0.3, bpm=128.0)

        plan = _plan(out, inc)

        assert plan.tempo_plan
        # 16 s of outgoing input render in 15 s at 128/120; unstretched they last 16 s
        assert plan.crossfade_duration == pytest.approx(15.0)
        assert plan.fadein_trim_start == 0.0
        assert plan == _plan_ramped_only(monkeypatch, out, inc)

    def test_two_kicks_keep_the_ramp(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Both decks kicking in the overlap keep the ramp."""
        out, inc = _kicked(), _kicked(122.0)

        plan = _plan(out, inc)

        assert plan.tempo_plan
        assert plan == _plan_ramped_only(monkeypatch, out, inc)

    def test_a_breakdown_before_the_overlap_keeps_the_ramp(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A kickless stretch window still ramps when both decks kick in the overlap."""
        out = _analysis_with_bands(np.where((_T >= 210.0) & (_T < 222.0), 0.02, 0.5), 0.3, 0.3, 0.3)
        inc = _kicked(122.0)

        plan = _plan(out, inc)

        assert plan.tempo_plan
        # the ramp runs from its first step to the overlap start, inside the breakdown
        overlap_start = plan.fade_out_window - plan.crossfade_duration * 122.0 / 120.0
        assert OFFSET + plan.tempo_plan.steps[0][0] >= 210.0
        assert OFFSET + overlap_start <= 222.0 + 1e-3
        assert plan == _plan_ramped_only(monkeypatch, out, inc)

    def test_a_rolling_intro_trim_moves_the_overlap_onto_a_breakdown(self) -> None:
        """The incoming overlap counts from the trim: a kicked head before it does not count."""
        breakdown = (_T >= 8.0) & (_T < 30.0)
        # quiet voice bands, so the rolling intro may cut into the breakdown
        quiet = np.where(breakdown, 0.05, 0.3)
        inc = _analysis_with_bands(np.where(breakdown, 0.02, 0.5), quiet, quiet, 0.3, bpm=122.0)

        plan = _plan(_kicked(), inc)

        assert not plan.tempo_plan
        # 8 outgoing bars, the incoming groove entry at 29.5 s landing on the overlap end
        assert plan.crossfade_duration == pytest.approx(16.0)
        assert plan.fadein_trim_start == pytest.approx(13.77, abs=0.01)


class TestBothDecksKick:
    """Both decks kick in a blend's overlap with at least a beat of kick each."""

    everywhere = ((0.0, 45.0),)

    @pytest.mark.parametrize(
        ("kick_out", "kick_in", "kicks"),
        [
            (((44.6, 45.0),), everywhere, False),
            (((44.5, 45.0),), everywhere, True),
            (everywhere, ((15.6, 45.0),), False),
            (everywhere, ((15.5, 45.0),), True),
            (None, None, True),
        ],
        ids=["outgoing graze", "outgoing beat", "incoming graze", "incoming beat", "no band data"],
    )
    def test_a_deck_needs_a_beat_of_kick_in_the_overlap(
        self,
        kick_out: tuple[tuple[float, float], ...] | None,
        kick_in: tuple[tuple[float, float], ...] | None,
        kicks: bool,
    ) -> None:
        """
        Less than one beat (0.5 s at 120 BPM) of kick in the 16 s overlap counts as none.

        :param kick_out: Outgoing kick runs, None without band data.
        :param kick_in: Incoming kick runs, None without band data.
        :param kicks: Whether both decks count as kicking.
        """
        ctx = _kick_ctx(kick_out, kick_in)

        assert planner._both_decks_kick(ctx, _blend(ratio=None)) is kicks

    def test_the_incoming_overlap_counts_from_the_trim(self) -> None:
        """An incoming kick before the trim is not in the overlap."""
        ctx = _kick_ctx(self.everywhere, ((0.0, 8.0),))

        assert planner._both_decks_kick(ctx, _blend(ratio=None, trim=0.0)) is True
        assert planner._both_decks_kick(ctx, _blend(ratio=None, trim=10.0)) is False

    def test_a_ramped_overlap_spans_the_crossfade_times_the_ratio(self) -> None:
        """At 1.08 a 15 s overlap plays 16.2 s of outgoing input, from 28.8 s."""
        ctx = _kick_ctx(((28.8, 30.0),), self.everywhere, in_bpm=129.6)

        assert planner._both_decks_kick(ctx, _blend(ratio=1.08, crossfade=15.0)) is True
        assert planner._both_decks_kick(ctx, _blend(ratio=None, crossfade=15.0)) is False

    def test_the_ratio_is_the_ramps_last_step(self) -> None:
        """A built ramp ends on the deck tempo ratio, rounded to 6 decimals."""
        ctx = build_transition_context(_plain(120.0), _plain(116.0), 45.0, LOGGER)
        spec = CandidateSpec(tier=ctx.tier, bars=8, anchor_s=None, entry_s=None)
        candidate = CandidateFactory(ctx, LOGGER).build(spec)

        assert candidate is not None
        assert candidate.plan.tempo_plan.steps[-1][1] == pytest.approx(116.0 / 120.0, abs=5e-7)


class TestDropUnneededStretch:
    """Only a ramped blend missing a kick on a deck is rebuilt unstretched."""

    def test_a_ramped_overlap_with_both_kicks_keeps_the_ramp(self) -> None:
        """
        Both decks kicking in the ramped overlap keep the ramp, before any rebuild.

        At 125 -> 120 BPM the ramped overlap is 18 s and the unstretched one 17.3 s, so a
        kick entering at 17 s plays in the ramped overlap only.
        """
        ctx = _kick_ctx(((0.0, 45.0),), ((17.0, 45.0),), out_bpm=125.0, in_bpm=120.0)
        factory = CandidateFactory(ctx, LOGGER)
        spec = CandidateSpec(tier=ctx.tier, bars=8, anchor_s=None, entry_s=None)
        ramped = factory.build(spec)
        unstretched = factory.build(spec, stretch=False)
        assert ramped is not None
        assert unstretched is not None
        assert ramped.plan.tempo_plan
        assert planner._both_decks_kick(ctx, ramped.plan)
        assert not planner._both_decks_kick(ctx, unstretched.plan)

        assert planner._drop_unneeded_stretch(ramped, [(spec, ramped)], factory, ctx) is ramped
