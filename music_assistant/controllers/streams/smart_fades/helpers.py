"""Shared helpers for smart fades."""

from __future__ import annotations

import numpy as np
import numpy.typing as npt

# Buffer size in seconds for crossfade analysis
SMART_CROSSFADE_DURATION = 45

# Below this many seconds of audible tail, a smart crossfade is pointless;
# the caller should fall back to a standard fade (which strips silence).
MIN_EFFECTIVE_FADE_BUFFER = 10.0


def detect_effective_audio_end(
    rms_energy: npt.NDArray[np.float32] | None,
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
    if (
        rms_energy is None
        or not track_duration
        or len(rms_energy) < 2
        or not np.any(np.isfinite(rms_energy))
    ):
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
) -> npt.NDArray[np.float32]:
    """Extrapolate downbeats based on actual intervals when detection is incomplete.

    This is needed when we want to perform beat alignment in an 'atmospheric' outro
    that does not have any detected downbeats.

    :param downbeats: Array of detected downbeat positions in seconds.
    :param buffer_size: Maximum buffer size in seconds.
    :param bpm: Optional BPM for validation when extrapolating with only 2 downbeats.
    """
    # Handle case with exactly 2 downbeats (with BPM validation)
    if len(downbeats) == 2 and bpm is not None:
        interval = float(downbeats[1] - downbeats[0])

        # Expected interval for this BPM (assuming 4/4 time signature)
        expected_interval = (60.0 / bpm) * 4

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
    """Compute S-curve tempo steps aligned to downbeats.

    :param start_ratio: Starting tempo ratio (e.g., 1.0).
    :param end_ratio: Target tempo ratio (e.g., 1.05).
    :param downbeats: Downbeat timestamps to align steps to.
    :param max_step_pct: Maximum tempo change per step as a fraction.
    :return: List of (timestamp_seconds, tempo_ratio) tuples.
    """
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
) -> npt.NDArray[np.float32]:
    """Generate evenly-spaced synthetic timing points for gradual stretch.

    Used when real beat/downbeat detection provides fewer than 2 timestamps
    in the stretch window.

    :param stretch_duration: Duration of the stretch window in seconds.
    :param bpm: BPM of the track (used to approximate bar-level spacing).
    :param n_min: Minimum number of timing points.
    """
    bar_duration = 4.0 * (60.0 / bpm)
    n_points = max(n_min, int(stretch_duration / bar_duration))
    return np.linspace(0, stretch_duration, n_points, dtype=np.float32)
