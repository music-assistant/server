"""Tests for the candidate selector: scoring, rejection filtering, and tie-breaking."""

from __future__ import annotations

import dataclasses
import logging

import numpy as np
import pytest

from music_assistant.controllers.streams.smart_fades.models import (
    Deck,
    TransitionStyle,
    TransitionTier,
)
from music_assistant.controllers.streams.smart_fades.planner.candidates import Candidate
from music_assistant.controllers.streams.smart_fades.planner.context import TransitionContext
from music_assistant.controllers.streams.smart_fades.planner.policies import Policy, Verdict
from music_assistant.controllers.streams.smart_fades.planner.selection import (
    CandidateSelector,
    ScoredCandidate,
)
from music_assistant.models.audio_analysis import AudioAnalysisData
from tests.controllers.streams.smart_fades.conftest import build_test_candidate


class _FixedPenaltyPolicy(Policy):
    """A stub policy that returns one fixed, deterministic verdict for every candidate."""

    def __init__(self, penalty: float = 0.0, rejected: bool = False, reason: str = "") -> None:
        self._verdict = Verdict(penalty=penalty, rejected=rejected, reason=reason)

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
        default_anchor=45.0,
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


def _named(source: str, style: TransitionStyle | None = None, duration: float = 20.0) -> Candidate:
    """Build a test candidate distinguishable by its ``spec.source``."""
    candidate = build_test_candidate(style=style, duration=duration)
    return dataclasses.replace(candidate, spec=dataclasses.replace(candidate.spec, source=source))


class TestCandidateSelector:
    """CandidateSelector.select: full scoring, rejection filtering, tie-breaking."""

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

    def test_all_rejected_returns_none(self) -> None:
        """When every candidate is rejected by some policy, select returns None."""
        selector = CandidateSelector(
            policies=[_FixedPenaltyPolicy(rejected=True, reason="always rejected")],
            logger=logging.getLogger(__name__),
        )

        result = selector.select([_named("a"), _named("b")], _ctx())

        assert result is None

    def test_rejected_candidate_never_wins_even_with_lowest_penalty(self) -> None:
        """A rejected candidate is excluded from ranking even if its penalty sum is lowest."""
        rejected = _named("rejected")
        survivor = _named("survivor")
        penalty_policy = _BySourcePenaltyPolicy({"rejected": 0.0, "survivor": 100.0})

        class _RejectByName(Policy):
            def evaluate(self, candidate: Candidate, ctx: TransitionContext) -> Verdict:
                if candidate.spec.source == "rejected":
                    return Verdict.reject("rejected by name")
                return Verdict.ok()

        selector = CandidateSelector(
            policies=[penalty_policy, _RejectByName()],
            logger=logging.getLogger(__name__),
        )

        result = selector.select([rejected, survivor], _ctx())

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

    def test_a_segue_shorter_than_the_best_other_survivor_never_wins(self) -> None:
        """A cheaper segue that would shorten the transition drops out of the ranking."""
        segue = _named("segue", style=TransitionStyle.SEGUE, duration=6.0)
        cut = _named("cut", style=TransitionStyle.CUT, duration=8.0)
        selector = CandidateSelector(
            policies=[_BySourcePenaltyPolicy({"segue": 0.0, "cut": 15.0})],
            logger=logging.getLogger(__name__),
        )

        result = selector.select([segue, cut], _ctx())

        assert result is not None
        assert result.candidate is cut

    def test_a_segue_at_least_as_long_as_the_best_other_survivor_competes(self) -> None:
        """A segue as long as the cut it replaces wins on its lower penalty."""
        segue = _named("segue", style=TransitionStyle.SEGUE, duration=8.0)
        cut = _named("cut", style=TransitionStyle.CUT, duration=8.0)
        selector = CandidateSelector(
            policies=[_BySourcePenaltyPolicy({"segue": 0.0, "cut": 15.0})],
            logger=logging.getLogger(__name__),
        )

        result = selector.select([cut, segue], _ctx())

        assert result is not None
        assert result.candidate is segue

    def test_segues_alone_compete_on_penalty(self) -> None:
        """Without any other survivor, the cheapest segue wins whatever its length."""
        long_segue = _named("long", style=TransitionStyle.SEGUE, duration=12.0)
        short_segue = _named("short", style=TransitionStyle.SEGUE, duration=4.0)
        selector = CandidateSelector(
            policies=[_BySourcePenaltyPolicy({"long": 3.0, "short": 1.0})],
            logger=logging.getLogger(__name__),
        )

        result = selector.select([long_segue, short_segue], _ctx())

        assert result is not None
        assert result.candidate is short_segue

    def test_a_lone_segue_waits_when_it_may_not_win_alone(self) -> None:
        """A selector told so ships no segue without another survivor to replace."""
        segue = _named("segue", style=TransitionStyle.SEGUE, duration=8.0)
        policies = [_FixedPenaltyPolicy()]
        logger = logging.getLogger(__name__)

        strict = CandidateSelector(policies, logger, lone_segue_wins=False)
        waiting = strict.select([segue], _ctx())
        alone = CandidateSelector(policies, logger).select([segue], _ctx())

        assert waiting is None
        assert alone is not None
        assert alone.candidate is segue

    @pytest.mark.parametrize("lone_segue_wins", [False, True])
    def test_a_segue_never_replaces_a_blend(self, lone_segue_wins: bool) -> None:
        """A longer, cheaper segue leaves a surviving blend in place, but replaces a cut."""
        segue = _named("segue", style=TransitionStyle.SEGUE, duration=15.0)
        blend = _named("blend", style=TransitionStyle.BLEND, duration=8.0)
        cut = _named("cut", style=TransitionStyle.CUT, duration=8.0)
        selector = CandidateSelector(
            policies=[_BySourcePenaltyPolicy({"segue": 0.0, "blend": 15.0, "cut": 15.0})],
            logger=logging.getLogger(__name__),
            lone_segue_wins=lone_segue_wins,
        )

        over_blend = selector.select([blend, segue], _ctx())
        over_cut = selector.select([cut, segue], _ctx())

        assert over_blend is not None
        assert over_blend.candidate is blend
        assert over_cut is not None
        assert over_cut.candidate is segue

    def test_a_segue_leaves_a_blend_in_place_also_behind_a_cheaper_cut(self) -> None:
        """Any surviving blend keeps the segue out, even when a cut scores below it."""
        segue = _named("segue", style=TransitionStyle.SEGUE, duration=15.0)
        blend = _named("blend", style=TransitionStyle.BLEND, duration=8.0)
        cut = _named("cut", style=TransitionStyle.CUT, duration=8.0)
        selector = CandidateSelector(
            policies=[_BySourcePenaltyPolicy({"segue": 0.0, "cut": 5.0, "blend": 15.0})],
            logger=logging.getLogger(__name__),
        )

        result = selector.select([blend, cut, segue], _ctx())

        assert result is not None
        assert result.candidate is cut

