"""
Smart Fades - data models.

A ``TransitionPlan`` is the renderer-agnostic description of a transition: it
captures every decision (where to cut, how long to blend, tempo ramp, shelf EQ)
without owning a single audio byte or FFmpeg filter.  A ``TransitionPlanner``
produces it from stored ``AudioAnalysisData``; a renderer turns it into the
``Filter`` chain.  Keeping the plan free of bytes is what lets alternative
planners be drop-in replacements and lets a plan be computed before any tail
is buffered.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field, replace
from enum import Enum, StrEnum
from typing import TYPE_CHECKING

from music_assistant.controllers.streams.smart_fades.filters import ShelfType
from music_assistant.controllers.streams.smart_fades.helpers import db_ramp

if TYPE_CHECKING:
    import numpy as np
    import numpy.typing as npt

    from music_assistant.models.audio_analysis import AudioAnalysisData


# Band edges (Hz) of the smart_fades ``extra_data["band_rms"]`` envelopes; None = up to Nyquist.
# Stored rows keep whatever bands their analysis_version wrote — read historical rows by shape.
BAND_RMS_BANDS: dict[str, tuple[float, float | None]] = {
    "low": (20.0, 120.0),
    "low_mid": (120.0, 400.0),
    "mid": (400.0, 4000.0),
    "high": (4000.0, None),
}


class SmartFadeNotApplicable(Exception):
    """Raised when the tracks cannot yield a smart crossfade and the caller should fall back."""


class TransitionTier(Enum):
    """How ambitious the transition is, decided from tempo, key and blendability."""

    # stretch range + key compatible: long, fully-featured DJ blend
    FULL_BLEND = "full_blend"
    # stretch range but keys clash: shorter energy-anchored blend
    TEMPO_BLEND = "tempo_blend"
    # tempos incompatible or material not blendable: short downbeat-snapped fade
    QUICK_FADE = "quick_fade"


@dataclass(slots=True)
class Deck:
    """
    One track on the planner's (virtual) DJ deck.

    Holds the track's stored analysis plus the beat grids that are usable for
    this transition (masked/shifted to the relevant buffer window by the planner).
    """

    analysis: AudioAnalysisData
    bpm: float
    beats: npt.NDArray[np.float32]
    downbeats: npt.NDArray[np.float32]
    # time signature numerator; callers fall back to 4 when analysis omits it
    beats_per_bar: int = 4


@dataclass(slots=True)
class BandProfile:
    """Bar-level band-power view of one track, on its own downbeat grid."""

    bar_starts: npt.NDArray[np.float64]
    bar_power: dict[str, npt.NDArray[np.float64]]
    total_power: npt.NDArray[np.float64]
    active: npt.NDArray[np.bool_]
    reference: dict[str, float]


@dataclass(slots=True)
class CrossfadeTimingInfo:
    """Timing breakdown of a crossfade mix output: PRE | CF | POST."""

    pre_crossfade_duration: float = 0.0
    crossfade_duration: float = 0.0
    fadein_trimmed_duration: float = 0.0
    post_crossfade_duration: float = 0.0


@dataclass(slots=True)
class TempoPlan:
    """
    Tempo ramp schedule for the outgoing track.

    ``steps`` is a list of ``(timestamp_seconds, tempo_ratio)`` points in the
    outgoing track's buffer-local time; empty means no time-stretching.
    """

    steps: list[tuple[float, float]] = field(default_factory=list)

    def __bool__(self) -> bool:
        """Return True when the plan actually stretches time."""
        return bool(self.steps)

    def savings_until(self, t: float) -> float:
        """
        Seconds removed from the rendered stream by the stretch up to input time t.

        Negative when the stretch slows the tail down (the rendered stream is
        lengthened).

        :param t: Input-time position (seconds) up to which to integrate savings.
        """
        savings = 0.0
        # rubberband is initialized at the FIRST step's ratio from t=0, so the
        # span before the first step already runs stretched (no-op for multi-step
        # ramps, whose first step has ratio 1.0)
        if self.steps and self.steps[0][0] > 0.0:
            first_ts, first_ratio = self.steps[0]
            span_end = min(first_ts, t)
            savings += span_end * (1.0 - 1.0 / first_ratio)
        for i, (ts, ratio) in enumerate(self.steps):
            if ts >= t:
                break
            seg_end = min(self.steps[i + 1][0] if i + 1 < len(self.steps) else t, t)
            savings += (seg_end - ts) * (1.0 - 1.0 / ratio)
        return savings


@dataclass(slots=True)
class ShelfSchedule:
    """One EQ gain schedule for a ShelfFilter (LOW/HIGH) or PeakFilter (PEAK)."""

    shelf_type: ShelfType
    frequency: int
    # (time_seconds, gain_db); the step at t=0 sets the initial gain
    steps: list[tuple[float, float]]
    # PEAK bandwidth in octaves; unused for LOW/HIGH shelves
    width_oct: float = 0.707

    def gain_at(self, schedule_time: float) -> float:
        """Interpolate the scheduled gain (dB) at a schedule-time position (clamped at the ends)."""
        steps = self.steps
        if schedule_time <= steps[0][0]:
            return steps[0][1]
        if schedule_time >= steps[-1][0]:
            return steps[-1][1]
        for (t0, g0), (t1, g1) in itertools.pairwise(steps):
            if t0 <= schedule_time <= t1:
                frac = (schedule_time - t0) / (t1 - t0) if t1 > t0 else 0.0
                return g0 + frac * (g1 - g0)
        return steps[-1][1]


@dataclass(slots=True)
class EqPlan:
    """Bass-swap EQ across the transition: who owns the low end, and when it swaps."""

    # seconds into the rendered crossfade
    swap_at: float
    # A-side schedules are in input time (pre-stretch), B-side in post-trim time;
    # None means the schedule is bypassed (a shelf shallower than the bypass floor)
    low_out: ShelfSchedule | None
    low_in: ShelfSchedule | None
    high_out: ShelfSchedule | None
    high_in: ShelfSchedule | None
    # measured mid-band (vocal) handover; None on either side means it is bypassed
    mid_out: ShelfSchedule | None = None
    mid_in: ShelfSchedule | None = None

    def with_mid_depth_scaled(self, shrink: float, *, bypass_below_db: float) -> EqPlan:
        """
        Return a copy with both mid schedules' depth scaled by ``shrink`` (fraction to KEEP).

        Bypasses the mid swap entirely when scaling drops its depth below ``bypass_below_db``.
        """
        if self.mid_out is None and self.mid_in is None:
            return self
        source = self.mid_out or self.mid_in
        assert source is not None  # narrowed by the check above
        depth = source.steps[-1 if self.mid_out else 0][1]
        if shrink <= 0.0 or abs(depth * shrink) < abs(bypass_below_db):
            return replace(self, mid_out=None, mid_in=None)
        mid_out = self.mid_out
        mid_in = self.mid_in
        if mid_out is not None:
            mid_out = replace(mid_out, steps=[(t, g * shrink) for t, g in mid_out.steps])
        if mid_in is not None:
            mid_in = replace(mid_in, steps=[(t, g * shrink) for t, g in mid_in.steps])
        return replace(self, mid_out=mid_out, mid_in=mid_in)

    def with_low_ramps_steepened(
        self, swap_at: float, new_len: float, ratio: float, cf_start_input: float
    ) -> EqPlan:
        """
        Return a copy whose low ramps span ``new_len`` centered on ``swap_at``.

        Only the ramp span is tightened; the endpoint gains stay put.
        """
        if self.low_out is None and self.low_in is None:
            return self
        start_in = max(0.0, swap_at - new_len / 2)
        new_len_input = new_len * ratio
        swap_at_input = cf_start_input + swap_at * ratio
        start_out = max(cf_start_input, swap_at_input - new_len_input / 2)

        low_out = self.low_out
        if low_out is not None:
            depth_a = low_out.steps[-1][1]
            low_out = replace(
                low_out, steps=[(0.0, 0.0), *db_ramp(start_out, new_len_input, 0.0, depth_a)]
            )
        low_in = self.low_in
        if low_in is not None:
            depth_b = low_in.steps[0][1]
            low_in = replace(
                low_in, steps=[(0.0, depth_b), *db_ramp(start_in, new_len, depth_b, 0.0)]
            )
        return replace(self, low_out=low_out, low_in=low_in)


@dataclass(slots=True)
class FadeOutTrim:
    """Where the outgoing track's audible content ends and how much was dropped."""

    end_pos: float
    trimmed_seconds: float


