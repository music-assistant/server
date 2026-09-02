"""
Smart Fades-aware ordering for Smart Shuffle.

This only reorders tracks MA has already selected. It uses stored tempo, key and end-to-start
energy; missing analysis stays neutral and nothing is analysed just to place a track in the queue.

Both dynamic refills and fixed queues consider every remaining track in the run being ordered;
dynamic mode simply orders one refill batch at a time from the queue tail. Close choices retain
some randomness.

This does not call the full transition planner to rank candidates. Smart Fades still decides the
actual transition.
"""

from __future__ import annotations

import asyncio
import math
import random
from collections.abc import Callable
from dataclasses import dataclass
from statistics import median
from typing import TYPE_CHECKING, TypeVar

from music_assistant.controllers.streams.audio_analysis import SMART_FADES_ANALYSIS_DOMAIN
from music_assistant.controllers.streams.smart_fades.helpers import camelot_affinity
from music_assistant.controllers.streams.smart_fades.planner.context import (
    TIME_STRETCH_BPM_PERCENTAGE_THRESHOLD,
)

if TYPE_CHECKING:
    from music_assistant_models.media_items import Track

    from music_assistant import MusicAssistant
    from music_assistant.models.audio_analysis import AudioAnalysisData

ItemT = TypeVar("ItemT")

_ANALYSIS_BATCH_SIZE = 32
_MOVE_PENALTY = 0.03
# The order-preservation penalty stops growing after a few positions so distant candidates
# stay reachable.
_MOVE_PENALTY_CAP_STEPS = 5
_NEAR_TIE_DELTA = 0.08
_EDGE_WINDOW_SECONDS = 15.0
_SILENT_TAIL_CUTOFF = 0.01

# Missing pieces stay at zero. Do not boost the pieces we do know just because
# something else is missing.
_TEMPO_WEIGHT = 0.45
_KEY_WEIGHT = 0.30
_ENERGY_WEIGHT = 0.25

# The planner has no half/double-time equivalence yet, so octave matches render as quick
# fades; keep them adjacent-friendly but below a beatmatchable pair.
_OCTAVE_TEMPO_FACTOR = 0.6


@dataclass(frozen=True, slots=True)
class _TrackFeatures:
    """Precomputed per-track fields used to score a transition."""

    bpm: float | None
    key: str | None
    mode: str | None
    head_level: float | None
    tail_level: float | None
    known: bool


def _features(analysis: AudioAnalysisData | None) -> _TrackFeatures:
    """Extract the fields ordering needs once, instead of rescanning the analysis per pair."""
    if analysis is None:
        return _TrackFeatures(
            bpm=None, key=None, mode=None, head_level=None, tail_level=None, known=False
        )
    return _TrackFeatures(
        bpm=analysis.bpm,
        key=analysis.key,
        mode=analysis.mode,
        head_level=_edge_energy(analysis, from_start=True),
        tail_level=_edge_energy(analysis, from_start=False),
        known=True,
    )


async def order_queue_items(
    mass: MusicAssistant,
    items: list[ItemT],
    *,
    get_track: Callable[[ItemT], Track | None],
    preceding_track: Track | None = None,
) -> list[ItemT]:
    """Reorder tracks only; a non-track item starts a new run."""
    if len(items) <= 1:
        return list(items)

    result: list[ItemT] = []
    run: list[ItemT] = []
    anchor = preceding_track

    async def flush_run() -> None:
        nonlocal anchor
        if not run:
            return
        ordered = await _order_run(
            mass,
            run,
            get_track=get_track,
            preceding_track=anchor,
        )
        result.extend(ordered)
        anchor = get_track(ordered[-1]) if ordered else None
        run.clear()

    for item in items:
        if get_track(item) is None:
            await flush_run()
            result.append(item)
            # Do not infer a musical transition across a non-track queue item.
            anchor = None
        else:
            run.append(item)

    await flush_run()
    return result


