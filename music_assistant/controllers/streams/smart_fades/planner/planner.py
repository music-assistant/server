"""
Smart Fades - the candidate/policy transition planner.

``SmartCrossFadePlanner.plan()`` is a thin orchestration of the pipeline:
build the immutable ``TransitionContext``, let the generators propose
candidate specs, build each into a timed candidate, score them all with the
rejection/penalty policies, finalize the winner's EQ - or, when every
candidate is rejected, retry with late-anchored rescue candidates (the
ungated audible-end ladder, a modest rescue rung and the segue), then ship a plain
equal-power fallback crossfade - or, when even that collides too severely,
the click-free emergency handoff as a last resort. Alternative strategies
slot in as sibling ``TransitionPlanner`` subclasses.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections import Counter
from dataclasses import replace
from typing import TYPE_CHECKING

from music_assistant.constants import VERBOSE_LOG_LEVEL
from music_assistant.controllers.streams.smart_fades.models import (
    QuickFadeTrigger,
    SmartFadeNotApplicable,
    TransitionStyle,
    TransitionTier,
)

from .assembly import EmergencyHandoffFactory, FallbackCrossfadeFactory, PlanAssembler
from .candidates import (
    _SINGS_DUTY,
    CandidateFactory,
    RescueAnchorGenerator,
    SegueGenerator,
    TrimClosingAnchorGenerator,
    _window_duties,
    default_generators,
)
from .context import build_transition_context
from .policies import default_policies
from .selection import CandidateSelector

if TYPE_CHECKING:
    import logging

    from music_assistant.controllers.streams.smart_fades.models import TransitionPlan
    from music_assistant.models.audio_analysis import AudioAnalysisData

    from .context import TransitionContext


class TransitionPlanner(ABC):
    """Abstract base class for transition planners."""

    def __init__(self, logger: logging.Logger) -> None:
        """Initialize the planner."""
        self.logger = logger

    @abstractmethod
    def plan(
        self,
        fade_out_analysis: AudioAnalysisData,
        fade_in_analysis: AudioAnalysisData,
        buffer_duration: float,
    ) -> TransitionPlan:
        """
        Build a ``TransitionPlan`` from the two tracks' analysis data.

        Pure over the analysis rows and the available holdback window — touches
        no audio bytes.  Raises ``SmartFadeNotApplicable`` when the tracks cannot
        yield this transition and the caller should fall back.

        :param fade_out_analysis: Analysis data for the outgoing track.
        :param fade_in_analysis: Analysis data for the incoming track.
        :param buffer_duration: Length in seconds of the available fade-out holdback.
        """


class SmartCrossFadePlanner(TransitionPlanner):
    """Plans a defensive, musically-aligned crossfade that never edits the music."""

    def plan(
        self,
        fade_out_analysis: AudioAnalysisData,
        fade_in_analysis: AudioAnalysisData,
        buffer_duration: float,
    ) -> TransitionPlan:
        """
        Build a smart-crossfade ``TransitionPlan`` from the two tracks' analysis.

        Vocal-aware protections engage per deck: each track with a validated
        FireRed vocal-activity timeline gets its vocals protected, while a
        track without one is planned on energy facts alone.

        :param fade_out_analysis: Analysis data for the outgoing track.
        :param fade_in_analysis: Analysis data for the incoming track.
        :param buffer_duration: Length in seconds of the available fade-out holdback.
        """
        ctx = build_transition_context(
            fade_out_analysis, fade_in_analysis, buffer_duration, self.logger
        )
        factory = CandidateFactory(ctx, self.logger)
        specs = [spec for generator in default_generators() for spec in generator.generate(ctx)]
        candidates = [candidate for spec in specs if (candidate := factory.build(spec)) is not None]
        if self.logger.isEnabledFor(VERBOSE_LOG_LEVEL):
            self.logger.log(
                VERBOSE_LOG_LEVEL,
                "generated %d specs (%s), %d built",
                len(specs),
                dict(Counter(spec.source for spec in specs)),
                len(candidates),
            )
        if not candidates:
            raise SmartFadeNotApplicable("no feasible transition candidate")
        # a beatmatchable pair keeps its blend, so here a segue may only replace a cut;
        # when every other candidate is rejected, the rescue pass weighs it
        selector = CandidateSelector(default_policies(), self.logger, segue_replaces_cuts_only=True)
        winner = selector.select(candidates, ctx)
        rescue_pass = winner is None
        if rescue_pass:
            # every candidate was rejected, or only segues survived: retry with the
            # ungated audible-end ladder, a modest late-anchored rescue rung and
            # a segue (also for a beatmatchable pair) before falling back to the handoff
            rescue_specs = [
                *TrimClosingAnchorGenerator(min_gap=0.0).generate(ctx),
                *RescueAnchorGenerator().generate(ctx),
                *SegueGenerator(allow_blend_context=True).generate(ctx),
            ]
            rescue_candidates = [
                candidate for spec in rescue_specs if (candidate := factory.build(spec)) is not None
            ]
            rescue_selector = CandidateSelector(default_policies(), self.logger)
            winner = rescue_selector.select(rescue_candidates, ctx) if rescue_candidates else None
        if winner is None:
            # a plain volume crossfade reads far less abrupt than the click-free
            # handoff, so it ships unless its vocal collision is too severe
            fallback = FallbackCrossfadeFactory(ctx, factory, self.logger).build()
            if fallback is not None:
                plan, source = fallback, "fallback-crossfade"
            else:
                plan = EmergencyHandoffFactory(ctx, factory, self.logger).build()
                source = "emergency-handoff"
            bars = None
        else:
            plan = PlanAssembler(ctx, self.logger).finalize(winner.candidate)
            source = winner.candidate.spec.source
            bars = None if plan.style is TransitionStyle.SEGUE else winner.candidate.spec.bars
            if rescue_pass:
                source += " (rescue pass)"
        self._log_plan(ctx, plan, source, bars)
        # the caller reads the outgoing grid off the planner after a successful
        # plan and expects it masked to the plan's own anchor
        self.outgoing = replace(
            ctx.outgoing,
            beats=ctx.outgoing.beats[ctx.outgoing.beats <= plan.fade_out_window],
            downbeats=ctx.outgoing.downbeats[ctx.outgoing.downbeats <= plan.fade_out_window],
        )
        return plan

    def _log_plan(
        self, ctx: TransitionContext, plan: TransitionPlan, source: str, bars: int | None
    ) -> None:
        """
        Log the one DEBUG line that sums up the shipped plan.

        :param ctx: The transition's context.
        :param plan: The plan that ships.
        :param source: The winning candidate's generator, or the fallback/handoff that shipped.
        :param bars: The winning candidate's bar count; None for an unphrased plan.
        """
        trigger = None
        if plan.tier is TransitionTier.QUICK_FADE:
            # meter and tempo do not depend on the anchor, so a blend context whose
            # shipped candidate re-anchored into a quick fade lost its beat grid
            trigger = ctx.quick_fade_trigger or QuickFadeTrigger.BEAT_GRID
        self.logger.debug(
            "planned transition: style=%s tier=%s%s strategy=%s source=%s%s overlap=%.2fs "
            "bpm=%.1f->%.1f (%+.1f%%)%s",
            plan.style,
            plan.tier.value,
            f" trigger={trigger}" if trigger is not None else "",
            plan.metrics.strategy,
            source,
            f" bars={bars}" if bars is not None else "",
            plan.crossfade_duration,
            ctx.outgoing.bpm,
            ctx.incoming.bpm,
            (ctx.incoming.bpm / ctx.outgoing.bpm - 1.0) * 100,
            f' reason="{_segue_reason(ctx, plan)}"' if plan.style is TransitionStyle.SEGUE else "",
        )


def _segue_reason(ctx: TransitionContext, plan: TransitionPlan) -> str:
    """Describe what a segue overlaps: the quiet material, its curves, who sings, who kicks."""
    assert ctx.segue is not None  # a segue is only generated from its facts
    out_duty, in_duty = _window_duties(ctx, plan.crossfade_duration)
    vocals = _sides(
        None if out_duty is None else out_duty > _SINGS_DUTY,
        None if in_duty is None else in_duty > _SINGS_DUTY,
    )
    start = plan.fade_out_window - plan.crossfade_duration
    kick = _sides(
        None if ctx.kick_out is None else _overlaps(ctx.kick_out, start, plan.fade_out_window),
        None if ctx.kick_in is None else _overlaps(ctx.kick_in, 0.0, plan.crossfade_duration),
    )
    return (
        f"quiet tail {ctx.segue.quiet_tail:.1f}s + head {ctx.segue.quiet_head:.1f}s, "
        f"curves {plan.fadeout_curve}/{plan.fadein_curve}, vocals {vocals}, kick {kick}"
    )


def _sides(out: bool | None, inc: bool | None) -> str:
    """Name which deck has something: both, out-only, in-only, none, or unknown."""
    if out is None or inc is None:
        return "unknown"
    if out and inc:
        return "both"
    if out:
        return "out-only"
    return "in-only" if inc else "none"


def _overlaps(runs: tuple[tuple[float, float], ...], start: float, end: float) -> bool:
    """Whether any run overlaps the window."""
    return any(left < end and right > start for left, right in runs)
