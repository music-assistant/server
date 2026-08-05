"""
Smart Fades - candidate scoring policies.

Each ``Policy`` independently judges one built ``Candidate`` against the
shared ``TransitionContext``, returning a ``Verdict``: either an outright
rejection (the candidate is disqualified) or a soft penalty folded into
ranking against other surviving candidates. Keeping each rule its own class lets selection
compose/reorder/disable them without touching the scoring math itself.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from music_assistant.controllers.streams.smart_fades.models import (
    TransitionPlan,
    TransitionStrategy,
    TransitionTier,
)
from music_assistant.controllers.streams.smart_fades.vocal import (
    COLLISION_SECONDS_LIMIT,
    SHORT_FADE_SECONDS,
    WEIGHTED_COLLISION_LIMIT,
)

from .candidates import RUNG_LADDER, Candidate, VocalOnsetEntryGenerator
from .context import TransitionContext

# The tier ladder's rungs, largest first, that a candidate's bar count is
# measured against (matches the old planner's candidate-bar-count ladder)
# Ambition ordering of the transition tiers, most ambitious first
_TIER_ORDER: tuple[TransitionTier, ...] = (
    TransitionTier.FULL_BLEND,
    TransitionTier.TEMPO_BLEND,
    TransitionTier.QUICK_FADE,
)


@dataclass(frozen=True, slots=True)
class Verdict:
    """One policy's judgment on a candidate: a rejection, or an accept with an optional penalty."""

    penalty: float = 0.0
    rejected: bool = False
    reason: str = ""

    @classmethod
    def reject(cls, reason: str) -> Verdict:
        """Disqualify a candidate outright, with a human-readable reason."""
        return cls(rejected=True, reason=reason)

    @classmethod
    def ok(cls, penalty: float = 0.0, reason: str = "") -> Verdict:
        """Accept a candidate, optionally carrying a soft penalty."""
        return cls(penalty=penalty, reason=reason)


class Policy(ABC):
    """One independent scoring rule applied to a built candidate."""

    @abstractmethod
    def evaluate(self, candidate: Candidate, ctx: TransitionContext) -> Verdict:
        """Judge one candidate against the shared per-transition context."""


class VocalCollisionPolicy(Policy):
    """Reject or penalize simultaneous outgoing/incoming vocal overlap inside the crossfade."""

    collision_seconds_limit: float = COLLISION_SECONDS_LIMIT
    weighted_collision_limit: float = WEIGHTED_COLLISION_LIMIT
    weighted_penalty_scale: float = 20.0

    def evaluate(self, candidate: Candidate, ctx: TransitionContext) -> Verdict:
        """Judge one candidate against the shared per-transition context."""
        if ctx.vocal_out_scoring is None or ctx.vocal_in_scoring is None:
            return Verdict.ok()
        metrics = candidate.metrics
        if (
            metrics.collision_seconds >= self.collision_seconds_limit
            or metrics.weighted_collision_seconds >= self.weighted_collision_limit
        ):
            return Verdict.reject("vocal collision exceeds the guard limit")
        # quadratic in the normalized residue: overlap is a threshold percept,
        # so near-inaudible low-gain residue stays cheap while the cost climbs
        # steeply toward the rejection boundary (panel-recommended shape)
        normalized = metrics.weighted_collision_seconds / self.weighted_collision_limit
        return Verdict.ok(normalized**2 * self.weighted_penalty_scale)


class VocalTruncationPolicy(Policy):
    """Reject a candidate that cuts off an audible outgoing vocal phrase."""

    # An audible outgoing phrase cut by more than this many seconds reads as a
    # truncation rather than an inaudible tail sliver
    max_truncated_vocal: float = 0.25

    def evaluate(self, candidate: Candidate, ctx: TransitionContext) -> Verdict:
        """Judge one candidate against the shared per-transition context."""
        if ctx.vocal_out_scoring is None:
            return Verdict.ok()
        # truncation = audible vocal BEYOND the candidate's anchor (cut off by the
        # trim), not vocal inside the fade - a phrase riding the fade is normal
        anchor = candidate.plan.fade_out_window
        truncated = sum(
            min(right, ctx.audio_end) - max(left, anchor)
            for left, right in ctx.vocal_out_scoring.windows
            if min(right, ctx.audio_end) > max(left, anchor)
        )
        if truncated > self.max_truncated_vocal:
            return Verdict.reject("truncates an audible outgoing vocal phrase")
        return Verdict.ok()


