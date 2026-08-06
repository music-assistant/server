"""
Smart Fades - vocal-activity contract and vocal-collision math.

Parses the optional 1800-bin ``vocal_activity`` list stored in
``AudioAnalysisData.extra_data``, turns it into hysteresis-gated vocal windows,
and scores how much two tracks' vocals would collide inside a candidate
crossfade. Every function here works over plain floats and lists - no NumPy -
so a missing or malformed timeline never pulls it onto a path that would
otherwise stay numpy-free.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from music_assistant.models.audio_analysis import AudioAnalysisData

# The analysis provider aggregates every canonical analysis timeline to this
# fixed number of bins, independent of track duration
VOCAL_ACTIVITY_BINS = 1800

# Hysteresis thresholds: a run opens once probability reaches OPEN and only
# closes once it drops below CLOSE, so a phrase's quieter syllables don't
# fragment its window into several
VOCAL_OPEN_THRESHOLD = 0.5
VOCAL_CLOSE_THRESHOLD = 0.3
# Padding absorbs the detector's attack/release lag around a real phrase
VOCAL_LEFT_PADDING = 0.25
VOCAL_RIGHT_PADDING = 0.75
# A raw run shorter than this is a detector blip (crowd shout, vocal-formant
# one-shot), not a phrase: it never becomes a window
MIN_VOCAL_RUN = 0.3
# Two padded windows closer than this bridge into one; the gap itself is
# clamped to one beat, since a shorter silence is almost certainly a breath
# inside the same phrase rather than a real gap between two of them
MIN_VOCAL_GAP = 0.5
MAX_VOCAL_GAP = 1.0
# Sustained-evidence gate: a run only becomes a window when its peak OR its
# mean shows real confidence; weak short runs are backing 'aahhs'/detector
# noise, and a window here can veto an entire 8-bar blend downstream
VOCAL_MIN_RUN_PEAK = 0.85
VOCAL_MIN_RUN_MEAN = 0.65

# Two-sided vocal-collision guard, in rendered-crossfade seconds. Collisions
# are scored on UNPADDED windows (padding is silence, not vocal), and the
# gain-weighted integral is the binding test: the raw limit only backstops
# pathological cases the weight can't see
COLLISION_SECONDS_LIMIT = 2.0
WEIGHTED_COLLISION_LIMIT = 0.35

# Click-free equal-power fallback bounds, used when no phrased candidate can
# avoid a vocal collision; the floor keeps the handoff reading as intentional
# rather than as a radio edit
MIN_HANDOFF_SECONDS = 0.4
MAX_HANDOFF_SECONDS = 1.0
# crossfades at or under this length must not drop more audible outgoing
# material than the overlap itself covers
SHORT_FADE_SECONDS = 8.0


@dataclass(slots=True)
class VocalMask:
    """Hysteresis-gated vocal-activity windows over one buffer, in buffer-local seconds."""

    windows: list[tuple[float, float]]

    def last_end(self) -> float:
        """End of the last window, or 0.0 when the mask has none."""
        return self.windows[-1][1] if self.windows else 0.0

    def clamped_to(self, upper_bound: float) -> VocalMask:
        """
        Return a copy whose windows never reach past ``upper_bound``.

        Windows starting at or beyond the bound are dropped entirely rather
        than clipped to a zero-length sliver at the edge.

        :param upper_bound: Latest buffer-local second any window may reach.
        """
        clipped = [
            (max(0.0, left), min(upper_bound, right))
            for left, right in self.windows
            if left < upper_bound
        ]
        return VocalMask(windows=clipped)


@dataclass(frozen=True, slots=True)
class VocalHysteresisConfig:
    """
    Tunable hysteresis/padding/gap-bridging thresholds for ``build_vocal_windows``.

    A run also needs a confident peak or mean probability to survive as a window.
    """

    open_threshold: float = VOCAL_OPEN_THRESHOLD
    close_threshold: float = VOCAL_CLOSE_THRESHOLD
    left_padding: float = VOCAL_LEFT_PADDING
    right_padding: float = VOCAL_RIGHT_PADDING
    min_run: float = MIN_VOCAL_RUN
    min_gap: float = MIN_VOCAL_GAP
    max_gap: float = MAX_VOCAL_GAP
    min_run_peak: float = VOCAL_MIN_RUN_PEAK
    min_run_mean: float = VOCAL_MIN_RUN_MEAN


DEFAULT_VOCAL_CONFIG = VocalHysteresisConfig()


@dataclass(frozen=True, slots=True)
class VocalTimeline:
    """A validated vocal-activity timeline: per-bin probabilities and their spacing."""

    probabilities: list[float]
    frame_duration: float


def parse_vocal_probabilities(analysis: AudioAnalysisData) -> VocalTimeline | None:
    """
    Validate and return the stored 1800-bin vocal-activity timeline.

    Returns ``None`` when ``extra_data["vocal_activity"]`` is absent or fails
    any part of the contract: it is not a list of exactly 1800 finite numeric
    probabilities in the inclusive range 0..1, or the analysis has no finite
    positive duration. Callers must treat that as "vocal-aware logic is
    unavailable for this track".

    :param analysis: Stored analysis row to read ``vocal_activity`` from.
    """
    duration = analysis.duration
    if (
        isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or not math.isfinite(duration)
        or duration <= 0.0
    ):
        return None
    vocal_activity = (analysis.extra_data or {}).get("vocal_activity")
    if not isinstance(vocal_activity, list) or len(vocal_activity) != VOCAL_ACTIVITY_BINS:
        return None
    values: list[float] = []
    for raw_value in vocal_activity:
        if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
            return None
        if raw_value < 0.0 or raw_value > 1.0:
            return None
        value = float(raw_value)
        if not math.isfinite(value):
            return None
        values.append(value)
    return VocalTimeline(
        probabilities=values,
        frame_duration=float(duration) / VOCAL_ACTIVITY_BINS,
    )


def build_vocal_windows(
    probabilities: list[float],
    frame_duration: float,
    start_s: float,
    end_s: float,
    *,
    beat_duration: float | None = None,
    config: VocalHysteresisConfig = DEFAULT_VOCAL_CONFIG,
) -> VocalMask:
    """
    Turn a validated vocal-activity timeline into hysteresis-gated windows over a time range.

    A run opens once probability reaches ``config.open_threshold`` and only
    closes once it drops below ``config.close_threshold``, then gets padded
    left/right and bridged across short gaps so one phrase doesn't fragment
    into several windows - and dropped if it never reaches a confident peak
    or mean. Probabilities are indexed from the same time origin as
    ``start_s``/``end_s``; windows outside that range are dropped.

    :param probabilities: Validated per-bin vocal probabilities (0..1).
    :param frame_duration: Seconds spanned by each probability bin.
    :param start_s: Range start (inclusive); windows are clipped to it.
    :param end_s: Range end (exclusive); windows are clipped to it.
    :param beat_duration: The track's beat length in seconds, used to size the
        gap bridge; ``None`` keeps the bridge at its minimum.
    :param config: Hysteresis/padding/gap-bridging thresholds.
    """
    if frame_duration <= 0.0 or not math.isfinite(frame_duration):
        return VocalMask(windows=[])

    runs: list[tuple[int, int]] = []
    run_start: int | None = None
    for index, probability in enumerate(probabilities):
        if probability >= config.open_threshold and run_start is None:
            run_start = index
        elif probability < config.close_threshold and run_start is not None:
            runs.append((run_start, index))
            run_start = None
    if run_start is not None:
        runs.append((run_start, len(probabilities)))
    # a sub-min_run blip would still grow to over a second once padded, enough
    # to fail a candidate on its own — drop it before padding can inflate it
    min_run_frames = max(
        1,
        math.ceil(math.nextafter(config.min_run / frame_duration, -math.inf)),
    )
    timed_runs: list[tuple[float, float]] = []
    for run_start_frame, run_end_frame in runs:
        if run_end_frame - run_start_frame < min_run_frames:
            continue
        run = probabilities[run_start_frame:run_end_frame]
        if max(run) < config.min_run_peak and sum(run) / len(run) < config.min_run_mean:
            continue
        timed_runs.append((run_start_frame * frame_duration, run_end_frame * frame_duration))

    gap = min(config.max_gap, max(config.min_gap, beat_duration or config.min_gap))
    windows: list[tuple[float, float]] = []
    for left, right in timed_runs:
        padded_left = max(start_s, left - config.left_padding)
        padded_right = min(end_s, right + config.right_padding)
        if padded_right <= start_s or padded_left >= end_s:
            continue
        if windows and padded_left - windows[-1][1] <= gap:
            windows[-1] = (windows[-1][0], max(windows[-1][1], padded_right))
        else:
            windows.append((padded_left, padded_right))
    return VocalMask(windows=windows)


def mask_saturated(mask: VocalMask, span: float) -> bool:
    """
    Whether a mask's windows cover >=90% of a span (near-continuous vocal, no fine structure).

    :param mask: Vocal-activity mask to measure.
    :param span: Length in seconds of the range the mask was built over.
    """
    covered = sum(right - left for left, right in mask.windows)
    return covered >= 0.9 * max(0.001, span)


def merge_windows(windows: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """
    Merge overlapping or touching (left, right) intervals into their minimal covering set.

    :param windows: Intervals to merge, in any order.
    """
    merged: list[tuple[float, float]] = []
    for left, right in sorted(windows):
        if merged and left <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], right))
        else:
            merged.append((left, right))
    return merged


def collision_metrics(
    outgoing_windows: list[tuple[float, float]],
    incoming_windows: list[tuple[float, float]],
    duration: float,
) -> tuple[float, float]:
    """
    Simultaneous and gain-weighted vocal overlap, in rendered-crossfade-local seconds.

    The weight models the acrossfade curve's simultaneous audible power at a
    rendered position (``phase = position / duration``): ``4 * phase * (1 -
    phase)``. It is integrated analytically (exact antiderivative, not a
    sampled sum) so the score is deterministic and independent of any step size.

    :param outgoing_windows: Outgoing vocal windows, in rendered crossfade-local seconds.
    :param incoming_windows: Incoming vocal windows, in the same rendered timeline.
    :param duration: Rendered crossfade duration in seconds.
    """
    if duration <= 0.0:
        return 0.0, 0.0
    overlaps: list[tuple[float, float]] = []
    for out_left, out_right in outgoing_windows:
        for in_left, in_right in incoming_windows:
            left = max(0.0, out_left, in_left)
            right = min(duration, out_right, in_right)
            if right > left:
                overlaps.append((left, right))
    merged = merge_windows(overlaps)
    collision_seconds = sum(right - left for left, right in merged)
    weighted_collision = sum(_weighted_overlap(left, right, duration) for left, right in merged)
    return collision_seconds, weighted_collision


def _weighted_overlap(left: float, right: float, duration: float) -> float:
    """Exact integral of the simultaneous-power weight 4p(1-p) over [left, right]."""

    def primitive(t: float) -> float:
        phase = t / duration
        return duration * (2.0 * phase * phase - (4.0 / 3.0) * phase * phase * phase)

    return primitive(right) - primitive(left)
