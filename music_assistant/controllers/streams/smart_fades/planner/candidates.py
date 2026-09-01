"""
Smart Fades - candidate value objects and the timed-candidate factory.

A ``CandidateSpec`` is a generator's declared intent (which tier rung, which
anchor/entry, which generator produced it) before any plan exists; a
``Candidate`` is that spec paired with its timed ``TransitionPlan`` and
computed ``PlanMetrics``, ready for policies to score. The factory builds
TIMED candidates only - anchor, overlap timing, tempo ramp, trims and metrics.
EQ is deliberately absent: scoring never needs it, so the winner-only
``PlanAssembler`` applies it after selection.

Every ``build()`` derives its anchored tail fresh from the immutable
``TransitionContext``, which is what replaces the old planner's
restore-pristine/re-anchor scratchpad: two builds of the same spec are
guaranteed to yield identical candidates.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from music_assistant.constants import VERBOSE_LOG_LEVEL
from music_assistant.controllers.streams.smart_fades.helpers import (
    MIN_EFFECTIVE_FADE_BUFFER,
    SMART_CROSSFADE_DURATION,
    compute_gradual_tempo_steps,
    generate_synthetic_timestamps,
)
from music_assistant.controllers.streams.smart_fades.models import (
    FadeOutTrim,
    PlanMetrics,
    TempoPlan,
    TransitionPlan,
    TransitionStrategy,
    TransitionTier,
)
from music_assistant.controllers.streams.smart_fades.structure import point_in_mask
from music_assistant.controllers.streams.smart_fades.vocal import (
    collision_metrics,
    mask_saturated,
    merge_windows,
)

from .context import TIME_STRETCH_BPM_PERCENTAGE_THRESHOLD, choose_tier

if TYPE_CHECKING:
    import logging
    from collections.abc import Iterable

    import numpy as np
    import numpy.typing as npt

    from music_assistant.controllers.streams.smart_fades.models import BandProfile

    from .context import TransitionContext

# Overlap length per tier, in bars of the outgoing grid (research: real DJ
# transitions cluster at 32 beats = 8 bars; the doubled 16-bar blend is
# earned only when FireRed shows both decks near-instrumental, where the
# long exposure carries no vocal-collision risk)
_FULL_BLEND_BARS: int = 8
_INSTRUMENTAL_BLEND_BARS: int = 16
# vocal duty (unpadded mask coverage) at or under this on BOTH decks
# qualifies as near-instrumental
_INSTRUMENTAL_DUTY_MAX: float = 0.05
# The most of the incoming track's head a candidate may cut beyond what its own
# overlap plays under the outgoing track: a deeper cut skips audio the listener
# never hears any part of.
_MAX_UNHEARD_INTRO_S: float = 2.0
_TEMPO_BLEND_BARS: int = 8
# QUICK_FADE bars by BPM incompatibility: (max diff %, bars); beyond -> 1 bar
_QUICK_FADE_LADDER: tuple[tuple[float, int], ...] = ((12.0, 4), (20.0, 2))
# phrase-aligned rung set every ladder walks, largest first
RUNG_LADDER: tuple[int, ...] = (16, 8, 4, 2, 1)

# Deep-trim guard: a bar is protected when its low band is silent but its
# voice/melody bands are active (cutting there beheads a sung intro)
_TRIM_GUARD_LOW_FLOOR: float = 0.25
_TRIM_GUARD_VOICE_FLOOR: float = 0.4

# Trim-closing anchors engage only when the audible end sits this much past
# the energy anchor; below it the default anchor leads the main pass (the
# rescue pass re-runs this generator ungated when that pass rejects everything)
_TRIM_CLOSING_MIN_GAP_S: float = 8.0

# Lazy-overlay length: a long unphrased equal-power blend, not a rung on any ladder
_LAZY_OVERLAY_SECONDS: float = 16.0
# both decks at or under this in-window vocal duty qualify as ambient
_LAZY_DUTY_MAX: float = 0.10


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


def earns_instrumental_blend(ctx: TransitionContext) -> bool:
    """Whether verified near-instrumental decks earn the doubled full-blend overlap."""
    duties = _vocal_duties(ctx)
    if duties is None:
        return False
    out_duty, in_duty = duties
    return out_duty <= _INSTRUMENTAL_DUTY_MAX and in_duty <= _INSTRUMENTAL_DUTY_MAX


def bars_ladder(ctx: TransitionContext, tier: TransitionTier) -> list[int]:
    """Candidate bar counts to try for a tier, largest first (shorter rungs fit smaller buffers)."""
    if tier is TransitionTier.QUICK_FADE:
        # a mismatched meter has no shared bar grid to blend across; cap short
        # regardless of how close the tempos happen to be
        ladder = ((0.0, 2),) if ctx.cross_meter else _QUICK_FADE_LADDER
        ideal = next((bars for limit, bars in ladder if ctx.bpm_diff_percent <= limit), 1)
    elif tier is TransitionTier.TEMPO_BLEND:
        ideal = _TEMPO_BLEND_BARS
    elif earns_instrumental_blend(ctx):
        ideal = _INSTRUMENTAL_BLEND_BARS
    else:
        ideal = _FULL_BLEND_BARS
    return [bars for bars in RUNG_LADDER if bars <= ideal]


class CandidateGenerator(ABC):
    """One source of candidate specs; generators only emit, the factory validates feasibility."""

    name: str

    @abstractmethod
    def generate(self, ctx: TransitionContext) -> Iterable[CandidateSpec]:
        """Emit this generator's candidate specs for one transition, best first."""