async def order_tracks(
    mass: MusicAssistant,
    tracks: list[Track],
    *,
    preceding_track: Track | None = None,
) -> list[Track]:
    """Reorder an accepted batch without changing which tracks are in it."""
    return await _order_run(
        mass,
        list(tracks),
        get_track=lambda track: track,
        preceding_track=preceding_track,
    )


async def _order_run(
    mass: MusicAssistant,
    items: list[ItemT],
    *,
    get_track: Callable[[ItemT], Track | None],
    preceding_track: Track | None,
) -> list[ItemT]:
    """Pick good neighbours and keep randomness between close choices."""
    if len(items) <= 1:
        return list(items)

    tracks = [get_track(item) for item in items]
    if any(track is None for track in tracks):
        # The public wrapper splits non-track boundaries before calling this function.
        return list(items)
    typed_tracks = [track for track in tracks if track is not None]

    distinct_tracks = list(dict.fromkeys(typed_tracks))
    if preceding_track is not None and preceding_track not in distinct_tracks:
        distinct_tracks.append(preceding_track)

    analysis_by_track: dict[Track, AudioAnalysisData | None] = {}
    for start in range(0, len(distinct_tracks), _ANALYSIS_BATCH_SIZE):
        batch = distinct_tracks[start : start + _ANALYSIS_BATCH_SIZE]
        loaded = await asyncio.gather(*(_stored_analysis(mass, track) for track in batch))
        analysis_by_track.update(zip(batch, loaded, strict=True))

    features = [_features(analysis_by_track[track]) for track in typed_tracks]
    seam_features = (
        _features(analysis_by_track[preceding_track]) if preceding_track is not None else None
    )

    # The pick loop is O(N^2) pure CPU and must not run on the event loop.
    order = await asyncio.to_thread(
        _pick_order, features, typed_tracks, seam_features, preceding_track
    )
    return [items[index] for index in order]


def _pick_order(
    features: list[_TrackFeatures],
    tracks: list[Track],
    seam: _TrackFeatures | None,
    seam_track: Track | None,
) -> list[int]:
    """Greedily pick the next best neighbour, keeping ties random."""
    remaining = list(range(len(features)))
    current = seam
    current_track = seam_track
    order: list[int] = []

    while remaining:
        window = _prefer_different_artist(remaining, tracks, current_track)

        if current is None or not current.known:
            picked = window[0]
        else:
            positions = {index: pos for pos, index in enumerate(remaining)}
            scored = [
                (
                    _pair_score(current, features[index])
                    - (_MOVE_PENALTY * min(positions[index], _MOVE_PENALTY_CAP_STEPS)),
                    index,
                )
                for index in window
            ]
            best = max(score for score, _index in scored)
            near_ties = [index for score, index in scored if score >= (best - _NEAR_TIE_DELTA)]
            picked = random.choice(near_ties)

        remaining.remove(picked)
        order.append(picked)
        current = features[picked]
        current_track = tracks[picked]

    return order


def _prefer_different_artist(
    indices: list[int],
    tracks: list[Track],
    current_track: Track | None,
) -> list[int]:
    """Keep the existing same-artist spacing when a local alternative exists."""
    current_artists = _artist_names(current_track)
    if not current_artists:
        return indices
    alternatives = [
        index for index in indices if current_artists.isdisjoint(_artist_names(tracks[index]))
    ]
    return alternatives or indices


def _artist_names(track: Track | None) -> set[str]:
    """Return normalized artist names for a track."""
    if track is None:
        return set()
    return {artist.name.lower() for artist in track.artists if artist.name}


