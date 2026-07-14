"""Tests for the candidate selector: scoring, veto filtering, and tie-breaking."""

from __future__ import annotations

import dataclasses
import logging

import numpy as np

from music_assistant.controllers.streams.smart_fades.models import Deck, TransitionTier
from music_assistant.controllers.streams.smart_fades.planner_pkg.candidates import Candidate
from music_assistant.controllers.streams.smart_fades.planner_pkg.context import TransitionContext
from music_assistant.controllers.streams.smart_fades.planner_pkg.policies import Policy, Verdict
from music_assistant.controllers.streams.smart_fades.planner_pkg.selection import (
    CandidateSelector,
    ScoredCandidate,
)
from music_assistant.models.audio_analysis import AudioAnalysisData
from tests.controllers.streams.smart_fades.conftest import build_test_candidate


class _FixedPenaltyPolicy(Policy):
    """A stub policy that returns one fixed, deterministic verdict for every candidate."""

    def __init__(self, penalty: float = 0.0, vetoed: bool = False, reason: str = "") -> None:
        self._verdict = Verdict(penalty=penalty, vetoed=vetoed, reason=reason)

    def evaluate(self, candidate: Candidate, ctx: TransitionContext) -> Verdict:
        """Return this instance's fixed verdict, regardless of the candidate."""
        return self._verdict


class _BySourcePenaltyPolicy(Policy):
    """A stub policy that maps ``spec.source`` to a fixed per-candidate penalty."""

    def __init__(self, penalties: dict[str, float]) -> None:
        self._penalties = penalties

    def evaluate(self, candidate: Candidate, ctx: TransitionContext) -> Verdict:
        """Return the configured penalty for this candidate's ``spec.source``."""
        return Verdict.ok(self._penalties[candidate.spec.source])


def _ctx() -> TransitionContext:
    """Build a minimal TransitionContext; stub policies never read its fields."""
    deck = Deck(
        analysis=AudioAnalysisData(),
        bpm=120.0,
        beats=np.array([], dtype=np.float32),
        downbeats=np.array([], dtype=np.float32),
    )
    return TransitionContext(
        outgoing=deck,
        incoming=deck,
        outgoing_profile=None,
        incoming_profile=None,
        buffer_duration=45.0,
        buffer_offset=0.0,
        audio_end=45.0,
        mix_out_anchor=None,
        kick_anchor=None,
        fade_onset=None,
        coda_zone=None,
        tier=TransitionTier.FULL_BLEND,
        cross_meter=False,
        bpm_diff_percent=0.0,
        vocal_out_placement=None,
        vocal_in_placement=None,
        vocal_out_scoring=None,
        vocal_in_scoring=None,
        natural_entry=0.0,
        protective_downbeats=(),
    )


def _named(source: str) -> Candidate:
    """Build a test candidate distinguishable only by its ``spec.source``."""
    candidate = build_test_candidate()
    return dataclasses.replace(candidate, spec=dataclasses.replace(candidate.spec, source=source))


class TestCandidateSelector:
    """CandidateSelector.select: full scoring, veto filtering, tie-breaking."""

    def test_lowest_penalty_wins(self) -> None:
        """The survivor with the smallest total penalty is selected."""
        low = _named("low")
        high = _named("high")
        selector = CandidateSelector(
            policies=[_BySourcePenaltyPolicy({"low": 1.0, "high": 5.0})],
            logger=logging.getLogger(__name__),
        )

        result = selector.select([high, low], _ctx())

        assert result is not None
        assert result.candidate is low
        assert result.total_penalty == 1.0

    def test_tie_breaks_to_first_in_input_order(self) -> None:
        """Equal-penalty survivors resolve to whichever came first in the input sequence."""
        first = _named("first")
        second = _named("second")
        selector = CandidateSelector(
            policies=[_FixedPenaltyPolicy(penalty=3.0)],
            logger=logging.getLogger(__name__),
        )

        result = selector.select([first, second], _ctx())

        assert result is not None
        assert result.candidate is first

    def test_all_vetoed_returns_none(self) -> None:
        """When every candidate is vetoed by some policy, select returns None."""
        selector = CandidateSelector(
            policies=[_FixedPenaltyPolicy(vetoed=True, reason="always vetoed")],
            logger=logging.getLogger(__name__),
        )

        result = selector.select([_named("a"), _named("b")], _ctx())

        assert result is None

    def test_vetoed_candidate_never_wins_even_with_lowest_penalty(self) -> None:
        """A vetoed candidate is excluded from ranking even if its penalty sum is lowest."""
        vetoed = _named("vetoed")
        survivor = _named("survivor")
        penalty_policy = _BySourcePenaltyPolicy({"vetoed": 0.0, "survivor": 100.0})

        class _VetoOnlyVetoed(Policy):
            def evaluate(self, candidate: Candidate, ctx: TransitionContext) -> Verdict:
                if candidate.spec.source == "vetoed":
                    return Verdict.veto("vetoed by name")
                return Verdict.ok()

        selector = CandidateSelector(
            policies=[penalty_policy, _VetoOnlyVetoed()],
            logger=logging.getLogger(__name__),
        )

        result = selector.select([vetoed, survivor], _ctx())

        assert result is not None
        assert result.candidate is survivor

    def test_empty_input_returns_none(self) -> None:
        """An empty candidate sequence yields None."""
        selector = CandidateSelector(
            policies=[_FixedPenaltyPolicy()], logger=logging.getLogger(__name__)
        )

        result = selector.select([], _ctx())

        assert result is None

    def test_scored_candidate_carries_all_verdicts(self) -> None:
        """The returned ScoredCandidate carries one verdict per policy, in evaluation order."""
        policies = [_FixedPenaltyPolicy(penalty=1.0), _FixedPenaltyPolicy(penalty=2.0)]
        selector = CandidateSelector(policies=policies, logger=logging.getLogger(__name__))

        result = selector.select([_named("only")], _ctx())

        assert result is not None
        assert isinstance(result, ScoredCandidate)
        assert len(result.verdicts) == len(policies)
        assert result.total_penalty == 3.0
