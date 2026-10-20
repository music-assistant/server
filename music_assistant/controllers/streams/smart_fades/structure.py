"""
Smart Fades - bar-level musical structure detectors over ``BandProfile``.

Detects the energy structure the transition planner reasons about: mastered
fadeouts and valid outro/coda zones.  Every energy feature is self-referenced
against the track's own full-track active-bar medians, so nothing here compares
material across tracks.  The vocal authority is the FireRed ``VocalMask``, which
the coda detector consumes to keep sung material out of a candidate exit zone -
this module never runs its own spectral vocal classifier.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np
    import numpy.typing as npt

    from music_assistant.controllers.streams.smart_fades.models import BandProfile
    from music_assistant.controllers.streams.smart_fades.vocal import VocalMask

# below -10 dB of the peak-normalized scale a band reference is silence
_REFERENCE_EPSILON = 1e-4
# fade stationarity is referenced to the run's first bars: prefix-decidable,
# so the earliest-start scan needs no self-referential run median
_STATIONARITY_PREFIX_BARS = 3
# kick-qualifying bar: low band at half the track's own kick reference
_KICK_RATIO = 0.5


@dataclass(slots=True)
class CodaZone:
    """Media-time extent of a valid outro/coda zone, on downbeat-aligned bar edges."""

    start_s: float
    end_s: float


def mask_intersects(
    mask: VocalMask, start_s: float, end_s: float, bar_s: float, min_fraction: float = 0.25
) -> bool:
    """
    Whether a media-time interval meaningfully overlaps any vocal window.

    :param mask: Vocal-activity mask for the track (windows in the same time origin).
    :param start_s: Interval start in media seconds (inclusive).
    :param end_s: Interval end in media seconds (exclusive).
    :param bar_s: Bar duration in seconds; the overlap unit.
    :param min_fraction: Bar fraction an overlap must cover (epsilon-touch guard).
    """
    required = min_fraction * bar_s
    return any(
        min(end_s, window_end) - max(start_s, window_start) >= required
        for window_start, window_end in mask.windows
    )


def point_in_mask(mask: VocalMask, t_s: float) -> bool:
    """
    Whether a media-time instant falls inside any vocal window.

    :param mask: Vocal-activity mask for the track.
    :param t_s: Instant in media seconds.
    """
    return any(window_start <= t_s < window_end for window_start, window_end in mask.windows)


def detect_mastered_fadeout(
    profile: BandProfile,
    start_s: float,
    end_s: float,
    *,
    min_bars: int = 4,
    drop_db: float = 10.0,
    monotone_share: float = 0.8,
    jitter_db: float = 0.5,
    frac_drift: float = 0.15,
    audible_floor: float = 0.01,
    frac_floor: float = 0.10,
) -> float | None:
    """
    Detect a mastered fadeout over a media-time range.

    A mastering fade is a post-mix gain ramp: total power collapses
    monotonically by ``drop_db`` or more while the band fractions stay frozen.
    Only the run of consecutive audible bars ending at the range's last
    audible bar is considered; the earliest qualifying run start wins.
    Returns the downbeat-snapped fade onset in media seconds, or ``None``
    when the tail holds power, re-orchestrates, or merely decrescendos.

    :param profile: The track's band profile.
    :param start_s: Range start in media seconds (inclusive).
    :param end_s: Range end in media seconds (exclusive).
    :param min_bars: Minimum run length in bars.
    :param drop_db: Total-power drop the run must reach, first to last bar.
    :param monotone_share: Share of run steps that must not rise past the jitter.
    :param jitter_db: Per-step rise tolerated as monotone.
    :param frac_drift: Band-fraction drift allowed from the run-prefix reference.
    :param audible_floor: Total-power ratio under which a bar is inaudible.
    :param frac_floor: Total-power ratio under which fractions are exempt from drift.
    """
    import numpy as np  # noqa: PLC0415

    indices = _range_indices(profile, start_s, end_s)
    if len(indices) == 0:
        return None
    r_total = _total_ratio(profile, indices)
    audible = r_total >= audible_floor
    if not audible.any():
        return None
    run_end = int(np.nonzero(audible)[0][-1])
    run_start = run_end
    while run_start > 0 and audible[run_start - 1]:
        run_start -= 1
    levels_db = np.zeros(len(indices))
    levels_db[audible] = 10.0 * np.log10(r_total[audible])
    fractions = {band: _band_fraction(profile, band, indices) for band in profile.bar_power}
    for k0 in range(run_start, run_end - min_bars + 2):
        steps = np.diff(levels_db[k0 : run_end + 1])
        if float((steps <= jitter_db).mean()) < monotone_share:
            continue
        if levels_db[k0] - levels_db[run_end] < drop_db:
            continue
        if not _fractions_stationary(
            fractions, r_total, k0, run_end, frac_drift=frac_drift, frac_floor=frac_floor
        ):
            continue
        # a spectrally frozen flat bed before the ramp legally joins the run;
        # the onset is where the descent actually starts, so skip flat prefix
        # bars (and land on the ramp's first bar) while the remaining run
        # still qualifies as a fade on its own
        onset = k0
        while (
            onset + min_bars <= run_end
            and levels_db[onset] - levels_db[onset + 1] < jitter_db
            and levels_db[onset + 1] - levels_db[run_end] >= drop_db
        ):
            onset += 1
        if (
            onset > k0
            and onset + min_bars <= run_end
            and levels_db[onset + 1] - levels_db[run_end] >= drop_db
        ):
            onset += 1
        return float(profile.bar_starts[indices[onset]])
    return None


def detect_coda_zone(
    profile: BandProfile,
    vocal_mask: VocalMask,
    earliest_s: float,
    fade_onset_s: float | None,
    start_s: float,
    end_s: float,
    *,
    total_floor: float = 0.15,
    min_seconds: float = 4.0,
    min_bars: int = 2,
    level_hold: float = 0.5,
) -> CodaZone | None:
    """
    Detect a valid outro/coda zone over a media-time range.

    A zone is a run of consecutive bars at audible program level that carry no
    vocal activity, starting at or after ``earliest_s`` (the later of the vocal
    end and the kick end) and ahead of any detected fade onset.  The run must
    be terminal - no vocal window and no kick-qualifying bar after it in the
    range - because a breakdown before returning content is never an exit.
    Validity needs one musical gesture (``min_bars`` bars AND ``min_seconds``
    seconds) and a sustained level: designed outros hold, undetected fades
    halve and fail.  Returns ``None`` when no valid zone exists.

    :param profile: The track's band profile.
    :param vocal_mask: The track's FireRed vocal mask (media time); its windows
        keep sung bars out of the zone and terminate it when a vocal returns.
    :param earliest_s: Earliest allowed bar start in media seconds.
    :param fade_onset_s: Detected mastered-fade onset; zone bars must end at
        or before it (``None`` when no fade was detected).
    :param start_s: Range start in media seconds (inclusive).
    :param end_s: Range end in media seconds (exclusive).
    :param total_floor: Total-power ratio a zone bar must hold (the bed must
        still carry audible program under an equal-power fade).
    :param min_seconds: Minimum zone duration in seconds.
    :param min_bars: Minimum zone length in bars.
    :param level_hold: Minimum second-half/first-half mean total power ratio.
    """
    import numpy as np  # noqa: PLC0415

    indices = _range_indices(profile, start_s, end_s)
    if len(indices) == 0:
        return None
    starts = profile.bar_starts[indices]
    ends = np.array([_bar_end(profile, int(index)) for index in indices])
    bar_lengths = ends - starts
    not_vocal = np.array(
        [
            not mask_intersects(vocal_mask, float(starts[i]), float(ends[i]), float(bar_lengths[i]))
            for i in range(len(indices))
        ]
    )
    qualifying = (
        (starts >= earliest_s) & (_total_ratio(profile, indices) >= total_floor) & not_vocal
    )
    if fade_onset_s is not None:
        qualifying &= ends <= fade_onset_s
    kick = _band_ratio(profile, "low", indices) >= _KICK_RATIO
    zone: CodaZone | None = None
    for first, last in _true_runs(qualifying):
        # terminal: no returning vocal and no kick-qualifying bar after the run
        if (
            mask_intersects(vocal_mask, float(ends[last]), end_s, float(bar_lengths[last]))
            or kick[last + 1 :].any()
        ):
            continue
        if last - first + 1 < min_bars or float(ends[last] - starts[first]) < min_seconds:
            continue
        totals = profile.total_power[indices[first : last + 1]]
        half = (last - first + 1) // 2
        if float(totals[half:].mean()) < level_hold * float(totals[:half].mean()):
            continue
        zone = CodaZone(start_s=float(starts[first]), end_s=float(ends[last]))
    return zone


def _range_indices(profile: BandProfile, start_s: float, end_s: float) -> npt.NDArray[np.intp]:
    """Find the indices of the profile bars whose start lies in [start_s, end_s)."""
    import numpy as np  # noqa: PLC0415

    return np.nonzero((profile.bar_starts >= start_s) & (profile.bar_starts < end_s))[0]


def _total_ratio(profile: BandProfile, indices: npt.NDArray[np.intp]) -> npt.NDArray[np.float64]:
    """r_T per bar: total power over the full-track median active-bar total."""
    import numpy as np  # noqa: PLC0415

    reference = float(np.median(profile.total_power[profile.active]))
    if reference < _REFERENCE_EPSILON:
        return np.zeros(len(indices))
    return profile.total_power[indices] / reference


def _band_ratio(
    profile: BandProfile, band: str, indices: npt.NDArray[np.intp]
) -> npt.NDArray[np.float64]:
    """r_b per bar: band power over the track reference (0 on a silent reference)."""
    import numpy as np  # noqa: PLC0415

    reference = profile.reference[band]
    if reference < _REFERENCE_EPSILON:
        return np.zeros(len(indices))
    return profile.bar_power[band][indices] / reference


def _band_fraction(
    profile: BandProfile, band: str, indices: npt.NDArray[np.intp]
) -> npt.NDArray[np.float64]:
    """f_b per bar: band share of the bar's total power (0 on silent bars)."""
    import numpy as np  # noqa: PLC0415

    total = profile.total_power[indices]
    return np.divide(
        profile.bar_power[band][indices],
        total,
        out=np.zeros(len(indices)),
        where=total > 0,
    )


