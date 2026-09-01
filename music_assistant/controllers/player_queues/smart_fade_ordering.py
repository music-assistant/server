"""
Smart Fades-aware ordering for Smart Shuffle.

This only reorders tracks MA has already selected. It uses stored tempo, key and end-to-start
energy; missing analysis stays neutral and nothing is analysed just to place a track in the queue.

Dynamic refills use a small local candidate window because new batches keep arriving. Fixed queues
can consider the full movable population within each recency tier. Close choices retain some
randomness.

This does not call the full transition planner to rank candidates. Smart Fades still decides the
actual transition.
"""

from __future__ import annotations

import asyncio
import math
import random
from collections.abc import Callable
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

# Dynamic refills only need a few sensible next choices, not a perfect route through the whole queue.
_DYNAMIC_LOOKAHEAD = 6
_ANALYSIS_BATCH_SIZE = 32
_MOVE_PENALTY = 0.03
_NEAR_TIE_DELTA = 0.08
_EDGE_WINDOW_SECONDS = 15.0
_SILENT_TAIL_CUTOFF = 0.01

# Missing pieces stay at zero. Do not boost the pieces we do know just because
# something else is missing.
_TEMPO_WEIGHT = 0.45
_KEY_WEIGHT = 0.30
_ENERGY_WEIGHT = 0.25


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
            candidate_limit=None,
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
        candidate_limit=_DYNAMIC_LOOKAHEAD,
    )


async def _order_run(
    mass: MusicAssistant,
    items: list[ItemT],
    *,
    get_track: Callable[[ItemT], Track | None],
    preceding_track: Track | None,
    candidate_limit: int | None,
) -> list[ItemT]:
    """Pick good neighbours and keep randomness between close choices."""
    if len(items) <= 1:
        return list(items)

    tracks = [get_track(item) for item in items]
    if any(track is None for track in tracks):
        # The public wrapper splits non-track boundaries before calling this function.
        return list(items)
    typed_tracks = [track for track in tracks if track is not None]
    remaining = list(zip(items, typed_tracks, strict=True))
    analysis_cache: dict[Track, AudioAnalysisData | None] = {}

    async def analysis_for(track: Track) -> AudioAnalysisData | None:
        if track not in analysis_cache:
            analysis_cache[track] = await _stored_analysis(mass, track)
        return analysis_cache[track]

    async def analyses_for(indices: list[int]) -> list[AudioAnalysisData | None]:
        pending: list[Track] = []
        seen: set[Track] = set()
        for index in indices:
            track = remaining[index][1]
            if track not in analysis_cache and track not in seen:
                pending.append(track)
                seen.add(track)
        for start in range(0, len(pending), _ANALYSIS_BATCH_SIZE):
            batch = pending[start : start + _ANALYSIS_BATCH_SIZE]
            loaded = await asyncio.gather(*(_stored_analysis(mass, track) for track in batch))
            analysis_cache.update(zip(batch, loaded, strict=True))
        return [analysis_cache[remaining[index][1]] for index in indices]

    current_track = preceding_track
    current_analysis = await analysis_for(preceding_track) if preceding_track is not None else None
    ordered: list[ItemT] = []

    while remaining:
        window_size = (
            len(remaining) if candidate_limit is None else min(candidate_limit, len(remaining))
        )
        window = list(range(window_size))
        window = _prefer_different_artist(window, remaining, current_track)

        if current_analysis is None:
            picked_index = window[0]
            picked_analysis = await analysis_for(remaining[picked_index][1])
        else:
            candidate_analyses = await analyses_for(window)
            scored = [
                (
                    _pair_score(current_analysis, analysis)
                    - (_MOVE_PENALTY * min(index, _DYNAMIC_LOOKAHEAD - 1)),
                    index,
                )
                for index, analysis in zip(window, candidate_analyses, strict=True)
            ]
            best = max(score for score, _index in scored)
            near_ties = [index for score, index in scored if score >= (best - _NEAR_TIE_DELTA)]
            picked_index = random.choice(near_ties)
            picked_analysis = candidate_analyses[window.index(picked_index)]

        item, current_track = remaining.pop(picked_index)
        ordered.append(item)
        current_analysis = picked_analysis

    return ordered


def _prefer_different_artist(
    indices: list[int],
    remaining: list[tuple[ItemT, Track]],
    current_track: Track | None,
) -> list[int]:
    """Keep the existing same-artist spacing when a local alternative exists."""
    current_artists = _artist_names(current_track)
    if not current_artists:
        return indices
    alternatives = [
        index for index in indices if current_artists.isdisjoint(_artist_names(remaining[index][1]))
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
    outgoing: AudioAnalysisData | None,
    incoming: AudioAnalysisData | None,
) -> float:
    """Score known compatibility; missing information adds zero."""
    if outgoing is None or incoming is None:
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

    if (energy := _energy_score(outgoing, incoming)) is not None:
        weighted += _ENERGY_WEIGHT * energy

    return weighted


def _tempo_score(outgoing_bpm: float | None, incoming_bpm: float | None) -> float | None:
    """Score tempo distance within the range Smart Fades can currently beat match."""
    if not outgoing_bpm or not incoming_bpm or outgoing_bpm <= 0 or incoming_bpm <= 0:
        return None

    ratio = incoming_bpm / outgoing_bpm
    diff_percent = abs(1.0 - ratio) * 100.0
    limit = TIME_STRETCH_BPM_PERCENTAGE_THRESHOLD

    if diff_percent <= limit:
        return 1.0 - (diff_percent / limit)

    # A tempo clash makes the pair less attractive; it does not remove the track.
    return -min(1.0, (diff_percent - limit) / (limit * 2.0))


def _energy_score(
    outgoing: AudioAnalysisData,
    incoming: AudioAnalysisData,
) -> float | None:
    """Compare outgoing-tail energy with incoming-head energy."""
    out_level = _edge_energy(outgoing, from_start=False)
    in_level = _edge_energy(incoming, from_start=True)
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
