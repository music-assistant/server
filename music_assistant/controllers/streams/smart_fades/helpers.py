"""Shared helpers for smart fades."""

from __future__ import annotations

import numpy as np
import numpy.typing as npt

# Buffer size in seconds for crossfade analysis
SMART_CROSSFADE_DURATION = 45


def get_bpm_diff_percentage(bpm1: float, bpm2: float) -> float:
    """Calculate BPM difference percentage between two BPM values.

    :param bpm1: First BPM value.
    :param bpm2: Second BPM value.
    """
    return abs(1.0 - bpm1 / bpm2) * 100


def extrapolate_downbeats(
    downbeats: npt.NDArray[np.float32],
    tempo_factor: float,
    buffer_size: float = SMART_CROSSFADE_DURATION,
    bpm: float | None = None,
) -> npt.NDArray[np.float32]:
    """Extrapolate downbeats based on actual intervals when detection is incomplete.

    This is needed when we want to perform beat alignment in an 'atmospheric' outro
    that does not have any detected downbeats.

    :param downbeats: Array of detected downbeat positions in seconds.
    :param tempo_factor: Tempo adjustment factor for time stretching.
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
            # Adjust detected downbeats for time stretching first
            adjusted_downbeats = downbeats / tempo_factor
            last_downbeat = adjusted_downbeats[-1]

            # If the last downbeat is close to the buffer end, no extrapolation needed
            if last_downbeat >= buffer_size - 5:
                return adjusted_downbeats

            # Adjust the interval for time stretching
            adjusted_interval = interval / tempo_factor

            # Extrapolate forward from last adjusted downbeat using adjusted interval
            extrapolated = []
            current_pos = last_downbeat + adjusted_interval
            max_extrapolation_distance = 25.0  # Don't extrapolate more than 25s

            while (
                current_pos < buffer_size
                and (current_pos - last_downbeat) <= max_extrapolation_distance
            ):
                extrapolated.append(current_pos)
                current_pos += adjusted_interval

            if extrapolated:
                # Combine adjusted detected downbeats and extrapolated downbeats
                return np.concatenate([adjusted_downbeats, np.array(extrapolated)])

            return adjusted_downbeats
        # else: interval doesn't match BPM, fall through to return original

    if len(downbeats) < 2:
        # Need at least 2 downbeats to extrapolate
        return downbeats / tempo_factor

    # Adjust detected downbeats for time stretching first
    adjusted_downbeats = downbeats / tempo_factor
    last_downbeat = adjusted_downbeats[-1]

    # If the last downbeat is close to the buffer end, no extrapolation needed
    if last_downbeat >= buffer_size - 5:
        return adjusted_downbeats

    # Calculate intervals from ORIGINAL downbeats (before time stretching)
    intervals = np.diff(downbeats)
    median_interval = float(np.median(intervals))
    std_interval = float(np.std(intervals))

    # Only extrapolate if intervals are consistent (low standard deviation)
    if std_interval > 0.2:
        return adjusted_downbeats

    # Adjust the interval for time stretching
    # When slowing down (tempo_factor < 1.0), intervals get longer
    adjusted_interval = median_interval / tempo_factor

    # Extrapolate forward from last adjusted downbeat using adjusted interval
    extrapolated = []
    current_pos = last_downbeat + adjusted_interval
    max_extrapolation_distance = 25.0  # Don't extrapolate more than 25s

    while current_pos < buffer_size and (current_pos - last_downbeat) <= max_extrapolation_distance:
        extrapolated.append(current_pos)
        current_pos += adjusted_interval

    if extrapolated:
        # Combine adjusted detected downbeats and extrapolated downbeats
        return np.concatenate([adjusted_downbeats, np.array(extrapolated)])

    return adjusted_downbeats


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
        timestamp = float(downbeats[i]) if i < len(downbeats) else float(downbeats[-1])
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
