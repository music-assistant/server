"""
Smart Fades - EQ assembly and the emergency vocal handoff.

The candidate factory builds every candidate with a neutral EQ plan (scoring
never needs it), so the bass/mid/high handover EQ is computed exactly once,
for the winner only, by ``PlanAssembler``. ``EmergencyHandoffFactory`` builds
the click-free equal-power fallback used when every phrased candidate still
collides with the incoming track's vocal.
"""

from __future__ import annotations

from dataclasses import replace
from dataclasses import replace as replace_spec
from typing import TYPE_CHECKING

from music_assistant.controllers.streams.smart_fades.bands import (
    loudness_referenced_level,
    smoothstep,
    window_duty,
    window_fraction,
    window_level,
)
from music_assistant.controllers.streams.smart_fades.filters import ShelfType
from music_assistant.controllers.streams.smart_fades.helpers import db_ramp
from music_assistant.controllers.streams.smart_fades.models import (
    BAND_RMS_BANDS,
    EqPlan,
    ShelfSchedule,
    TempoPlan,
    TransitionPlan,
    TransitionStrategy,
    TransitionTier,
)
from music_assistant.controllers.streams.smart_fades.vocal import (
    COLLISION_SECONDS_LIMIT,
    MAX_HANDOFF_SECONDS,
    MIN_HANDOFF_SECONDS,
    SHORT_FADE_SECONDS,
    WEIGHTED_COLLISION_LIMIT,
)

from .candidates import CandidateSpec, _nearest_protective_anchor

if TYPE_CHECKING:
    import logging

    from music_assistant.controllers.streams.smart_fades.models import BandProfile

    from .candidates import Candidate, CandidateFactory
    from .context import TransitionContext

# Bass-swap EQ: shelf corners/depths as on real club mixers
low_shelf_freq: int = 100
high_shelf_freq: int = 13000
eq_kill_db: float = -26.0
high_ease_db: float = -20.0
# bass handover spans half the overlap; <2 bars reads as an event, >8 bars is masked
bass_swap_fraction: float = 0.5
bass_swap_min_bars: int = 2
bass_swap_max_bars: int = 8
# decision window for the reciprocal swap depth gates, in A/B-bars
bass_swap_window_bars: int = 8

# Reciprocal bass-swap gate: smoothstep each side's low-band fraction (over the
# OTHER deck's window) to scale its own kill depth; dropped below eq_bypass_below_db.
# Corridor sits deliberately below the published dance-master figures (~0.34-0.45
# of power under 120Hz; pop ~0.14 — Elowsson & Friberg 2017, Pestana et al. 2013):
# it reads an 8-bar transition window, not a whole-track LTAS, and putting lo under
# the pop mean lets ordinary pop still earn a partial swap. Final values corpus-tuned.
low_gate_lo: float = 0.10
low_gate_hi: float = 0.25
eq_bypass_below_db: float = -6.0

# Reciprocal high-ease gate, same shape on the high band. Mean-music LTAS is ~0.02 in
# 4-11kHz (Elowsson & Friberg 2017): lo = an average track earns no duck, hi = clearly
# brighter than average earns the full ease. A deck whose own-window high level is below
# high_own_side_floor of its own reference gets no shelf at all.
high_gate_lo: float = 0.02
high_gate_hi: float = 0.06
high_own_side_floor: float = 0.25
# Cymbal-wash mode: when both own-windows read bright and comparably loud a plain
# reciprocal ease would stack their highs, so duck A complementary with B's restore.
wash_duty: float = 0.8
wash_depth_db: float = -26.0
wash_level_tolerance_db: float = 6.0
wash_min_blend_bars: int = 8

# Measured mid-band (vocal) swap: bass-swap gate shape on the mid band, gated also on
# duty so one loud mid bar can't unlock a swap over otherwise instrumental material.
mid_freq: int = 1200
mid_width_oct: float = 2.5
# Corridor sits in the gap between vocal-forward mixes (~0.25-0.45 mid fraction) and
# instrumental dance (~0.10-0.15); absolute placement verified on our unweighted
# pipeline (LTAS: Elowsson & Friberg 2017, Pestana et al. 2013).
mid_gate_lo: float = 0.18
mid_gate_hi: float = 0.30
mid_duty_lo: float = 0.60
mid_duty_hi: float = 0.85
# the mid (vocal) swap is capped shallower on a tempo blend than a full blend
mid_cap_full_db: float = -8.0
mid_cap_tempo_db: float = -6.0
mid_bypass_below_db: float = -1.0