def _fractions_stationary(
    fractions: dict[str, npt.NDArray[np.float64]],
    r_total: npt.NDArray[np.float64],
    run_start: int,
    run_end: int,
    *,
    frac_drift: float,
    frac_floor: float,
) -> bool:
    """Check that every band's fraction stays within frac_drift of the run-prefix mean."""
    import numpy as np  # noqa: PLC0415

    prefix = slice(run_start, min(run_start + _STATIONARITY_PREFIX_BARS, run_end + 1))
    tested = np.nonzero(r_total[run_start : run_end + 1] >= frac_floor)[0] + run_start
    for values in fractions.values():
        anchor = float(values[prefix].mean())
        if np.any(np.abs(values[tested] - anchor) > frac_drift):
            return False
    return True


def _true_runs(mask: npt.NDArray[np.bool_]) -> list[tuple[int, int]]:
    """Find the (first, last) index pairs of the maximal True runs in a boolean mask."""
    runs: list[tuple[int, int]] = []
    run_start: int | None = None
    for index, value in enumerate(mask):
        if value and run_start is None:
            run_start = index
        elif not value and run_start is not None:
            runs.append((run_start, index - 1))
            run_start = None
    if run_start is not None:
        runs.append((run_start, len(mask) - 1))
    return runs


def _bar_end(profile: BandProfile, bar_index: int) -> float:
    """Media end of a bar: the next downbeat, or +median bar for the last bar."""
    import numpy as np  # noqa: PLC0415

    starts = profile.bar_starts
    if bar_index + 1 < len(starts):
        return float(starts[bar_index + 1])
    return float(starts[-1] + np.median(np.diff(starts)))
