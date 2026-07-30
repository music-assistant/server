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
from collections import Counter
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
            histogram = Counter(
                verdict.reason for entry in scored for verdict in entry.verdicts if verdict.rejected
            )
            reasons = ", ".join(f"{reason} x{count}" for reason, count in histogram.most_common())
            self._logger.debug(
                "all %d candidates rejected (%s); emergency handoff will be used",
                len(scored),
                reasons,
            )
            return None
        winner = min(survivors, key=lambda entry: entry.total_penalty)
        if self._logger.isEnabledFor(VERBOSE_LOG_LEVEL):
            ranked = sorted(survivors, key=lambda entry: entry.total_penalty)
            runner_up = ranked[1] if len(ranked) > 1 else None
            self._logger.log(
                VERBOSE_LOG_LEVEL,
                "selection: scored=%d survivors=%d winner source=%s tier=%s bars=%d total=%.2f "
                "runner_up=%s runner_up_total=%s",
                len(scored),
                len(survivors),
                winner.candidate.spec.source,
                winner.candidate.spec.tier,
                winner.candidate.spec.bars,
                winner.total_penalty,
                runner_up.candidate.spec.source if runner_up is not None else None,
                runner_up.total_penalty if runner_up is not None else None,
            )
        return winner

    def _score(self, candidate: Candidate, ctx: TransitionContext) -> ScoredCandidate:
        """Evaluate every policy on one candidate and log its full scoreboard entry."""
        verdicts = tuple(policy.evaluate(candidate, ctx) for policy in self._policies)
        total_penalty = sum(verdict.penalty for verdict in verdicts)
        rejected = any(verdict.rejected for verdict in verdicts)
        if self._logger.isEnabledFor(VERBOSE_LOG_LEVEL):
            # per-policy breakdown so a tuning pass can see which penalty (or
            # rejection) each policy contributed, not just the aggregate total
            breakdown_entries = []
            for policy, verdict in zip(self._policies, verdicts, strict=True):
                name = type(policy).__name__.removesuffix("Policy")
                value = (
                    f"REJECTED({verdict.reason})" if verdict.rejected else f"{verdict.penalty:.2f}"
                )
                breakdown_entries.append(f"{name}={value}")
            plan, metrics = candidate.plan, candidate.metrics
            self._logger.log(
                VERBOSE_LOG_LEVEL,
                "candidate source=%s tier=%s bars=%d duration=%.2f anchor=%.2f fadein_trim=%s "
                "total=%.2f rejected=%s trim=%.2f collision=%.2f weighted_collision=%.2f "
                "on_downbeat=%s %s",
                candidate.spec.source,
                candidate.spec.tier,
                candidate.spec.bars,
                plan.crossfade_duration,
                plan.fade_out_window,
                plan.fadein_trim_start,
                total_penalty,
                rejected,
                metrics.audible_outgoing_trim,
                metrics.collision_seconds,
                metrics.weighted_collision_seconds,
                metrics.anchor_on_downbeat,
                " ".join(breakdown_entries),
            )
        return ScoredCandidate(
            candidate=candidate,
            total_penalty=total_penalty,
            verdicts=verdicts,
            rejected=rejected,
        )