# Dip guard: combined qsin-weighted power of both decks may never sag more than this
# below its plateau across the overlap (outside the intentional bass-handover notch).
max_predicted_dip_db: float = 3.0


class PlanAssembler:
    """Applies the full EQ handover and its dip-guard repair to the selected candidate's plan."""

    def __init__(self, ctx: TransitionContext, logger: logging.Logger) -> None:
        """Initialize the assembler for one transition."""
        self._ctx = ctx
        self._logger = logger

    def finalize(self, candidate: Candidate) -> TransitionPlan:
        """
        Return the candidate's plan with the full EQ schedule and dip guard applied.

        EQ is deliberately absent from every factory-built candidate (scoring
        never needs it); this is where the bass/mid/high handover for the
        selected winner is computed and folded in.
        """
        return replace(candidate.plan, eq_plan=self._choose_eq(candidate.plan))

    def _choose_eq(self, plan: TransitionPlan) -> EqPlan:
        """Plan the low/mid/high EQ handover, centered on the swap point."""
        ctx = self._ctx
        effective_end = plan.fade_out_window
        crossfade_duration = plan.crossfade_duration
        tempo_plan = plan.tempo_plan
        # A-side schedules are in A-input time (rendered before the tempo stretch);
        # B-side schedules are in B's post-trim time, where t=0 is the crossfade start
        bar_in = ctx.incoming.beats_per_bar * 60.0 / ctx.incoming.bpm
        swap_at = self._choose_swap_point(crossfade_duration, plan.fadein_trim_start)
        swap_len = min(
            max(bass_swap_fraction * crossfade_duration, bass_swap_min_bars * bar_in),
            bass_swap_max_bars * bar_in,
            crossfade_duration,
        )
        # pull a late swap point back so the centered window still fits the overlap
        swap_at = min(swap_at, crossfade_duration - swap_len / 2)
        ease = 0.25 * crossfade_duration

        # the ramp completes before the crossfade, so rendered-to-A-input mapping is linear
        ratio = tempo_plan.steps[-1][1] if tempo_plan else 1.0
        cf_start_input = effective_end - crossfade_duration * ratio
        swap_at_input = cf_start_input + swap_at * ratio
        swap_len_input = swap_len * ratio
        start_in = max(0.0, swap_at - swap_len / 2)
        start_out = max(cf_start_input, swap_at_input - swap_len_input / 2)

        depth_a, depth_b = self._choose_swap_depths(effective_end)

        low_out = (
            ShelfSchedule(
                ShelfType.LOW,
                low_shelf_freq,
                [(0.0, 0.0), *db_ramp(start_out, swap_len_input, 0.0, depth_a)],
            )
            if abs(depth_a) >= abs(eq_bypass_below_db)
            else None
        )
        low_in = (
            ShelfSchedule(
                ShelfType.LOW,
                low_shelf_freq,
                [(0.0, depth_b), *db_ramp(start_in, swap_len, depth_b, 0.0)],
            )
            if abs(depth_b) >= abs(eq_bypass_below_db)
            else None
        )
        high_out, high_in = self._choose_high_swap(
            effective_end,
            start_out,
            start_in,
            cf_start_input,
            swap_len_input,
            ease,
            ratio,
            crossfade_duration,
        )

        depth_mid_a, depth_mid_b = self._choose_mid_swap_depths(effective_end, plan.tier)
        mid_out = (
            ShelfSchedule(
                ShelfType.PEAK,
                mid_freq,
                [(0.0, 0.0), *db_ramp(start_out, swap_len_input, 0.0, depth_mid_a)],
                width_oct=mid_width_oct,
            )
            if depth_mid_a is not None
            else None
        )
        mid_in = (
            ShelfSchedule(
                ShelfType.PEAK,
                mid_freq,
                [(0.0, depth_mid_b), *db_ramp(start_in, swap_len, depth_mid_b, 0.0)],
                width_oct=mid_width_oct,
            )
            if depth_mid_b is not None
            else None
        )

        eq_plan = EqPlan(
            swap_at=swap_at,
            low_out=low_out,
            low_in=low_in,
            high_out=high_out,
            high_in=high_in,
            mid_out=mid_out,
            mid_in=mid_in,
        )
        # the low crossover's rendered ramp window; the dip guard exempts it as
        # the intentional bass-handover gesture, but ONLY when a low swap is
        # actually engaged — a bass-light pair has no handover to exempt, so
        # the notch collapses to an interval that never matches a sample time
        notch = (
            (start_in, start_in + swap_len)
            if (low_out is not None or low_in is not None)
            else (-1.0, -1.0)
        )
        return self._apply_dip_guard(
            eq_plan,
            effective_end=effective_end,
            crossfade_duration=crossfade_duration,
            cf_start_input=cf_start_input,
            ratio=ratio,
            swap_at=swap_at,
            bar_in=bar_in,
            notch=notch,
        )

    def _choose_swap_point(
        self, crossfade_duration: float, fadein_trim_start: float | None
    ) -> float:
        """Pick the bass-swap moment: B's groove entry when inside the overlap, else 60% through."""
        import numpy as np  # noqa: PLC0415

        trim = fadein_trim_start or 0.0
        candidate = self._ctx.natural_entry - trim
        if not 0.0 < candidate <= crossfade_duration:
            candidate = 0.6 * crossfade_duration
        # snap to the incoming grid so the new bassline lands on its own 1
        post_trim = self._ctx.incoming.downbeats - trim
        post_trim = post_trim[(post_trim > 0.0) & (post_trim < crossfade_duration)]
        if len(post_trim):
            candidate = float(post_trim[np.argmin(np.abs(post_trim - candidate))])
        return candidate

    def _swap_windows(
        self, effective_end: float, window_bars: int
    ) -> tuple[tuple[float, float], tuple[float, float]]:
        """Reciprocal decision windows for the swap gates: A's outgoing tail, B's incoming head."""
        ctx = self._ctx
        bar_out = ctx.outgoing.beats_per_bar * 60.0 / ctx.outgoing.bpm
        bar_in = ctx.incoming.beats_per_bar * 60.0 / ctx.incoming.bpm
        anchor_media = ctx.buffer_offset + effective_end
        w_a_out = (anchor_media - window_bars * bar_out, anchor_media)
        entry = ctx.natural_entry
        w_b_in = (entry, entry + window_bars * bar_in)
        return w_a_out, w_b_in

    def _choose_swap_depths(self, effective_end: float) -> tuple[float, float]:
        """Reciprocally scale each side's bass-kill depth to the other deck's measured bass."""
        ctx = self._ctx
        # missing band data on either side keeps the shipped full-depth kill (bit-identical)
        if ctx.outgoing_profile is None or ctx.incoming_profile is None:
            return eq_kill_db, eq_kill_db
        w_a_out, w_b_in = self._swap_windows(effective_end, bass_swap_window_bars)
        f_low_b_in = window_fraction(ctx.incoming_profile, "low", *w_b_in)
        f_low_a_out = window_fraction(ctx.outgoing_profile, "low", *w_a_out)
        depth_a = eq_kill_db * smoothstep(f_low_b_in, low_gate_lo, low_gate_hi)
        depth_b = eq_kill_db * smoothstep(f_low_a_out, low_gate_lo, low_gate_hi)
        return depth_a, depth_b

    def _choose_high_swap(
        self,
        effective_end: float,
        start_out: float,
        start_in: float,
        cf_start_input: float,
        swap_len_input: float,
        ease: float,
        ratio: float,
        crossfade_duration: float,
    ) -> tuple[ShelfSchedule | None, ShelfSchedule | None]:
        """Build the high-ease shelves: reciprocal depths, own-side no-op skip, cymbal-wash mode."""
        ctx = self._ctx
        wash = False
        if ctx.outgoing_profile is None or ctx.incoming_profile is None:
            depth_a = depth_b = high_ease_db
        else:
            w_a_out, w_b_in = self._swap_windows(effective_end, bass_swap_window_bars)
            f_high_b_in = window_fraction(ctx.incoming_profile, "high", *w_b_in)
            f_high_a_out = window_fraction(ctx.outgoing_profile, "high", *w_a_out)
            depth_a = high_ease_db * smoothstep(f_high_b_in, high_gate_lo, high_gate_hi)
            depth_b = high_ease_db * smoothstep(f_high_a_out, high_gate_lo, high_gate_hi)

            own_dark_a = window_level(ctx.outgoing_profile, "high", *w_a_out) < (
                high_own_side_floor * ctx.outgoing_profile.reference["high"]
            )
            own_dark_b = window_level(ctx.incoming_profile, "high", *w_b_in) < (
                high_own_side_floor * ctx.incoming_profile.reference["high"]
            )
            if own_dark_a:
                depth_a = 0.0
            if own_dark_b:
                depth_b = 0.0

            wash = self._wash_mode_engages(w_a_out, w_b_in, crossfade_duration)
            if wash:
                depth_a = wash_depth_db

        high_out = (
            ShelfSchedule(
                ShelfType.HIGH,
                high_shelf_freq,
                self._high_out_steps(
                    depth_a,
                    start_out,
                    start_in,
                    cf_start_input,
                    swap_len_input,
                    ease,
                    ratio,
                    wash=wash,
                ),
            )
            if abs(depth_a) >= abs(eq_bypass_below_db)
            else None
        )
        high_in = (
            ShelfSchedule(
                ShelfType.HIGH,
                high_shelf_freq,
                [
                    (0.0, depth_b),
                    *db_ramp(max(0.0, start_in - ease), ease, depth_b, 0.0),
                ],
            )
            if abs(depth_b) >= abs(eq_bypass_below_db)
            else None
        )
        return high_out, high_in

    @staticmethod
    def _high_out_steps(
        depth_a: float,
        start_out: float,
        start_in: float,
        cf_start_input: float,
        swap_len_input: float,
        ease: float,
        ratio: float,
        *,
        wash: bool,
    ) -> list[tuple[float, float]]:
        """Build A's high-duck ramp: shipped post-swap placement, or wash mode's mirrored duck."""
        if wash:
            # mirror B's restore window; it ends at start_in inside the overlap,
            # so the duck can never overrun the crossfade end
            start = cf_start_input + max(0.0, start_in - ease) * ratio
        else:
            start = start_out + swap_len_input
        return [(0.0, 0.0), *db_ramp(start, ease * ratio, 0.0, depth_a)]

    def _wash_mode_engages(
        self,
        w_a_out: tuple[float, float],
        w_b_in: tuple[float, float],
        crossfade_duration: float,
    ) -> bool:
        """Return True when both decks read bright, comparably loud, over a long enough blend."""
        import numpy as np  # noqa: PLC0415

        ctx = self._ctx
        assert ctx.outgoing_profile is not None  # narrowed by the caller
        assert ctx.incoming_profile is not None
        bar_out = ctx.outgoing.beats_per_bar * 60.0 / ctx.outgoing.bpm
        if crossfade_duration < wash_min_blend_bars * bar_out:
            return False
        # absolute brightness floor: duty vs a track's OWN reference measures
        # consistency, not brightness — a dark steady track has duty 1.0
        f_high_a = window_fraction(ctx.outgoing_profile, "high", *w_a_out)
        f_high_b = window_fraction(ctx.incoming_profile, "high", *w_b_in)
        if f_high_a < high_gate_hi or f_high_b < high_gate_hi:
            return False
        duty_a = window_duty(ctx.outgoing_profile, "high", *w_a_out, k=0.5)
        duty_b = window_duty(ctx.incoming_profile, "high", *w_b_in, k=0.5)
        if duty_a < wash_duty or duty_b < wash_duty:
            return False
        level_a = loudness_referenced_level(ctx.outgoing_profile, "high", *w_a_out)
        level_b = loudness_referenced_level(ctx.incoming_profile, "high", *w_b_in)
        if level_a <= 0.0 or level_b <= 0.0:
            return False
        # the planner has no access to playback state, so this presumes loudness-
        # normalized playback; that assumption is strictly more conservative than
        # a duty-only fallback, which would engage wash mode more often
        level_gap_db = abs(10.0 * float(np.log10(level_a / level_b)))
        return level_gap_db <= wash_level_tolerance_db

    def _choose_mid_swap_depths(
        self, effective_end: float, tier: TransitionTier
    ) -> tuple[float | None, float | None]:
        """Gate and scale the measured mid-band (vocal) swap depth; ``None`` means bypass."""
        ctx = self._ctx
        cap = mid_cap_full_db if tier is TransitionTier.FULL_BLEND else mid_cap_tempo_db
        if tier is TransitionTier.QUICK_FADE:
            return None, None
        if ctx.outgoing_profile is None or ctx.incoming_profile is None:
            return None, None
        w_a_out, w_b_in = self._swap_windows(effective_end, bass_swap_window_bars)

        def _score(profile: BandProfile, window: tuple[float, float]) -> float:
            f_mid = window_fraction(profile, "mid", *window)
            duty_mid = window_duty(profile, "mid", *window, k=0.5)
            return smoothstep(f_mid, mid_gate_lo, mid_gate_hi) * smoothstep(
                duty_mid, mid_duty_lo, mid_duty_hi
            )

        score_a = _score(ctx.outgoing_profile, w_a_out)
        score_b = _score(ctx.incoming_profile, w_b_in)
        # the weaker side rules: a swap only reads as a handover when both decks carry a mid element
        depth = cap * min(score_a, score_b)
        if abs(depth) < abs(mid_bypass_below_db):
            return None, None
        return depth, depth

    def _apply_dip_guard(
        self,
        eq_plan: EqPlan,
        *,
        effective_end: float,
        crossfade_duration: float,
        cf_start_input: float,
        ratio: float,
        swap_at: float,
        bar_in: float,
        notch: tuple[float, float],
    ) -> EqPlan:
        """Remediate a predicted outside-notch dip: shrink mid depth, then steepen low ramps."""
        ctx = self._ctx
        if ctx.outgoing_profile is None or ctx.incoming_profile is None:
            return eq_plan
        w_a_out, w_b_in = self._swap_windows(effective_end, bass_swap_window_bars)
        f_a = {
            band: window_fraction(ctx.outgoing_profile, band, *w_a_out) for band in BAND_RMS_BANDS
        }
        f_b = {
            band: window_fraction(ctx.incoming_profile, band, *w_b_in) for band in BAND_RMS_BANDS
        }

        def dip_db(plan: EqPlan) -> float:
            return self._predicted_dip_db(
                plan, crossfade_duration, cf_start_input, ratio, f_a, f_b, notch
            )

        if dip_db(eq_plan) <= max_predicted_dip_db:
            return eq_plan

        # (1) reduce mid depth toward bypass, in steps, until the dip clears
        # or mid is fully bypassed
        shrink_steps = (0.75, 0.5, 0.25, 0.0)
        for shrink in shrink_steps:
            candidate = eq_plan.with_mid_depth_scaled(shrink, bypass_below_db=mid_bypass_below_db)
            if dip_db(candidate) <= max_predicted_dip_db or shrink == 0.0:
                eq_plan = candidate
                break
        if dip_db(eq_plan) <= max_predicted_dip_db:
            return eq_plan

        # (2) steepen the low ramps to the 2-bar floor; endpoints untouched.
        # This is the last remediation step (never shallow the low depth —
        # there is no further knob beyond the floor), so its result is
        # returned whether or not it fully clears the budget.
        if eq_plan.low_out is None and eq_plan.low_in is None:
            # a bass-light pair has no low handover to tighten and no notch to
            # narrow; mid-scaling was the only lever
            return eq_plan
        floor_len = bass_swap_min_bars * bar_in
        return eq_plan.with_low_ramps_steepened(swap_at, floor_len, ratio, cf_start_input)

    def _predicted_dip_db(
        self,
        eq_plan: EqPlan,
        crossfade_duration: float,
        cf_start_input: float,
        ratio: float,
        f_a: dict[str, float],
        f_b: dict[str, float],
        notch: tuple[float, float],
        n_samples: int = 64,
    ) -> float:
        """Max plateau-to-valley drop, in dB, of the predicted combined qsin-weighted power."""
        import numpy as np  # noqa: PLC0415

        # qsin weights match the renderer's acrossfade=...:c1=qsin:c2=qsin curve
        schedules_a = {"low": eq_plan.low_out, "mid": eq_plan.mid_out, "high": eq_plan.high_out}
        schedules_b = {"low": eq_plan.low_in, "mid": eq_plan.mid_in, "high": eq_plan.high_in}
        running_max = 0.0
        max_drop_db = 0.0
        for i in range(n_samples + 1):
            t = crossfade_duration * i / n_samples
            if notch[0] <= t <= notch[1]:
                continue
            w_a = np.cos(np.pi / 2 * t / crossfade_duration) ** 2 if crossfade_duration else 1.0
            w_b = np.sin(np.pi / 2 * t / crossfade_duration) ** 2 if crossfade_duration else 0.0
            p_a = sum(
                f_a[band]
                * 10.0
                ** (_band_gain(schedules_a.get(band), t, cf_start_input, ratio, side="A") / 10.0)
                for band in BAND_RMS_BANDS
            )
            p_b = sum(
                f_b[band]
                * 10.0
                ** (_band_gain(schedules_b.get(band), t, cf_start_input, ratio, side="B") / 10.0)
                for band in BAND_RMS_BANDS
            )
            power = float(w_a * p_a + w_b * p_b)
            running_max = max(running_max, power)
            if running_max > 0.0 and power > 0.0:
                max_drop_db = max(max_drop_db, 10.0 * float(np.log10(running_max / power)))
        return max_drop_db


