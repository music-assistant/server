"""
Smart Fades - the candidate/policy transition planner.

``SmartCrossFadePlanner.plan()`` is a thin orchestration of the pipeline:
build the immutable ``TransitionContext``, let the generators propose
candidate specs, build each into a timed candidate, score them all with the
veto/penalty policies, finalize the winner's EQ - or, when every candidate
is vetoed, ship the click-free emergency handoff. Alternative transition
strategies slot in as sibling ``TransitionPlanner`` subclasses.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import replace
from typing import TYPE_CHECKING

from music_assistant.controllers.streams.smart_fades.models import SmartFadeNotApplicable

from .assembly import EmergencyHandoffFactory, PlanAssembler
from .candidates import CandidateFactory, default_generators
from .context import build_transition_context
from .policies import default_policies
from .selection import CandidateSelector

if TYPE_CHECKING:
    import logging

    from music_assistant.controllers.streams.smart_fades.models import TransitionPlan
    from music_assistant.models.audio_analysis import AudioAnalysisData


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

        Vocal-aware policies engage only when both tracks carry a validated
        FireRed vocal-activity timeline; otherwise the same pipeline yields
        the energy-only plan, with default (energy-only) metrics.

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
        if not candidates:
            raise SmartFadeNotApplicable("no feasible transition candidate")
        winner = CandidateSelector(default_policies(), self.logger).select(candidates, ctx)
        if winner is None:
            # every candidate breached a hard veto: ship the click-free handoff
            plan = EmergencyHandoffFactory(ctx, factory, self.logger).build()
        else:
            plan = PlanAssembler(ctx, self.logger).finalize(winner.candidate)
        # the caller reads the outgoing grid off the planner after a successful
        # plan and expects it masked to the plan's own anchor
        self.outgoing = replace(
            ctx.outgoing,
            beats=ctx.outgoing.beats[ctx.outgoing.beats <= plan.fade_out_window],
            downbeats=ctx.outgoing.downbeats[ctx.outgoing.downbeats <= plan.fade_out_window],
        )
        return plan