async def _stored_analysis(
    mass: MusicAssistant,
    track: Track,
) -> AudioAnalysisData | None:
    """Read stored Smart Fades analysis only; never start analysis here."""
    for mapping in sorted(track.provider_mappings, key=lambda item: item.quality, reverse=True):
        provider = mapping.provider_instance or mapping.provider_domain
        analysis = await mass.streams.audio_analysis.get_audio_analysis(
            mapping.item_id,
            provider,
            priority=(SMART_FADES_ANALYSIS_DOMAIN,),
        )
        if analysis is not None:
            return analysis

    # Some provider-created queue items have no mapping. The fallback is still only a lookup;
    # no row means no analysis.
    if track.item_id and track.provider:
        return await mass.streams.audio_analysis.get_audio_analysis(
            track.item_id,
            track.provider,
            priority=(SMART_FADES_ANALYSIS_DOMAIN,),
        )
    return None


def _pair_score(
    outgoing: _TrackFeatures | None,
    incoming: _TrackFeatures | None,
) -> float:
    """Score known compatibility; missing information adds zero."""
    if outgoing is None or incoming is None or not outgoing.known or not incoming.known:
        return 0.0

    weighted = 0.0

    if (tempo := _tempo_score(outgoing.bpm, incoming.bpm)) is not None:
        weighted += _TEMPO_WEIGHT * tempo

    affinity = camelot_affinity(
        outgoing.key,
        outgoing.mode,
        incoming.key,
        incoming.mode,
    )
    if affinity is not None:
        # Normalize affinity onto the existing penalty/point scale.
        weighted += _KEY_WEIGHT * (affinity * 1.5 - 0.5)

    if (energy := _energy_score(outgoing.tail_level, incoming.head_level)) is not None:
        weighted += _ENERGY_WEIGHT * energy

    return weighted


def _tempo_score(outgoing_bpm: float | None, incoming_bpm: float | None) -> float | None:
    """Score tempo distance within the range Smart Fades can currently beat match."""
    if not outgoing_bpm or not incoming_bpm or outgoing_bpm <= 0 or incoming_bpm <= 0:
        return None

    ratio = incoming_bpm / outgoing_bpm
    same_diff = abs(1.0 - ratio) * 100.0
    octave_diff = min(abs(1.0 - ratio * 2.0), abs(1.0 - ratio / 2.0)) * 100.0
    diff_percent = min(same_diff, octave_diff)
    limit = TIME_STRETCH_BPM_PERCENTAGE_THRESHOLD

    if diff_percent <= limit:
        score = 1.0 - (diff_percent / limit)
        # An octave match cannot be beatmatched yet, so it stays below a same-tempo pair.
        return score * _OCTAVE_TEMPO_FACTOR if octave_diff < same_diff else score

    # A tempo clash makes the pair less attractive; it does not remove the track.
    return -min(1.0, (diff_percent - limit) / (limit * 2.0))


def _energy_score(
    out_level: float | None,
    in_level: float | None,
) -> float | None:
    """Compare outgoing-tail energy with incoming-head energy."""
    if out_level is None or in_level is None:
        return None
    if out_level <= _SILENT_TAIL_CUTOFF:
        return None

    # RMS is normalized by the analysis provider. Equal edges score +1, a 0.5 jump is neutral,
    # and the largest possible mismatch approaches -1.
    return max(-1.0, min(1.0, 1.0 - (2.0 * abs(out_level - in_level))))


def _edge_energy(
    analysis: AudioAnalysisData,
    *,
    from_start: bool,
) -> float | None:
    """Return median normalized RMS over roughly 15 seconds at one edge of the track."""
    values = analysis.rms_energy
    if values is None:
        return None

    clean = [
        float(value)
        for value in values
        if isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0.0
    ]
    if len(clean) < 4:
        return None

    if analysis.duration and analysis.duration > 0:
        bins_per_second = len(clean) / analysis.duration
        window_size = round(_EDGE_WINDOW_SECONDS * bins_per_second)
    else:
        window_size = len(clean) // 20
    window_size = max(4, min(len(clean), window_size))

    edge = clean[:window_size] if from_start else clean[-window_size:]
    return median(edge)