class EmergencyHandoffFactory:
    """Builds the click-free equal-power SHORT_VOCAL_HANDOFF fallback plan."""

    def __init__(
        self, ctx: TransitionContext, factory: CandidateFactory, logger: logging.Logger
    ) -> None:
        """Initialize the handoff factory for one transition."""
        self._ctx = ctx
        self._factory = factory
        self._logger = logger

    def build(self) -> TransitionPlan:
        """
        Build the never-fail click-free equal-power handoff plan.

        Used only when every phrased candidate still collides: keeps the
        outgoing vocal's protective anchor and shrinks the overlap to the
        auditioned click-free window, favoring the incoming track's own
        vocal onset so the handoff needs no EQ to hide anything.
        """
        ctx = self._ctx
        base_spec = CandidateSpec(
            tier=ctx.tier,
            bars=1,
            anchor_s=None,
            entry_s=ctx.natural_entry,
            strategy=TransitionStrategy.SHORT_VOCAL_HANDOFF,
            source="emergency-handoff",
            ideal_bars=1,
        )
        candidate = self._factory.build(base_spec)
        assert candidate is not None  # the 1-bar rung always yields a candidate
        spec = base_spec

        # old-planner protection semantics: extend the anchor (never past the
        # RMS-audible boundary) far enough to cover any outgoing vocal the
        # handoff would cut short, AND to keep a short fade's audible trim
        # within its own overlap length
        out_mask = ctx.vocal_out_placement
        last_vocal_end = min(out_mask.last_end(), ctx.audio_end) if out_mask is not None else 0.0
        plan0 = candidate.plan
        vocal_would_be_cut = last_vocal_end > plan0.fade_out_window + 1e-9
        overtrims_short_fade = (
            plan0.crossfade_duration <= SHORT_FADE_SECONDS
            and ctx.audio_end - plan0.fade_out_window > plan0.crossfade_duration + 1e-9
        )
        if vocal_would_be_cut or overtrims_short_fade:
            target = max(plan0.fade_out_window, last_vocal_end)
            if overtrims_short_fade:
                target = max(target, ctx.audio_end - plan0.crossfade_duration)
            target = min(target, ctx.audio_end)
            anchor = _nearest_protective_anchor(ctx, target, prefer_earliest=vocal_would_be_cut)
            spec = replace_spec(base_spec, anchor_s=anchor)
            rebuilt = self._factory.build(spec)
            assert rebuilt is not None  # the 1-bar rung always yields a candidate
            candidate = rebuilt

        in_mask = ctx.vocal_in_placement
        incoming_onset = in_mask.windows[0][0] if in_mask is not None and in_mask.windows else 0.0
        duration = max(MIN_HANDOFF_SECONDS, min(MAX_HANDOFF_SECONDS, incoming_onset - 0.1))
        handoff = self._as_handoff(candidate.plan, duration)
        metrics = self._factory.score(spec, handoff)
        if (
            metrics.collision_seconds >= COLLISION_SECONDS_LIMIT
            or metrics.weighted_collision_seconds >= WEIGHTED_COLLISION_LIMIT
        ) and duration > MIN_HANDOFF_SECONDS:
            handoff = self._as_handoff(candidate.plan, MIN_HANDOFF_SECONDS)
            metrics = self._factory.score(spec, handoff)
        return replace(handoff, metrics=metrics)

    @staticmethod
    def _as_handoff(protected: TransitionPlan, duration: float) -> TransitionPlan:
        """Return a copy of ``protected`` shrunk to a tempo/EQ-free equal-power handoff."""
        return replace(
            protected,
            crossfade_duration=duration,
            fadein_trim_start=None,
            tempo_plan=TempoPlan(),
            eq_plan=EqPlan.neutral(swap_at=duration / 2.0),
        )


def _band_gain(
    schedule: ShelfSchedule | None,
    rendered_t: float,
    cf_start_input: float,
    ratio: float,
    *,
    side: str,
) -> float:
    """Gain (dB) of a band schedule at rendered overlap time; ``None`` is 0dB."""
    if schedule is None:
        return 0.0
    # A-side schedules live in pre-stretch input time; B-side already in rendered time
    schedule_time = cf_start_input + rendered_t * ratio if side == "A" else rendered_t
    return schedule.gain_at(schedule_time)
