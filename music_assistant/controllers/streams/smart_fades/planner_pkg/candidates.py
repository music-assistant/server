"""
Smart Fades - candidate value objects.

A ``CandidateSpec`` is a candidate factory's declared intent (which tier rung,
which anchor/entry, which relaxation was applied) before any plan exists; a
``Candidate`` is that spec paired with its timed ``TransitionPlan`` and
computed ``PlanMetrics``, ready for policies to score. The factory and
generators that build these are later tasks - this module holds only the two
frozen shapes they and the policies both depend on.
"""

from __future__ import annotations

from dataclasses import dataclass

from music_assistant.controllers.streams.smart_fades.models import (
    PlanMetrics,
    TransitionPlan,
    TransitionStrategy,
    TransitionTier,
)


@dataclass(frozen=True, slots=True)
class CandidateSpec:
    """A candidate's declared shape: tier rung, anchor/entry choice, and generator provenance."""

    tier: TransitionTier
    bars: int
    # buffer-local; None = pristine audible end
    anchor_s: float | None
    # None = natural entry
    entry_s: float | None
    strategy: TransitionStrategy = TransitionStrategy.ENERGY_ALIGNED
    # generator name, for scoreboard + tie-break
    source: str = ""
    # "outgoing" | "incoming" | None (16-bar relaxation)
    one_sided_vocal: str | None = None
    # the tier ladder's top rung; 0 = same as bars
    ideal_bars: int = 0


@dataclass(frozen=True, slots=True)
class Candidate:
    """One fully-built candidate: its spec, timed plan, and computed metrics."""

    spec: CandidateSpec
    # timed plan, eq_plan neutral until assembly
    plan: TransitionPlan
    metrics: PlanMetrics
    # the tier ladder's top rung for this context
    ideal_bars: int
