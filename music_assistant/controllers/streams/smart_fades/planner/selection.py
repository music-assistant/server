"""
Smart Fades - candidate selection.

A ``CandidateSelector`` scores every built candidate against the full policy
set, folding each policy's ``Verdict`` into one ``ScoredCandidate`` scoreboard
entry, then picks the lowest-penalty, non-rejected survivor. Every policy runs
on every candidate - no short-circuit on the first rejection - so the debug
log always shows the complete scoreboard, not just whichever rule fired first.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from music_assistant.constants import VERBOSE_LOG_LEVEL

from .candidates import Candidate
from .context import TransitionContext
from .policies import Policy, Verdict


@dataclass(frozen=True, slots=True)
class ScoredCandidate:
    """One candidate's full scoreboard entry: every policy's verdict and its resulting rank."""

    candidate: Candidate
    total_penalty: float
    verdicts: tuple[Verdict, ...]
    rejected: bool


class CandidateSelector:
    """Scores every candidate against a fixed policy set and picks the best survivor."""

    def __init__(self, policies: Sequence[Policy], logger: logging.Logger) -> None:
        """Initialize the selector with the policy set to score every candidate against."""
        self._policies = tuple(policies)
        self._logger = logger

    def select(
        self, candidates: Sequence[Candidate], ctx: TransitionContext
    ) -> ScoredCandidate | None:
        """
        Score every candidate; return the lowest-penalty survivor, or None when all are rejected.

        Ties resolve to whichever candidate appears earlier in ``candidates``.

        :param candidates: Built candidates to score, in generator-declared order.
        :param ctx: The shared per-transition facts every policy judges against.
        """
        scored = [self._score(candidate, ctx) for candidate in candidates]
        survivors = [entry for entry in scored if not entry.rejected]
        if not survivors:
            return None
        return min(survivors, key=lambda entry: entry.total_penalty)

    def _score(self, candidate: Candidate, ctx: TransitionContext) -> ScoredCandidate:
        """Evaluate every policy on one candidate and log its full scoreboard entry."""
        verdicts = tuple(policy.evaluate(candidate, ctx) for policy in self._policies)
        total_penalty = sum(verdict.penalty for verdict in verdicts)
        rejected = any(verdict.rejected for verdict in verdicts)
        reasons = [verdict.reason for verdict in verdicts if verdict.reason]
        self._logger.log(
            VERBOSE_LOG_LEVEL,
            "candidate source=%s bars=%d anchor=%s total=%.2f rejected=%s reasons=%s",
            candidate.spec.source,
            candidate.spec.bars,
            candidate.spec.anchor_s,
            total_penalty,
            rejected,
            reasons,
        )
        return ScoredCandidate(
            candidate=candidate,
            total_penalty=total_penalty,
            verdicts=verdicts,
            rejected=rejected,
        )