class EnergyLadderGenerator(CandidateGenerator):
    """Emits the tier's bar-count ladder at each viable anchor, across entry options."""

    name = "energy-ladder"

    def generate(self, ctx: TransitionContext) -> Iterable[CandidateSpec]:
        """Emit one spec per (rung, entry option) at the default, full-band and pinned anchors."""
        ladder = bars_ladder(ctx, ctx.tier)
        ideal = ladder[0]
        # the default anchor is already kick-folded; a pure full-band variant
        # would let a longer blend win past the kick die-out, defeating the
        # researched kick handover, so it is deliberately not emitted (trim-closing
        # may still anchor later when >=8s of audible tail would otherwise be stranded)
        anchors: list[float | None] = [None]
        a_pin = _fade_onset_pin(ctx)
        if a_pin < ctx.default_anchor:
            anchors.append(a_pin)
        for anchor in anchors:
            for bars in ladder:
                # the default anchor keeps the factory-chosen entry (grid
                # alignment + rolling intro), exactly like the old energy
                # candidate; explicit entry options are pin-scoped, like the
                # old remediation rungs they came from
                entries = [None] if anchor is None else _entry_options(ctx, bars)
                for entry in entries:
                    yield CandidateSpec(
                        tier=ctx.tier,
                        bars=bars,
                        anchor_s=anchor,
                        entry_s=entry,
                        source=self.name,
                        ideal_bars=ideal,
                    )


class CodaAnchorGenerator(CandidateGenerator):
    """Emits candidates anchored inside the outgoing track's validated coda/outro zone."""

    name = "coda-anchor"

    def generate(self, ctx: TransitionContext) -> Iterable[CandidateSpec]:
        """Emit the zone's fitting rung(s) at the coda anchor, or nothing without a valid zone."""
        # coda shifting is a vocal-collision remediation: without a vocal
        # timeline the zone was validated against an empty mask and the old
        # planner never coda-shifted, so stay inactive on the energy-only path
        if ctx.vocal_out_placement is None:
            return
        if ctx.coda_zone is None or ctx.outgoing_profile is None:
            return
        zone = ctx.coda_zone
        bar_starts = ctx.outgoing_profile.bar_starts
        in_zone = bar_starts[(bar_starts >= zone.start_s) & (bar_starts <= zone.end_s)]
        if not len(in_zone):
            return
        anchor = float(in_zone[-1]) - ctx.buffer_offset
        if anchor < MIN_EFFECTIVE_FADE_BUFFER:
            return
        bar_a = ctx.outgoing.beats_per_bar * 60.0 / ctx.outgoing.bpm
        zone_bars = int((zone.end_s - zone.start_s) / bar_a)
        top_rung = next((n for n in (16, 8, 4, 2) if n <= zone_bars), None)
        if top_rung is None:
            return
        for bars in dict.fromkeys((top_rung, 2)):
            for entry in _entry_options(ctx, bars):
                yield CandidateSpec(
                    tier=ctx.tier,
                    bars=bars,
                    anchor_s=anchor,
                    entry_s=entry,
                    source=self.name,
                    ideal_bars=top_rung,
                )


class ProtectiveAnchorGenerator(CandidateGenerator):
    """Emits the tier's ladder anchored to keep A's last outgoing vocal phrase intact."""

    name = "protective-anchor"

    def generate(self, ctx: TransitionContext) -> Iterable[CandidateSpec]:
        """Emit every ladder rung at the nearest anchor that doesn't truncate A's last phrase."""
        if ctx.vocal_out_placement is None or not ctx.vocal_out_placement.windows:
            return
        target = _outgoing_vocal_end(ctx)
        anchor = _nearest_protective_anchor(ctx, target)
        ladder = bars_ladder(ctx, ctx.tier)
        ideal = ladder[0]
        bar_seconds = ctx.outgoing.beats_per_bar * 60.0 / ctx.outgoing.bpm
        for bars in ladder:
            # the old protection had two re-anchor triggers: cover the last
            # vocal phrase, and close a short fade's audible-trim gap; emit
            # both anchors so each stays reachable as a candidate
            anchors = [anchor]
            trim_target = ctx.audio_end - bars * bar_seconds
            if trim_target > anchor:
                trim_anchor = _nearest_protective_anchor(ctx, trim_target, prefer_earliest=False)
                if trim_anchor not in anchors:
                    anchors.append(trim_anchor)
            # the factory-chosen (beat-aligned) entry leads, as the old
            # protection rebuild pinned it; explicit groove/natural options
            # follow so a remediated entry stays reachable too
            for rung_anchor in anchors:
                for entry in [None, *_entry_options(ctx, bars)]:
                    yield CandidateSpec(
                        tier=ctx.tier,
                        bars=bars,
                        anchor_s=rung_anchor,
                        entry_s=entry,
                        source=self.name,
                        ideal_bars=ideal,
                    )


class VocalOnsetEntryGenerator(CandidateGenerator):
    """Emits an entry that lands B's first vocal onset exactly at the overlap end."""

    name = "vocal-onset-entry"

    def generate(self, ctx: TransitionContext) -> Iterable[CandidateSpec]:
        """Emit the vocal-onset-aligned entry at the tier's ideal rung, or nothing when illegal."""
        if ctx.vocal_in_placement is None or not ctx.vocal_in_placement.windows:
            return
        if mask_saturated(ctx.vocal_in_placement, float(SMART_CROSSFADE_DURATION)):
            return
        ideal = bars_ladder(ctx, ctx.tier)[0]
        bar_b = ctx.incoming.beats_per_bar * 60.0 / ctx.incoming.bpm
        entry = ctx.vocal_in_placement.windows[0][0] - ideal * bar_b
        if entry < 0.0 or point_in_mask(ctx.vocal_in_placement, entry):
            return
        a_pin = _fade_onset_pin(ctx)
        anchors: list[float | None] = [a_pin if a_pin < ctx.default_anchor else None]
        # the old planner's protection rebuild preserved a remediated entry at
        # the protective anchor; emitting the combination keeps that reachable
        if ctx.vocal_out_placement is not None and ctx.vocal_out_placement.windows:
            protective = _nearest_protective_anchor(
                ctx, min(ctx.vocal_out_placement.last_end(), ctx.audio_end)
            )
            if protective not in anchors and protective != ctx.default_anchor:
                anchors.append(protective)
        for anchor in anchors:
            yield CandidateSpec(
                tier=ctx.tier,
                bars=ideal,
                anchor_s=anchor,
                entry_s=entry,
                source=self.name,
                ideal_bars=ideal,
            )