class TestDressedSelection:
    """A dressed transition only ever replaces the cut that would ship otherwise."""

    def _select(
        self, candidates: list[Candidate], penalties: dict[str, float]
    ) -> ScoredCandidate | None:
        selector = CandidateSelector(
            policies=[_BySourcePenaltyPolicy(penalties)],
            logger=logging.getLogger(__name__),
            lone_segue_wins=False,
        )
        return selector.select(candidates, _ctx())

    def test_a_cheaper_dressed_transition_replaces_the_winning_cut(self) -> None:
        """A shorter, cheaper filter out ships in place of the cut."""
        cut = _named("cut", style=TransitionStyle.CUT, duration=8.0)
        dressed = _named("filter", style=TransitionStyle.FILTER_OUT, duration=4.0)

        result = self._select([cut, dressed], {"cut": 15.0, "filter": 2.0})

        assert result is not None
        assert result.candidate is dressed

    def test_a_dearer_dressed_transition_leaves_the_cut(self) -> None:
        """A dressed transition scoring worse than the cut does not ship."""
        cut = _named("cut", style=TransitionStyle.CUT)
        dressed = _named("echo", style=TransitionStyle.ECHO_OUT)

        result = self._select([cut, dressed], {"cut": 15.0, "echo": 16.0})

        assert result is not None
        assert result.candidate is cut

    def test_a_dressed_transition_never_replaces_a_blend_or_a_segue(self) -> None:
        """Against a winning blend or segue the cheapest dressed transition stays out."""
        blend = _named("blend", style=TransitionStyle.BLEND, duration=8.0)
        segue = _named("segue", style=TransitionStyle.SEGUE, duration=10.0)
        cut = _named("cut", style=TransitionStyle.CUT, duration=8.0)
        dressed = _named("echo", style=TransitionStyle.ECHO_OUT, duration=2.0)
        penalties = {"blend": 10.0, "segue": 5.0, "cut": 15.0, "echo": 0.0}

        over_blend = self._select([blend, cut, dressed], penalties)
        over_segue = self._select([cut, segue, dressed], penalties)

        assert over_blend is not None
        assert over_blend.candidate is blend
        assert over_segue is not None
        assert over_segue.candidate is segue

    def test_a_dressed_transition_does_not_shorten_what_a_segue_must_match(self) -> None:
        """A segue as long as the cut competes even when a shorter dressed one scores better."""
        cut = _named("cut", style=TransitionStyle.CUT, duration=8.0)
        segue = _named("segue", style=TransitionStyle.SEGUE, duration=8.0)
        dressed = _named("filter", style=TransitionStyle.FILTER_OUT, duration=4.0)

        result = self._select([cut, dressed, segue], {"cut": 15.0, "filter": 0.0, "segue": 4.0})

        assert result is not None
        assert result.candidate is segue

    def test_dressed_transitions_alone_win_only_the_rescue_pass(self) -> None:
        """Without a surviving blend or cut, a dressed transition wins the rescue pass only."""
        dressed = _named("echo", style=TransitionStyle.ECHO_OUT)
        rescue = CandidateSelector([_FixedPenaltyPolicy()], logging.getLogger(__name__))

        alone = rescue.select([dressed], _ctx())

        assert self._select([dressed], {"echo": 0.0}) is None
        assert alone is not None
        assert alone.candidate is dressed
