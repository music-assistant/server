"""Smart Fades - data models.

A ``TransitionPlan`` is the renderer-agnostic description of a transition: it
captures every decision (where to cut, how long to blend, tempo ramp, EQ sweeps)
without owning a single audio byte or FFmpeg filter.  A ``TransitionPlanner``
produces it from stored ``AudioAnalysisData``; a renderer turns it into the
``Filter`` chain.  Keeping the plan free of bytes is what lets alternative
planners be drop-in replacements and lets a plan be computed before any tail
is buffered.
"""

from __future__ import annotations

from dataclasses import dataclass, field


class SmartFadeNotApplicable(Exception):
    """Raised when the tracks cannot yield a smart crossfade and the caller should fall back."""


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
class SweepSpec:
    """Parameters for one ``FrequencySweepFilter`` in the EQ plan."""

    sweep_type: str
    target_freq: int
    duration: float
    start_time: float
    sweep_direction: str
    poles: int
    curve_type: str
    stream_type: str


@dataclass(slots=True)
class EqPlan:
    """Spectral shaping applied across the transition region."""

    crossover_freq: int
    fadeout: SweepSpec
    fadein: SweepSpec


@dataclass(slots=True)
class FadeOutTrim:
    """Where the outgoing track's audible content ends and how much was dropped."""

    end_pos: float
    trimmed_seconds: float


@dataclass(slots=True)
class TransitionPlan:
    """
    Renderer-agnostic description of how two tracks are joined.

    All times are in the outgoing track's buffer-local seconds.
    """

    # Audible end of the fade-out tail (buffer-local seconds).
    fade_out_window: float
    crossfade_duration: float
    eq_plan: EqPlan
    tempo_plan: TempoPlan = field(default_factory=TempoPlan)
    # Drop the silent outro tail before the audible end. None = no trim.
    fadeout_trim: FadeOutTrim | None = None
    # Trim this many seconds off the incoming head for beat alignment. None = no trim.
    fadein_trim_start: float | None = None