class RescueAnchorGenerator(CandidateGenerator):
    """Emits a modest, late-anchored rung as a last resort before the emergency handoff."""

    name = "rescue-anchor"

    def generate(self, ctx: TransitionContext) -> Iterable[CandidateSpec]:
        """Emit 1-2 bar rungs anchored as late as the tail allows, never past A's own vocal end."""
        full_ladder = bars_ladder(ctx, ctx.tier)
        bar_seconds = ctx.outgoing.beats_per_bar * 60.0 / ctx.outgoing.bpm
        last_vocal_end = _outgoing_vocal_end(ctx)
        for bars in [rung for rung in full_ladder if rung <= 2]:
            target = min(ctx.audio_end, max(ctx.audio_end - bars * bar_seconds, last_vocal_end))
            anchor = _nearest_protective_anchor(ctx, target, prefer_earliest=False)
            for entry in [None, *_entry_options(ctx, bars)]:
                yield CandidateSpec(
                    tier=ctx.tier,
                    bars=bars,
                    anchor_s=anchor,
                    entry_s=entry,
                    source=self.name,
                    ideal_bars=full_ladder[0],
                )


class TrimClosingAnchorGenerator(CandidateGenerator):
    """Emits the tier's ladder at that anchor, at the audible end, when a large tail is stranded."""

    name = "trim-closing-anchor"

    def __init__(self, min_gap: float = _TRIM_CLOSING_MIN_GAP_S) -> None:
        """Initialize the generator with the smallest stranded-tail gap that engages it."""
        self._min_gap = min_gap

    def generate(self, ctx: TransitionContext) -> Iterable[CandidateSpec]:
        """Emit every ladder rung at the audible end, or nothing when the trim gap is small."""
        anchor = ctx.audio_end
        if anchor - ctx.default_anchor < self._min_gap:
            return
        # ctx.tier is decided at the early anchor; the grid can be blendable at the audible end
        _, tier = choose_tier(ctx.outgoing, ctx.incoming, anchor)
        ladder = bars_ladder(ctx, tier)
        for bars in ladder:
            yield CandidateSpec(
                tier=tier,
                bars=bars,
                anchor_s=anchor,
                entry_s=None,
                source=self.name,
                ideal_bars=ladder[0],
            )


class LazyOverlayGenerator(CandidateGenerator):
    """Emits one long unphrased overlay when the grid is unusable but both decks are ambient."""

    name = "lazy-overlay"

    def generate(self, ctx: TransitionContext) -> Iterable[CandidateSpec]:
        """Emit the overlay spec, or nothing when the pair doesn't qualify."""
        if ctx.tier is not TransitionTier.QUICK_FADE or ctx.cross_meter:
            return
        if ctx.bpm_diff_percent > TIME_STRETCH_BPM_PERCENTAGE_THRESHOLD:
            return
        duties = _window_duties(ctx, _LAZY_OVERLAY_SECONDS)
        if duties is None or duties[0] > _LAZY_DUTY_MAX or duties[1] > _LAZY_DUTY_MAX:
            return
        yield CandidateSpec(
            tier=ctx.tier,
            bars=1,
            anchor_s=ctx.audio_end,
            entry_s=None,
            strategy=TransitionStrategy.LAZY_OVERLAY,
            source=self.name,
            ideal_bars=1,
        )


def default_generators() -> tuple[CandidateGenerator, ...]:
    """Return the standard generator set, in preference order (best first)."""
    return (
        EnergyLadderGenerator(),
        CodaAnchorGenerator(),
        ProtectiveAnchorGenerator(),
        VocalOnsetEntryGenerator(),
        LazyOverlayGenerator(),
        TrimClosingAnchorGenerator(),
    )


