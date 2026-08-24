"""Shared helpers for smart fades."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np
    import numpy.typing as npt

# Buffer size in seconds for crossfade analysis
SMART_CROSSFADE_DURATION = 45

# Below this many seconds of audible tail, a smart crossfade is pointless;
# the caller should fall back to a standard fade (which strips silence).
MIN_EFFECTIVE_FADE_BUFFER = 8.0

# Fraction of sustained (median-active) energy below which the outro no longer
# carries the groove; the crossfade should end at or before this point.
MIX_OUT_ENERGY_FRACTION = 0.70


def detect_effective_audio_end(
    rms_energy: npt.NDArray[np.float32] | list[float] | None,
    track_duration: float | None,
    buffer_duration: float,
) -> float:
    """
    Return the buffer-local time where the outgoing track's audible content ends.

    Returns ``buffer_duration`` when no usable energy data exists or there is no
    trailing silence, and ``0.0`` when the entire tail is silent.

    :param rms_energy: Peak-normalized RMS energy bins spanning the full track.
    :param track_duration: Full track duration in seconds.
    :param buffer_duration: Length in seconds of the fade-out holdback buffer.
    """
    # numpy is imported inside the functions here to keep it off the server startup path
    import numpy as np  # noqa: PLC0415

    if rms_energy is None or not track_duration:
        return buffer_duration
    rms_energy = np.asarray(rms_energy, dtype=np.float32)
    if len(rms_energy) < 2 or not np.any(np.isfinite(rms_energy)):
        return buffer_duration
    bin_duration = track_duration / len(rms_energy)
    start_bin = max(0, int((track_duration - buffer_duration) / bin_duration))
    tail = rms_energy[start_bin:]
    # floor relative to sustained track energy, so hiss/noise tails count as
    # silence but intentionally quiet outros do not
    sustained = rms_energy[rms_energy > 0.01]
    floor = max(0.02, 0.05 * float(np.median(sustained))) if len(sustained) else 0.02
    audible = np.nonzero(tail > floor)[0]
    if len(audible) == 0:
        return 0.0
    return min(
        float((start_bin + audible[-1] + 1) * bin_duration)
        - max(0.0, track_duration - buffer_duration),
        buffer_duration,
    )


def extrapolate_downbeats(
    downbeats: npt.NDArray[np.float32],
    buffer_size: float = SMART_CROSSFADE_DURATION,
    bpm: float | None = None,
    beats_per_bar: int = 4,
) -> npt.NDArray[np.float32]:
    """
    Extrapolate downbeats based on actual intervals when detection is incomplete.

    This is needed when we want to perform beat alignment in an 'atmospheric' outro
    that does not have any detected downbeats.

    :param downbeats: Array of detected downbeat positions in seconds.
    :param buffer_size: Maximum buffer size in seconds.
    :param bpm: Optional BPM for validation when extrapolating with only 2 downbeats.
    :param beats_per_bar: Track meter, used to predict the bar interval from BPM.
    """
    import numpy as np  # noqa: PLC0415

    # Handle case with exactly 2 downbeats (with BPM validation)
    if len(downbeats) == 2 and bpm is not None:
        interval = float(downbeats[1] - downbeats[0])

        # Expected interval for this BPM and meter
        expected_interval = (60.0 / bpm) * beats_per_bar

        # Only extrapolate if interval matches BPM within 15% tolerance
        if abs(interval - expected_interval) / expected_interval < 0.15:
            last_downbeat = float(downbeats[-1])

            # If the last downbeat is close to the buffer end, no extrapolation needed
            if last_downbeat >= buffer_size - 5:
                return downbeats

            # Extrapolate forward from last downbeat
            extrapolated = []
            current_pos = last_downbeat + interval
            max_extrapolation_distance = 25.0  # Don't extrapolate more than 25s

            while (
                current_pos < buffer_size
                and (current_pos - last_downbeat) <= max_extrapolation_distance
            ):
                extrapolated.append(current_pos)
                current_pos += interval

            if extrapolated:
                return np.concatenate([downbeats, np.array(extrapolated, dtype=np.float32)])

            return downbeats
        # else: interval doesn't match BPM, fall through to return original

    if len(downbeats) < 2:
        return downbeats

    last_downbeat = float(downbeats[-1])

    # If the last downbeat is close to the buffer end, no extrapolation needed
    if last_downbeat >= buffer_size - 5:
        return downbeats

    # Calculate intervals between downbeats
    intervals = np.diff(downbeats)
    median_interval = float(np.median(intervals))
    std_interval = float(np.std(intervals))

    # Only extrapolate if intervals are consistent (low standard deviation)
    if std_interval > 0.2:
        return downbeats

    # Extrapolate forward from last downbeat using median interval
    extrapolated = []
    current_pos = last_downbeat + median_interval
    max_extrapolation_distance = 25.0  # Don't extrapolate more than 25s

    while current_pos < buffer_size and (current_pos - last_downbeat) <= max_extrapolation_distance:
        extrapolated.append(current_pos)
        current_pos += median_interval

    if extrapolated:
        return np.concatenate([downbeats, np.array(extrapolated, dtype=np.float32)])

    return downbeats


def compute_gradual_tempo_steps(
    start_ratio: float,
    end_ratio: float,
    downbeats: npt.NDArray[np.float32],
    max_step_pct: float = 0.005,
) -> list[tuple[float, float]]:
    """
    Compute S-curve tempo steps aligned to downbeats.

    :param start_ratio: Starting tempo ratio (e.g., 1.0).
    :param end_ratio: Target tempo ratio (e.g., 1.05).
    :param downbeats: Downbeat timestamps to align steps to.
    :param max_step_pct: Maximum tempo change per step as a fraction.
    :return: List of (timestamp_seconds, tempo_ratio) tuples.
    """
    import numpy as np  # noqa: PLC0415

    total_change = abs(end_ratio - start_ratio)
    if total_change < 1e-6:
        return []

    min_steps = max(1, int(np.ceil(total_change / max_step_pct)))
    n_steps = min(min_steps, len(downbeats))
    if n_steps < 1:
        return [(0.0, end_ratio)]

    # Evenly sample timestamps across the full window when we have more than needed
    if len(downbeats) > n_steps:
        indices = np.round(np.linspace(0, len(downbeats) - 1, n_steps)).astype(int)
        selected_downbeats = downbeats[indices]
    else:
        selected_downbeats = downbeats[:n_steps]

    # S-curve (sigmoid) with steepness adapted to keep max step within budget
    if n_steps == 1:
        sigmoid_values = np.array([1.0])
    else:
        # Binary search for the steepest k where max step <= max_step_pct
        k_lo, k_hi = 0.1, 10.0
        for _ in range(20):
            k_mid = (k_lo + k_hi) / 2.0
            x = np.linspace(-1, 1, n_steps)
            s = 1.0 / (1.0 + np.exp(-k_mid * x))
            s = (s - s[0]) / (s[-1] - s[0])
            deltas = np.diff(s) * total_change
            if float(np.max(deltas)) <= max_step_pct:
                k_lo = k_mid
            else:
                k_hi = k_mid
        k = k_lo
        x = np.linspace(-1, 1, n_steps)
        sigmoid_values = 1.0 / (1.0 + np.exp(-k * x))
        sigmoid_values = (sigmoid_values - sigmoid_values[0]) / (
            sigmoid_values[-1] - sigmoid_values[0]
        )

    steps: list[tuple[float, float]] = []
    for i in range(n_steps):
        timestamp = float(selected_downbeats[i])
        ratio = start_ratio + (end_ratio - start_ratio) * float(sigmoid_values[i])
        steps.append((timestamp, round(ratio, 6)))

    return steps


def generate_synthetic_timestamps(
    stretch_duration: float,
    bpm: float,
    n_min: int = 4,
    beats_per_bar: int = 4,
) -> npt.NDArray[np.float32]:
    """
    Generate evenly-spaced synthetic timing points for gradual stretch.

    Used when real beat/downbeat detection provides fewer than 2 timestamps
    in the stretch window.

    :param stretch_duration: Duration of the stretch window in seconds.
    :param bpm: BPM of the track (used to approximate bar-level spacing).
    :param n_min: Minimum number of timing points.
    :param beats_per_bar: Track meter, used to approximate bar-level spacing.
    """
    import numpy as np  # noqa: PLC0415

    bar_duration = beats_per_bar * (60.0 / bpm)
    n_points = max(n_min, int(stretch_duration / bar_duration))
    return np.linspace(0, stretch_duration, n_points, dtype=np.float32)


def sustained_energy_floor(rms_energy: npt.NDArray[np.float32]) -> float:
    """
    Median energy of the track's active (non-silent) bins.

    :param rms_energy: Peak-normalized RMS energy bins spanning the full track.
    """
    import numpy as np  # noqa: PLC0415

    active = rms_energy[rms_energy > 0.01]
    return float(np.median(active)) if len(active) else 0.0


def detect_mix_out_point(
    rms_energy: npt.NDArray[np.float32] | list[float] | None,
    track_duration: float | None,
    buffer_duration: float,
    bpm: float,
    fraction: float = MIX_OUT_ENERGY_FRACTION,
    beats_per_bar: int = 4,
) -> float:
    """
    Return the buffer-local time where the outro's energy last drops below the floor.

    The floor is ``fraction`` of the track's sustained energy; the curve is
    smoothed over ~1 bar so isolated quiet bins don't move the anchor.  Returns
    ``buffer_duration`` when no usable data exists or there is no decay, and
    ``0.0`` when the entire tail sits below the floor.

    :param rms_energy: Peak-normalized RMS energy bins spanning the full track.
    :param track_duration: Full track duration in seconds.
    :param buffer_duration: Length in seconds of the fade-out holdback buffer.
    :param bpm: Track tempo, used for the one-bar smoothing window.
    :param fraction: Energy floor as a fraction of the sustained level.
    :param beats_per_bar: Track meter, used for the one-bar smoothing window.
    """
    import numpy as np  # noqa: PLC0415

    if rms_energy is None or not track_duration:
        return buffer_duration
    rms_energy = np.asarray(rms_energy, dtype=np.float32)
    if len(rms_energy) < 2 or not np.any(np.isfinite(rms_energy)):
        return buffer_duration
    floor = fraction * sustained_energy_floor(rms_energy)
    if floor <= 0.0:
        return buffer_duration
    bin_duration = track_duration / len(rms_energy)
    bins_per_bar = max(1, round((beats_per_bar * 60.0 / bpm) / bin_duration))
    # a median (not mean) over the bar: isolated quiet bins don't move the anchor,
    # but a real silence cliff stays exactly where it is instead of smearing early
    if bins_per_bar > 1:
        padded = np.pad(rms_energy, bins_per_bar // 2, mode="edge")
        windows = np.lib.stride_tricks.sliding_window_view(padded, bins_per_bar)
        smoothed = np.median(windows, axis=1)[: len(rms_energy)]
    else:
        smoothed = rms_energy
    start_bin = max(0, int((track_duration - buffer_duration) / bin_duration))
    tail = smoothed[start_bin:]
    above = np.nonzero(tail >= floor)[0]
    if len(above) == 0:
        return 0.0
    return min(
        float((start_bin + above[-1] + 1) * bin_duration)
        - max(0.0, track_duration - buffer_duration),
        buffer_duration,
    )


def detect_groove_entry(
    rms_energy: npt.NDArray[np.float32] | list[float] | None,
    track_duration: float | None,
    downbeats: npt.NDArray[np.float32],
    k_sigma: float = 1.5,
) -> float:
    """
    Return the media time where the track's groove enters (first sustained energy step).

    Detects the first bar whose energy steps up by more than ``k_sigma`` standard
    deviations over the preceding bars AND holds for the following 4 bars —
    typically the drums/kick entering after an intro.  Returns ``0.0`` when the
    track opens at full energy or no usable data exists.

    :param rms_energy: Peak-normalized RMS energy bins spanning the full track.
    :param track_duration: Full track duration in seconds.
    :param downbeats: Downbeat grid in media time.
    :param k_sigma: Step threshold in standard deviations of bar-to-bar changes.
    """
    import numpy as np  # noqa: PLC0415

    if rms_energy is None or not track_duration or len(downbeats) < 12:
        return 0.0
    rms_energy = np.asarray(rms_energy, dtype=np.float32)
    t = (np.arange(len(rms_energy)) + 0.5) * (track_duration / len(rms_energy))
    bar_rms = np.array(
        [
            float(rms_energy[(t >= downbeats[i]) & (t < downbeats[i + 1])].mean())
            if ((t >= downbeats[i]) & (t < downbeats[i + 1])).any()
            else 0.0
            for i in range(len(downbeats) - 1)
        ]
    )
    diffs = np.diff(bar_rms)
    sigma = float(np.std(diffs)) or 1e-6
    floor = 0.5 * sustained_energy_floor(rms_energy)
    for i in range(1, len(bar_rms) - 4):
        pre = bar_rms[max(0, i - 4) : i].mean()
        post = bar_rms[i : i + 4].mean()
        if bar_rms[i] - pre > k_sigma * sigma and post > max(pre * 1.5, floor):
            return float(downbeats[i])
    return 0.0


def db_ramp(
    start: float,
    duration: float,
    from_db: float,
    to_db: float,
    step_interval: float = 0.1,
) -> list[tuple[float, float]]:
    """
    Build a linear-in-dB gain schedule for asendcmd-driven filters.

    :param start: Schedule start time in seconds.
    :param duration: Ramp length in seconds.
    :param from_db: Gain at the start of the ramp.
    :param to_db: Gain at the end of the ramp.
    :param step_interval: Seconds between steps (small enough to avoid zipper noise).
    """
    n_steps = max(2, int(duration / step_interval))
    return [
        (start + (i / n_steps) * duration, from_db + (to_db - from_db) * (i / n_steps))
        for i in range(n_steps + 1)
    ]


def keys_compatible(
    key_a: str | None, mode_a: str | None, key_b: str | None, mode_b: str | None
) -> bool:
    """
    Return True when two keys mix harmonically on the Camelot wheel.

    Compatible = same slot, one step along the wheel in the same mode, or
    relative major/minor.  Unknown or missing keys are treated as incompatible
    so key gating only ever shortens a blend (a short fade never sounds wrong).

    :param key_a: Pitch class of the first track's key, e.g. "C", "F#", "Bb".
    :param mode_a: "major" or "minor".
    :param key_b: Pitch class of the second track's key.
    :param mode_b: "major" or "minor".
    """
    a = _camelot_code(key_a, mode_a)
    b = _camelot_code(key_b, mode_b)
    if a is None or b is None:
        return False
    num_a, major_a = a
    num_b, major_b = b
    if major_a == major_b:
        return min((num_a - num_b) % 12, (num_b - num_a) % 12) <= 1
    return num_a == num_b


_FLAT_TO_SHARP = {"Db": "C#", "Eb": "D#", "Gb": "F#", "Ab": "G#", "Bb": "A#"}
# Camelot wheel numbers; relative major/minor share a number (Am=8A, C=8B)
_CAMELOT_MAJOR = {
    "C": 8,
    "G": 9,
    "D": 10,
    "A": 11,
    "E": 12,
    "B": 1,
    "F#": 2,
    "C#": 3,
    "G#": 4,
    "D#": 5,
    "A#": 6,
    "F": 7,
}
_CAMELOT_MINOR = {
    "A": 8,
    "E": 9,
    "B": 10,
    "F#": 11,
    "C#": 12,
    "G#": 1,
    "D#": 2,
    "A#": 3,
    "F": 4,
    "C": 5,
    "G": 6,
    "D": 7,
}


def _camelot_code(key: str | None, mode: str | None) -> tuple[int, bool] | None:
    """Return (wheel number, is_major) or None for unknown input."""
    if not key or mode not in ("major", "minor"):
        return None
    key = _FLAT_TO_SHARP.get(key, key)
    table = _CAMELOT_MAJOR if mode == "major" else _CAMELOT_MINOR
    num = table.get(key)
    return (num, mode == "major") if num else None
