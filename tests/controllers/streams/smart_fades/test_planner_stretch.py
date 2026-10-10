"""Planner scenarios for the tempo ramp: a blend stretches only where both decks kick."""

from __future__ import annotations

import logging

import numpy as np
import pytest

from music_assistant.controllers.streams.smart_fades.models import (
    TransitionPlan,
    TransitionStyle,
)
from music_assistant.controllers.streams.smart_fades.planner import SmartCrossFadePlanner, planner
from music_assistant.controllers.streams.smart_fades.planner.candidates import Candidate
from music_assistant.controllers.streams.smart_fades.planner.context import TransitionContext
from music_assistant.controllers.streams.smart_fades.planner.policies import (
    Policy,
    Verdict,
    default_policies,
)
from music_assistant.controllers.streams.smart_fades.planner.selection import ScoredCandidate
from music_assistant.models.audio_analysis import AudioAnalysisData
from tests.controllers.streams.smart_fades.conftest import _analysis_with_bands

LOGGER = logging.getLogger(__name__)
# the outgoing buffer starts here for a 45 s buffer on a 240 s track
OFFSET = 195.0
_T = np.arange(1800) * (240.0 / 1800)


def _kicked(bpm: float = 120.0) -> AudioAnalysisData:
    """Build a 240 s track that kicks throughout."""
    return _analysis_with_bands(0.5, 0.3, 0.3, 0.3, bpm=bpm)


def _beatless_intro() -> AudioAnalysisData:
    """Build a 122 BPM track without a kick for its first 40 s."""
    return _analysis_with_bands(np.where(_T < 40.0, 0.02, 0.5), 0.3, 0.3, 0.3, bpm=122.0)


def _plan(fade_out: AudioAnalysisData, fade_in: AudioAnalysisData) -> TransitionPlan:
    return SmartCrossFadePlanner(LOGGER).plan(fade_out, fade_in, 45.0)


def _keep_winner(
    _planner: SmartCrossFadePlanner, winner: ScoredCandidate, *_: object
) -> ScoredCandidate:
    return winner


def _plan_ramped_only(
    monkeypatch: pytest.MonkeyPatch, fade_out: AudioAnalysisData, fade_in: AudioAnalysisData
) -> TransitionPlan:
    """Plan the pair as the planner did before it dropped unneeded stretches."""
    with monkeypatch.context() as patch:
        patch.setattr(SmartCrossFadePlanner, "_drop_unneeded_stretch", _keep_winner)
        return _plan(fade_out, fade_in)


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
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With its unstretched build rejected by a policy, the ramped winner ships as before."""
        out, inc = _kicked(), _beatless_intro()
        before = _plan_ramped_only(monkeypatch, out, inc)
        monkeypatch.setattr(
            planner, "default_policies", lambda: (*default_policies(), _RejectUnstretched())
        )

        plan = _plan(out, inc)

        assert plan.tempo_plan
        assert plan == before

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