class CandidateFactory:
    """Builds timed candidates from specs, purely over the transition context."""

    def __init__(self, ctx: TransitionContext, logger: logging.Logger) -> None:
        """Initialize the factory for one transition."""
        self._ctx = ctx
        self._logger = logger

    def build(self, spec: CandidateSpec) -> Candidate | None:
        """
        Build one complete timed candidate for a spec, or ``None`` when it is infeasible.

        Every timing, tempo and trim decision is derived fresh from the
        context and the spec's anchor - a candidate never inherits state from
        a previously built one. Infeasible means the spec's bar count needs
        more room than the incoming buffer has, or its entry leaves no legal
        alignment; a 1-bar spec never fails this way, matching the plan floor.
        The returned candidate's spec reflects what was actually built: a
        re-anchored tail can downgrade the tier and cap the bar count.
        """
        if spec.strategy is TransitionStrategy.LAZY_OVERLAY:
            return self._build_lazy_overlay(spec)
        tail = self._anchored_tail(spec.anchor_s)
        # a re-anchored tail can downgrade the tier (shorter/irregular grid); the
        # requested bar count still reflects the old tier, so cap it at the new
        # tier's largest rung or a long overlap ships without its tempo ramp
        _, tier = choose_tier(self._ctx.outgoing, self._ctx.incoming, tail.effective_end)
        bars_cap = bars_ladder(self._ctx, tier)[0]
        bars = min(spec.bars, bars_cap)

        fadein_start_pos = (
            spec.entry_s if spec.entry_s is not None else self._choose_fadein_entry(tail, bars)
        )
        if bars > 1 and fadein_start_pos is None:
            self._logger.log(
                VERBOSE_LOG_LEVEL,
                "dropping spec source=%s tier=%s bars=%d anchor=%s: no beat-aligned incoming entry",
                spec.source,
                tier,
                bars,
                spec.anchor_s,
            )
            return None
        crossfade_duration = self._calculate_crossfade_duration(tail, bars)

        tempo_plan = self._choose_tempo_ramp(tier, tail, crossfade_duration)
        crossfade_duration, fadein_trim_start = self._lock_in_timing(
            tail, crossfade_duration, fadein_start_pos, tempo_plan
        )
        if bars > 1 and fadein_start_pos is not None and fadein_trim_start is None:
            self._logger.log(
                VERBOSE_LOG_LEVEL,
                "dropping spec source=%s tier=%s bars=%d anchor=%s: "
                "no legal timing lock for the pinned entry",
                spec.source,
                tier,
                bars,
                spec.anchor_s,
            )
            return None
        # Rolling-intro alignment: on a full blend with no pinned entry, deepen B's
        # trim so its groove entry lands at the overlap END (B's intro runs under A,
        # its drop hits where A's music dies). A sung run that no legal cut clears
        # is infeasible so the caller's ladder drops to a shorter overlap instead —
        # except at the 1-bar floor, which must always yield a candidate: there the
        # un-deepened trim ships as-is.
        if tier is TransitionTier.FULL_BLEND and spec.entry_s is None:
            feasible, aligned = self._align_rolling_intro(crossfade_duration, fadein_trim_start)
            if not feasible:
                if bars > 1:
                    self._logger.log(
                        VERBOSE_LOG_LEVEL,
                        "dropping spec source=%s tier=%s bars=%d anchor=%s: "
                        "rolling intro: no legal cut clears the sung run",
                        spec.source,
                        tier,
                        bars,
                        spec.anchor_s,
                    )
                    return None
            else:
                fadein_trim_start = aligned
        if (
            fadein_trim_start is not None
            and fadein_trim_start > crossfade_duration + _MAX_UNHEARD_INTRO_S
        ):
            # deeper than the blend justifies: more of the incoming track would be
            # skipped than the listener hears blended, so it plays from its head
            self._logger.log(
                VERBOSE_LOG_LEVEL,
                "stripping fade-in trim for spec source=%s tier=%s: %.2fs trim exceeds "
                "the %.2fs overlap",
                spec.source,
                tier,
                fadein_trim_start,
                crossfade_duration,
            )
            fadein_trim_start = None

        plan = TransitionPlan(
            tier=tier,
            fade_out_window=tail.effective_end,
            crossfade_duration=crossfade_duration,
            tempo_plan=tempo_plan,
            fadeout_trim=tail.fadeout_trim,
            fadein_trim_start=fadein_trim_start,
        )
        built_spec = replace(spec, tier=tier, bars=bars)
        return Candidate(
            spec=built_spec,
            plan=plan,
            metrics=self._score(built_spec, plan),
            ideal_bars=spec.ideal_bars or spec.bars,
        )

    def score(self, spec: CandidateSpec, plan: TransitionPlan) -> PlanMetrics:
        """Score an arbitrary (spec, plan) pair against this context, for a plan edited post-build."""
        return self._score(spec, plan)

    @property
    def _bpm_ratio(self) -> float:
        """Tempo ratio between the incoming and outgoing track."""
        return self._ctx.incoming.bpm / self._ctx.outgoing.bpm

    def _anchored_tail(self, anchor_s: float | None) -> _AnchoredTail:
        """Derive the tail state for an anchor, never later than the RMS-audible boundary."""
        import numpy as np  # noqa: PLC0415

        ctx = self._ctx
        anchor = anchor_s if anchor_s is not None else ctx.default_anchor
        effective_end = min(anchor, ctx.audio_end)
        # same sub-half-second slack rule as the tail cue: the rendered stream
        # still ends at the buffer end, so the anchor must follow it
        fadeout_trim: FadeOutTrim | None
        if effective_end >= ctx.buffer_duration - 0.5:
            effective_end = ctx.buffer_duration
            fadeout_trim = None
        else:
            fadeout_trim = FadeOutTrim(
                end_pos=effective_end,
                trimmed_seconds=ctx.buffer_duration - effective_end,
            )
        protective = np.asarray(ctx.protective_downbeats, dtype=np.float32)
        return _AnchoredTail(
            effective_end=effective_end,
            fadeout_trim=fadeout_trim,
            beats=ctx.outgoing.beats[ctx.outgoing.beats <= effective_end],
            downbeats=ctx.outgoing.downbeats[ctx.outgoing.downbeats <= effective_end],
            # protective downbeats reach all the way to audio_end, so they cover
            # any position an anchor could have chosen
            extrapolated_downbeats=protective[protective <= effective_end],
        )

    def _choose_fadein_entry(self, tail: _AnchoredTail, crossfade_bars: int) -> float | None:
        """Choose where the incoming track enters, aligned to its beat grid."""

        def calculate_beat_positions(
            fade_out_beats: npt.NDArray[np.float32],
            fade_in_beats: npt.NDArray[np.float32],
            num_beats: int,
        ) -> float | None:
            """Calculate start positions from beat arrays."""
            if len(fade_out_beats) < num_beats or len(fade_in_beats) < num_beats:
                return None

            fade_in_slice = fade_in_beats[:num_beats]
            return float(fade_in_slice[0])

        # Try downbeats first for most musical timing
        downbeat_positions = calculate_beat_positions(
            tail.extrapolated_downbeats, self._ctx.incoming.downbeats, crossfade_bars
        )
        if downbeat_positions is not None:
            return downbeat_positions

        # Try regular beats if downbeats insufficient
        required_beats = crossfade_bars * self._ctx.incoming.beats_per_bar
        beat_positions = calculate_beat_positions(
            tail.beats, self._ctx.incoming.beats, required_beats
        )
        if beat_positions is not None:
            return beat_positions

        # Fallback: No beat alignment possible
        self._logger.log(VERBOSE_LOG_LEVEL, "No beat alignment possible (insufficient beats)")
        return None

    def _calculate_crossfade_duration(self, tail: _AnchoredTail, crossfade_bars: int) -> float:
        """Calculate the crossfade duration for a bar count, capped to the audible tail."""
        downbeats = tail.downbeats
        bar_seconds = self._ctx.outgoing.beats_per_bar * 60.0 / self._ctx.outgoing.bpm
        # the downbeat span assumes the anchor sits (near) the last downbeat;
        # when the grid dies early (unsnapped anchor), the anchor gap would be
        # added to EVERY rung — a "1-bar" quick fade could span the whole gap
        if (
            len(downbeats) > crossfade_bars
            and tail.effective_end - float(downbeats[-1]) < bar_seconds
        ):
            # the real span between downbeats honors the track's own tempo/rubato
            # more precisely than a constant-BPM estimate
            musical_duration = float(
                tail.effective_end - downbeats[len(downbeats) - 1 - crossfade_bars]
            )
        else:
            seconds_per_beat = 60.0 / self._ctx.incoming.bpm
            musical_duration = crossfade_bars * self._ctx.incoming.beats_per_bar * seconds_per_beat

        # Cap at the audible fade-out room so crossfade_start never goes negative
        # downstream (effective_end <= SMART_CROSSFADE_DURATION always)
        actual_duration = min(musical_duration, tail.effective_end)

        if musical_duration > actual_duration:
            self._logger.log(
                VERBOSE_LOG_LEVEL,
                "Constraining crossfade duration from %.1fs to %.1fs (audible tail limit)",
                musical_duration,
                actual_duration,
            )

        return actual_duration

    def _choose_tempo_ramp(
        self, tier: TransitionTier, tail: _AnchoredTail, crossfade_duration: float
    ) -> TempoPlan:
        """Choose the gradual tempo ramp that beatmatches the outgoing track, if any."""
        if tier is TransitionTier.QUICK_FADE:
            return TempoPlan()
        if not 0.1 < self._ctx.bpm_diff_percent <= TIME_STRETCH_BPM_PERCENTAGE_THRESHOLD:
            return TempoPlan()
        return TempoPlan(steps=self._compute_tempo_steps(tail, crossfade_duration))

    def _compute_tempo_steps(
        self, tail: _AnchoredTail, crossfade_duration: float
    ) -> list[tuple[float, float]]:
        """Compute the gradual tempo ramp in the 10s window before the crossfade."""
        stretch_duration = 10.0
        crossfade_start = tail.effective_end - crossfade_duration
        # A crossfade consuming the whole audible tail leaves no room for a
        # pre-fade tempo ramp
        if crossfade_start <= 0:
            return []
        stretch_start = max(0.0, crossfade_start - stretch_duration)
        stretch_end = crossfade_start

        # Collect timing points within the stretch window
        beats = tail.beats
        beat_mask = (beats >= stretch_start) & (beats <= stretch_end)
        db_mask = (tail.extrapolated_downbeats >= stretch_start) & (
            tail.extrapolated_downbeats <= stretch_end
        )
        window_beats = beats[beat_mask] - stretch_start
        window_downbeats = tail.extrapolated_downbeats[db_mask] - stretch_start

        # >3% BPM diff: beat-level stepping (more steps = smoother)
        # <=3%: downbeat-level stepping, fall back to beats if too few
        if self._ctx.bpm_diff_percent > 3.0:
            stretch_timestamps = window_beats
        elif len(window_downbeats) >= 2:
            stretch_timestamps = window_downbeats
        else:
            stretch_timestamps = window_beats

        # Fall back to synthetic timestamps when < 2 real timestamps
        if len(stretch_timestamps) < 2:
            stretch_timestamps = generate_synthetic_timestamps(
                stretch_end - stretch_start,
                self._ctx.outgoing.bpm,
                beats_per_bar=self._ctx.outgoing.beats_per_bar,
            )

        tempo_steps = compute_gradual_tempo_steps(
            start_ratio=1.0,
            end_ratio=self._bpm_ratio,
            downbeats=stretch_timestamps,
        )
        if not tempo_steps:
            tempo_steps = [(0.0, self._bpm_ratio)]

        # Shift timestamps back to buffer-relative coordinates for FFmpeg
        return [(ts + stretch_start, ratio) for ts, ratio in tempo_steps]

    def _lock_in_timing(
        self,
        tail: _AnchoredTail,
        crossfade_duration: float,
        fadein_start_pos: float | None,
        tempo_plan: TempoPlan,
    ) -> tuple[float, float | None]:
        """
        Lock the overlap timing: confirm the fade-in entry and snap to downbeats.

        Returns the final crossfade duration (downbeat-snapped and compensated
        for time-stretch compression) and the fade-in trim position, or ``None``
        when beat alignment is skipped.

        :param tail: The anchored tail the candidate is built on.
        :param crossfade_duration: Draft crossfade duration in seconds.
        :param fadein_start_pos: Chosen entry point in the incoming track, if any.
        :param tempo_plan: The tempo ramp chosen for this transition.
        """
        # Adjust crossfade duration to align with outgoing track's downbeats.
        # When stretching, only consider downbeats after the stretch window
        # to ensure the outgoing track has reached the target tempo.
        crossfade_start = tail.effective_end - crossfade_duration
        crossfade_duration = self._adjust_crossfade_to_downbeats(
            tail,
            crossfade_duration=crossfade_duration,
            fadein_start_pos=fadein_start_pos,
            min_downbeat_pos=crossfade_start if tempo_plan else 0.0,
            render_ratio=self._bpm_ratio if tempo_plan else 1.0,
        )

        # Compensate crossfade duration for time-stretch compression.
        # Gate on the tempo plan (not stretch eligibility) so a guard-skipped
        # stretch doesn't apply a compensation for a stretch that never ran.
        if tempo_plan:
            crossfade_duration = crossfade_duration / self._bpm_ratio

        fadein_trim_start: float | None = None
        if (
            fadein_start_pos is not None
            and fadein_start_pos + crossfade_duration <= SMART_CROSSFADE_DURATION
        ):
            fadein_trim_start = fadein_start_pos
        else:
            self._logger.log(
                VERBOSE_LOG_LEVEL,
                "Skipping beat alignment: not enough audio after trim (%s + %.1fs > %.1fs)",
                fadein_start_pos,
                crossfade_duration,
                SMART_CROSSFADE_DURATION,
            )

        return crossfade_duration, fadein_trim_start

    def _adjust_crossfade_to_downbeats(
        self,
        tail: _AnchoredTail,
        crossfade_duration: float,
        fadein_start_pos: float | None,
        min_downbeat_pos: float = 0.0,
        render_ratio: float = 1.0,
    ) -> float:
        """Adjust crossfade duration to align with outgoing track's downbeats."""
        # If we don't have downbeats or beat alignment is disabled, return original duration
        if len(tail.extrapolated_downbeats) == 0 or fadein_start_pos is None:
            return crossfade_duration

        # Calculate where the crossfade would start in the buffer
        ideal_start_pos = tail.effective_end - crossfade_duration

        self._logger.log(
            VERBOSE_LOG_LEVEL,
            "Downbeat adjustment - ideal_start=%.2fs (effective_end=%.1fs - crossfade=%.2fs), "
            "fadein_start=%.2fs",
            ideal_start_pos,
            tail.effective_end,
            crossfade_duration,
            fadein_start_pos,
        )

        # Find the closest downbeats (earlier and later)
        earlier_downbeat = None
        later_downbeat = None

        for downbeat in tail.extrapolated_downbeats:
            if downbeat < min_downbeat_pos:
                continue
            if downbeat <= ideal_start_pos:
                earlier_downbeat = downbeat
            elif downbeat > ideal_start_pos and later_downbeat is None:
                later_downbeat = downbeat
                break

        # Try earlier downbeat first (longer crossfade)
        if earlier_downbeat is not None:
            adjusted_duration = float(tail.effective_end - earlier_downbeat)
            if fadein_start_pos + adjusted_duration / render_ratio <= SMART_CROSSFADE_DURATION:
                if abs(adjusted_duration - crossfade_duration) > 0.1:
                    self._logger.log(
                        VERBOSE_LOG_LEVEL,
                        "Adjusted crossfade duration from %.2fs to %.2fs to align with "
                        "downbeat at %.2fs (earlier)",
                        crossfade_duration,
                        adjusted_duration,
                        earlier_downbeat,
                    )
                return adjusted_duration

        # Try later downbeat (shorter crossfade)
        if later_downbeat is not None:
            adjusted_duration = float(tail.effective_end - later_downbeat)
            if fadein_start_pos + adjusted_duration / render_ratio <= SMART_CROSSFADE_DURATION:
                if abs(adjusted_duration - crossfade_duration) > 0.1:
                    self._logger.log(
                        VERBOSE_LOG_LEVEL,
                        "Adjusted crossfade duration from %.2fs to %.2fs to align with "
                        "downbeat at %.2fs (later)",
                        crossfade_duration,
                        adjusted_duration,
                        later_downbeat,
                    )
                return adjusted_duration

        # If no suitable downbeat found, return original duration
        self._logger.log(
            VERBOSE_LOG_LEVEL,
            "Could not adjust crossfade duration to downbeats, using original %.2fs",
            crossfade_duration,
        )
        return crossfade_duration

    def _align_rolling_intro(
        self, crossfade_duration: float, fadein_trim_start: float | None
    ) -> tuple[bool, float | None]:
        """
        Deepen B's trim when the overlap can't cover its intro (else B's groove lands on dead air).

        Returns ``(feasible, trim)``: ``(False, None)`` when a sung run leaves
        no legal cut anywhere in the buffer, else the (possibly deepened) trim.
        """
        import numpy as np  # noqa: PLC0415

        entry = self._ctx.natural_entry
        trim = fadein_trim_start or 0.0
        if entry <= 0.0 or entry - trim <= crossfade_duration:
            return True, fadein_trim_start
        if entry > SMART_CROSSFADE_DURATION:
            # groove enters beyond the buffered head — unreachable defensively
            return True, fadein_trim_start
        deep_trim = entry - crossfade_duration
        downbeats = self._ctx.incoming.downbeats
        if len(downbeats):
            deep_trim = float(downbeats[np.argmin(np.abs(downbeats - deep_trim))])
        guarded = self._guard_deep_trim(deep_trim, crossfade_duration)
        if guarded is None:
            self._logger.log(
                VERBOSE_LOG_LEVEL,
                "Rolling intro: no legal cut clears the sung run within the buffer; "
                "dropping to a shorter overlap instead",
            )
            return False, None
        self._logger.log(
            VERBOSE_LOG_LEVEL,
            "Rolling intro: trimming %.1fs of pre-groove intro to keep the handover anchored",
            guarded,
        )
        return True, guarded

    def _guard_deep_trim(self, deep_trim: float, crossfade_duration: float) -> float | None:
        """Push a deep-trim cut off a protected run; ``None`` when no legal cut fits the buffer."""
        import numpy as np  # noqa: PLC0415

        # the cut is a minimum, so only search later: first unprotected downbeat at/after wins
        if self._ctx.incoming_profile is None:
            return deep_trim
        bar_starts = self._ctx.incoming_profile.bar_starts
        protected = self._protected_bars(self._ctx.incoming_profile)
        start_idx = int(np.searchsorted(bar_starts, deep_trim - 1e-6))
        if start_idx >= len(bar_starts) or not protected[start_idx]:
            return deep_trim
        for i in range(start_idx, len(protected)):
            if protected[i]:
                continue
            candidate = float(bar_starts[i])
            if candidate + crossfade_duration <= SMART_CROSSFADE_DURATION:
                return candidate
            break
        return None

    @staticmethod
    def _protected_bars(profile: BandProfile) -> npt.NDArray[np.bool_]:
        """Mark each bar of ``profile`` as protected: low-silent but voice/melody-active."""
        low = profile.bar_power["low"]
        low_mid = profile.bar_power["low_mid"]
        mid = profile.bar_power["mid"]
        low_silent = low < _TRIM_GUARD_LOW_FLOOR * profile.reference["low"]
        voice_active = (low_mid >= _TRIM_GUARD_VOICE_FLOOR * profile.reference["low_mid"]) | (
            mid >= _TRIM_GUARD_VOICE_FLOOR * profile.reference["mid"]
        )
        return low_silent & voice_active

    def _build_lazy_overlay(self, spec: CandidateSpec) -> Candidate:
        """Build the unphrased long-overlay candidate: anchored at the audible end, no alignment."""
        tail = self._anchored_tail(spec.anchor_s)
        plan = TransitionPlan(
            tier=spec.tier,
            fade_out_window=tail.effective_end,
            crossfade_duration=min(_LAZY_OVERLAY_SECONDS, tail.effective_end),
            tempo_plan=TempoPlan(),
            fadeout_trim=tail.fadeout_trim,
            fadein_trim_start=None,
        )
        return Candidate(
            spec=spec, plan=plan, metrics=self._score(spec, plan), ideal_bars=spec.ideal_bars
        )

    def _score(self, spec: CandidateSpec, plan: TransitionPlan) -> PlanMetrics:
        """Score a candidate: trims, retained vocal time, downbeat alignment, collision."""
        ctx = self._ctx
        audible_outgoing_trim = max(0.0, ctx.audio_end - plan.fade_out_window)
        anchor_on_downbeat = self._is_on_downbeat(plan.fade_out_window)
        # deliberate extension over the old planner (which never scored the
        # energy-only path): policies need real trim/downbeat facts on every
        # candidate; each vocal-dependent field needs only its own deck's mask
        outgoing_vocal_fade_seconds = 0.0
        collision_seconds = weighted_collision = 0.0
        if ctx.vocal_out_scoring is not None:
            outgoing_windows = self._rendered_outgoing_windows(plan)
            in_fade = [
                (max(0.0, left), min(plan.crossfade_duration, right))
                for left, right in outgoing_windows
                if right > 0.0 and left < plan.crossfade_duration
            ]
            outgoing_vocal_fade_seconds = sum(
                right - left for left, right in merge_windows(in_fade)
            )
            if ctx.vocal_in_scoring is not None:
                collision_seconds, weighted_collision = collision_metrics(
                    outgoing_windows,
                    self._rendered_incoming_windows(plan),
                    plan.crossfade_duration,
                )
        return PlanMetrics(
            strategy=spec.strategy,
            audible_outgoing_trim=audible_outgoing_trim,
            outgoing_vocal_fade_seconds=outgoing_vocal_fade_seconds,
            anchor_on_downbeat=anchor_on_downbeat,
            collision_seconds=collision_seconds,
            weighted_collision_seconds=weighted_collision,
        )

    def _rendered_outgoing_windows(self, plan: TransitionPlan) -> list[tuple[float, float]]:
        """Map the outgoing (unpadded) vocal scoring mask into rendered crossfade-local seconds."""
        assert self._ctx.vocal_out_scoring is not None  # narrowed by the caller
        rendered_anchor = self._rendered_time(plan, plan.fade_out_window)
        rendered_start = rendered_anchor - plan.crossfade_duration
        return [
            (
                self._rendered_time(plan, left) - rendered_start,
                self._rendered_time(plan, right) - rendered_start,
            )
            for left, right in self._ctx.vocal_out_scoring.windows
        ]

    def _rendered_incoming_windows(self, plan: TransitionPlan) -> list[tuple[float, float]]:
        """Map the incoming (unpadded) scoring mask into the plan's fadein-trim-relative seconds."""
        assert self._ctx.vocal_in_scoring is not None  # narrowed by the caller
        trim = plan.fadein_trim_start or 0.0
        return [(left - trim, right - trim) for left, right in self._ctx.vocal_in_scoring.windows]

    @staticmethod
    def _rendered_time(plan: TransitionPlan, input_time: float) -> float:
        """Map a buffer-local outgoing input-time position to its rendered-stream position."""
        clamped = max(0.0, min(input_time, plan.fade_out_window))
        return clamped - plan.tempo_plan.savings_until(clamped)

    def _is_on_downbeat(self, position: float, tolerance: float = 0.05) -> bool:
        """Whether a buffer-local position sits within tolerance of an outgoing downbeat."""
        import numpy as np  # noqa: PLC0415

        downbeats = np.asarray(self._ctx.protective_downbeats, dtype=np.float32)
        return bool(len(downbeats) and np.min(np.abs(downbeats - position)) <= tolerance)