class AudibleTrimPolicy(Policy):
    """Reject a short fade that trims more audible outgoing material than it spans, else penalize."""

    short_fade_seconds: float = SHORT_FADE_SECONDS
    trim_penalty_per_second: float = 1.0

    def evaluate(self, candidate: Candidate, ctx: TransitionContext) -> Verdict:
        """Judge one candidate against the shared per-transition context."""
        plan = candidate.plan
        trim = candidate.metrics.audible_outgoing_trim
        # missing vocal data must never disable this guard: a stale
        # pre-vocal-analysis row would otherwise ship a huge, unprotected cut
        if plan.crossfade_duration <= self.short_fade_seconds and trim > plan.crossfade_duration:
            return Verdict.reject("audible trim exceeds a short fade's own duration")
        return Verdict.ok(trim * self.trim_penalty_per_second)


class DeadAirPolicy(Policy):
    """Penalize a handover that strands the listener in silence before B's groove entry."""

    grace_seconds: float = 4.0
    dead_air_penalty_per_second: float = 1.0
    # pre-fade outgoing level (relative to its own track peak) below which the
    # outgoing is already a whisper and a slow incoming intro is welcome
    hot_outgoing_floor: float = 0.178  # -15 dB
    lookback_seconds: float = 8.0

    def evaluate(self, candidate: Candidate, ctx: TransitionContext) -> Verdict:
        """Judge one candidate against the shared per-transition context."""
        plan = candidate.plan
        gap = (
            ctx.natural_entry
            - (plan.fadein_trim_start or 0.0)
            - plan.crossfade_duration
            - self.grace_seconds
        )
        if gap <= 0.0 or not self._outgoing_is_hot(plan, ctx):
            return Verdict.ok()
        return Verdict.ok(gap * self.dead_air_penalty_per_second)

    def _outgoing_is_hot(self, plan: TransitionPlan, ctx: TransitionContext) -> bool:
        """Whether the outgoing still plays at level just before the candidate's fade."""
        analysis = ctx.outgoing.analysis
        rms = analysis.rms_energy
        duration = analysis.duration
        if rms is None or len(rms) == 0 or not duration:
            # without energy data the gate cannot clear the penalty; dead air
            # after an unknown outgoing is worse than a trimmed intro
            return True
        peak = max(rms)
        if peak <= 0.0:
            return False
        bin_seconds = duration / len(rms)
        # sample the source RMS over the last lookback window ending at the cut
        # anchor; the stored energy is unfaded, so this reads the outgoing's own
        # level right before the cut with no crossfade-ramp or tempo-stretch mixed in
        anchor = ctx.buffer_offset + plan.fade_out_window
        low = max(0, int((anchor - self.lookback_seconds) / bin_seconds))
        high = max(low + 1, min(len(rms), int(anchor / bin_seconds)))
        window = rms[low:high]
        return sum(window) / len(window) >= peak * self.hot_outgoing_floor


class OverlapPreferencePolicy(Policy):
    """Prefer the tier's top rung and the context's chosen tier."""

    rung_penalty_per_step: float = 10.0
    tier_penalty_per_step: float = 15.0
    lazy_overlay_penalty: float = 2.0

    def evaluate(self, candidate: Candidate, ctx: TransitionContext) -> Verdict:
        """Judge one candidate against the shared per-transition context."""
        spec = candidate.spec
        if spec.strategy is TransitionStrategy.LAZY_OVERLAY:
            # unphrased fallback: mildly dispreferred so any surviving phrased
            # candidate of comparable cost still wins
            return Verdict.ok(self.lazy_overlay_penalty)
        rung_gap = RUNG_LADDER.index(spec.bars) - RUNG_LADDER.index(candidate.ideal_bars)
        tier_steps = max(0, _TIER_ORDER.index(spec.tier) - _TIER_ORDER.index(ctx.tier))
        penalty = self.rung_penalty_per_step * rung_gap
        penalty += self.tier_penalty_per_step * tier_steps
        return Verdict.ok(penalty)


class AnchorAlignmentPolicy(Policy):
    """Prefer downbeat-anchored fades and groove-aligned incoming entries."""

    downbeat_penalty: float = 4.0
    entry_misalignment_penalty: float = 2.0

    def evaluate(self, candidate: Candidate, ctx: TransitionContext) -> Verdict:
        """Judge one candidate against the shared per-transition context."""
        penalty = 0.0
        if not candidate.metrics.anchor_on_downbeat:
            penalty += self.downbeat_penalty
        spec = candidate.spec
        # a generator-pinned entry counts as groove-aligned only when the
        # generator itself already lands it on the vocal onset
        if spec.entry_s is not None and spec.source != VocalOnsetEntryGenerator.name:
            penalty += self.entry_misalignment_penalty
        return Verdict.ok(penalty)


def default_policies() -> tuple[Policy, ...]:
    """Return the standard policy set applied to every candidate, in evaluation order."""
    return (
        VocalCollisionPolicy(),
        VocalTruncationPolicy(),
        AudibleTrimPolicy(),
        DeadAirPolicy(),
        OverlapPreferencePolicy(),
        AnchorAlignmentPolicy(),
    )