class TransitionStrategy(StrEnum):
    """How a vocal-aware plan's final overlap was ultimately decided."""

    # a normal phrased candidate cleared the vocal-collision guard
    ENERGY_ALIGNED = "energy_aligned"
    # every phrased candidate collided; shipped the click-free equal-power fallback
    SHORT_VOCAL_HANDOFF = "short_vocal_handoff"


@dataclass(frozen=True, slots=True)
class PlanMetrics:
    """
    Vocal-aware plan telemetry; every field defaults to its energy-only value.

    Populated only when both tracks carry a validated FireRed vocal-activity
    timeline; an energy-only plan (vocal data absent/invalid) keeps these
    defaults untouched.
    """

    strategy: TransitionStrategy = TransitionStrategy.ENERGY_ALIGNED
    # seconds of RMS-audible outgoing material dropped before the crossfade start
    audible_outgoing_trim: float = 0.0
    # seconds of outgoing vocal activity that fall inside the rendered crossfade
    outgoing_vocal_fade_seconds: float = 0.0
    # whether the final fade_out_window sits on an outgoing downbeat
    anchor_on_downbeat: bool = False
    # simultaneous outgoing/incoming vocal overlap in rendered-crossfade seconds
    collision_seconds: float = 0.0
    # gain-weighted overlap (the acrossfade curve's simultaneous-power integral)
    weighted_collision_seconds: float = 0.0


@dataclass(slots=True)
class TransitionPlan:
    """
    Renderer-agnostic description of how two tracks are joined.

    All times are in the outgoing track's buffer-local seconds.
    """

    # how ambitious the transition is; drives overlap length, tempo and EQ
    tier: TransitionTier
    # audible end of the fade-out tail (buffer-local seconds)
    fade_out_window: float
    crossfade_duration: float
    eq_plan: EqPlan
    tempo_plan: TempoPlan = field(default_factory=TempoPlan)
    fadeout_trim: FadeOutTrim | None = None
    # seconds trimmed off the incoming head for beat alignment
    fadein_trim_start: float | None = None
    metrics: PlanMetrics = field(default_factory=PlanMetrics)