@dataclass(frozen=True, slots=True)
class _AnchoredTail:
    """The outgoing tail derived for one anchor: masked grids, trim, and effective end."""

    effective_end: float
    fadeout_trim: FadeOutTrim | None
    beats: npt.NDArray[np.float32]
    downbeats: npt.NDArray[np.float32]
    extrapolated_downbeats: npt.NDArray[np.float32]


def _vocal_duties(ctx: TransitionContext) -> tuple[float, float] | None:
    """Outgoing/incoming vocal duty fractions the instrumental-blend/lazy-overlay gates key on."""
    if ctx.vocal_out_scoring is None or ctx.vocal_in_scoring is None:
        return None
    out_duty = sum(right - left for left, right in ctx.vocal_out_scoring.windows) / max(
        ctx.audio_end, 0.001
    )
    in_duty = sum(right - left for left, right in ctx.vocal_in_scoring.windows) / float(
        SMART_CROSSFADE_DURATION
    )
    return out_duty, in_duty


def _window_duties(ctx: TransitionContext, seconds: float) -> tuple[float, float] | None:
    """
    Vocal duty per deck over the window an unphrased overlay of ``seconds`` actually spans.

    :param ctx: The transition context.
    :param seconds: Requested overlay length; the outgoing window is the last
        ``seconds`` before the audible end, the incoming window its first ``seconds``.
    """
    if ctx.vocal_out_scoring is None or ctx.vocal_in_scoring is None:
        return None
    # mirrors the anchored tail: a sub-half-second gap to the buffer end is not trimmed
    effective_end = ctx.audio_end
    if effective_end >= ctx.buffer_duration - 0.5:
        effective_end = ctx.buffer_duration
    span = min(seconds, effective_end)
    if span <= 0.0:
        return None
    out_secs = sum(
        max(0.0, min(right, effective_end) - max(left, effective_end - span))
        for left, right in ctx.vocal_out_scoring.windows
    )
    in_secs = sum(
        max(0.0, min(right, span) - max(left, 0.0)) for left, right in ctx.vocal_in_scoring.windows
    )
    return out_secs / span, in_secs / span


