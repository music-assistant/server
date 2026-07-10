"""
Smart Fades - band-power signal module.

Turns the analyzer-v2 ``band_rms`` envelopes into bar-level power statistics on
a track's own downbeat grid.  All planner gates read these fractions — power
fractions cancel the per-track peak normalization, so they are the only
cross-track-comparable quantity the stored data supports.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant.controllers.streams.smart_fades.models import BAND_RMS_BANDS, BandProfile

if TYPE_CHECKING:
    import numpy as np
    import numpy.typing as npt

    from music_assistant.models.audio_analysis import AudioAnalysisData

# bars quieter than 1/16th of the track's p95 power carry no usable signal
_ACTIVE_FLOOR_FRACTION = 0.0625


def build_band_profile(analysis: AudioAnalysisData) -> BandProfile | None:
    """
    Build the bar-level band-power profile for one track.

    Returns ``None`` when the stored analysis lacks usable v2 band envelopes or
    a usable downbeat grid, or the track has no active bars (all-silent
    envelopes) — callers must treat that as "bypass this policy".

    :param analysis: Stored analysis row (any version).
    """
    # numpy is imported here to keep it off the server startup path
    import numpy as np  # noqa: PLC0415

    band_rms = (analysis.extra_data or {}).get("band_rms")
    if (
        not band_rms
        or set(band_rms) != set(BAND_RMS_BANDS)
        or any(len(v) != 1800 for v in band_rms.values())
        or analysis.downbeats is None
        or len(analysis.downbeats) < 8
        or not analysis.duration
    ):
        return None
    downbeats = np.asarray(analysis.downbeats, dtype=np.float64)
    median_bar = float(np.median(np.diff(downbeats)))
    edges = np.concatenate([downbeats, [downbeats[-1] + median_bar]])
    bin_duration = analysis.duration / 1800
    idx = np.clip((edges / bin_duration).astype(int), 0, 1800)
    n_bars = len(downbeats)
    bar_power: dict[str, npt.NDArray[np.float64]] = {}
    for name, env in band_rms.items():
        power = np.asarray(env, dtype=np.float64) ** 2
        cum = np.concatenate([[0.0], np.cumsum(power)])
        widths = np.maximum(idx[1:] - idx[:-1], 1)
        bar_power[name] = (cum[idx[1:]] - cum[idx[:-1]]) / widths
    total: npt.NDArray[np.float64] = np.sum(list(bar_power.values()), axis=0)
    # the > 0 term keeps all-silent tracks bar-less (0 >= 0 would mark every bar active)
    active: npt.NDArray[np.bool_] = (
        total >= _ACTIVE_FLOOR_FRACTION * float(np.percentile(total, 95))
    ) & (total > 0.0)
    if not active.any():
        return None
    reference = {name: float(np.median(p[active])) for name, p in bar_power.items()}
    return BandProfile(
        bar_starts=downbeats[:n_bars],
        bar_power=bar_power,
        total_power=total,
        active=active,
        reference=reference,
    )


def window_fraction(profile: BandProfile, band: str, start_s: float, end_s: float) -> float:
    """
    Power fraction of one band over a media-time window (0.0 on an empty window).

    :param profile: The track's band profile.
    :param band: One of the ``BAND_RMS_BANDS`` names.
    :param start_s: Window start in media seconds (inclusive).
    :param end_s: Window end in media seconds (exclusive).
    """
    mask = (profile.bar_starts >= start_s) & (profile.bar_starts < end_s)
    if not mask.any():
        return 0.0
    total = float(profile.total_power[mask].sum())
    return float(profile.bar_power[band][mask].sum()) / total if total > 0 else 0.0


def window_level(profile: BandProfile, band: str, start_s: float, end_s: float) -> float:
    """
    Mean bar power of one band over a media-time window (0.0 on an empty window).

    :param profile: The track's band profile.
    :param band: One of the ``BAND_RMS_BANDS`` names.
    :param start_s: Window start in media seconds (inclusive).
    :param end_s: Window end in media seconds (exclusive).
    """
    mask = (profile.bar_starts >= start_s) & (profile.bar_starts < end_s)
    return float(profile.bar_power[band][mask].mean()) if mask.any() else 0.0


def window_duty(
    profile: BandProfile, band: str, start_s: float, end_s: float, k: float = 0.5
) -> float:
    """
    Fraction of window bars whose band power reaches ``k`` x the track's reference.

    :param profile: The track's band profile.
    :param band: One of the ``BAND_RMS_BANDS`` names.
    :param start_s: Window start in media seconds (inclusive).
    :param end_s: Window end in media seconds (exclusive).
    :param k: Reference multiplier a bar must reach to count.
    """
    mask = (profile.bar_starts >= start_s) & (profile.bar_starts < end_s)
    if not mask.any():
        return 0.0
    threshold = k * profile.reference[band]
    return float((profile.bar_power[band][mask] >= threshold).mean())


def smoothstep(x: float, lo: float, hi: float) -> float:
    """
    Hermite ramp: 0 at/below ``lo``, 1 at/above ``hi``, smooth in between.

    :param x: Input value.
    :param lo: Threshold below which the result is exactly 0.
    :param hi: Value at/above which the result is exactly 1.
    """
    if x <= lo:
        return 0.0
    if x >= hi:
        return 1.0
    t = (x - lo) / (hi - lo)
    return t * t * (3.0 - 2.0 * t)


def loudness_referenced_level(
    profile: BandProfile, band: str, start_s: float, end_s: float
) -> float:
    """
    Window level scaled by the track's own active-bar total power.

    Cancels per-track peak normalization; cross-deck comparable only when
    playback loudness normalization is active for both tracks.

    :param profile: The track's band profile.
    :param band: One of the ``BAND_RMS_BANDS`` names.
    :param start_s: Window start in media seconds (inclusive).
    :param end_s: Window end in media seconds (exclusive).
    """
    reference = float(profile.total_power[profile.active].mean())
    return window_level(profile, band, start_s, end_s) / reference if reference > 0 else 0.0