def _fade_onset_pin(ctx: TransitionContext) -> float:
    """Buffer-local anchor pin ahead of a detected mastered fade, else the default anchor."""
    if ctx.fade_onset is None:
        return ctx.default_anchor
    # never pin inside A's own vocal window: exiting mid-phrase is the exact
    # defect this pin fixes, so the vocal end floors the pin
    lead_end = ctx.vocal_out_placement.last_end() if ctx.vocal_out_placement else 0.0
    onset_local = max(ctx.fade_onset, lead_end)
    if onset_local < MIN_EFFECTIVE_FADE_BUFFER:
        return ctx.default_anchor
    return min(ctx.default_anchor, onset_local)


def _entry_options(ctx: TransitionContext, bars: int) -> list[float]:
    """B entries for a rung: groove alignment, intro-keeping 0.0 (bars<=2), then natural entry."""
    import numpy as np  # noqa: PLC0415

    bar_b = ctx.incoming.beats_per_bar * 60.0 / ctx.incoming.bpm
    options: list[float] = []
    deep = ctx.natural_entry - bars * bar_b
    if deep > 0.0 and len(ctx.incoming.downbeats):
        downbeats = ctx.incoming.downbeats
        snapped = float(downbeats[np.argmin(np.abs(downbeats - deep))])
        in_mask = ctx.vocal_in_placement is not None and point_in_mask(
            ctx.vocal_in_placement, snapped
        )
        if snapped >= 0.0 and not in_mask:
            options.append(snapped)
    # at 1-2 bars the overlap is a handover, not a blend: keeping B's whole
    # intro (zero trim) lets it build naturally after A's tail rides out; it
    # goes before the natural entry so a scoring tie prefers keeping the intro
    if bars <= 2 and ctx.natural_entry > 0.0 and 0.0 not in options:
        options.append(0.0)
    if ctx.natural_entry not in options:
        options.append(ctx.natural_entry)
    return options


def _outgoing_vocal_end(ctx: TransitionContext) -> float:
    """Return the last outgoing vocal end within the audible tail, or 0.0 without vocal data."""
    mask = ctx.vocal_out_placement
    if mask is None or not mask.windows:
        return 0.0
    return min(mask.last_end(), ctx.audio_end)


def _nearest_protective_anchor(
    ctx: TransitionContext, target: float, *, prefer_earliest: bool = True
) -> float:
    """
    Protective downbeat at/after target within the RMS-audible boundary.

    :param ctx: The transition context.
    :param target: Earliest buffer-local position the anchor may take.
    :param prefer_earliest: Use the first qualifying downbeat (protecting a
        vocal needs only just enough extra room) instead of the last (closing
        an audible-trim gap as tightly as possible).
    """
    candidates = [
        downbeat for downbeat in ctx.protective_downbeats if target <= downbeat <= ctx.audio_end
    ]
    if candidates:
        return candidates[0] if prefer_earliest else candidates[-1]
    return min(target, ctx.audio_end)
