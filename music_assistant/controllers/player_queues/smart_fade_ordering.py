"""
Smart Fades-aware ordering for Smart Shuffle.

SFREQ-01: Only reorder tracks MA already has. Do not pick extra music here.

SFREQ-06/SFREQ-07: Use stored tempo, key and end-to-start energy. Missing analysis stays neutral,
and nothing is analysed just to find a place in the queue.

SFREQ-09: Keep the search local and keep some randomness when the choices are close.

SFREQ-10: Do not ask the full transition planner to rank queue candidates. This code finds better
neighbours; Smart Fades still decides the actual transition.
"""

from __future__ import annotations

import asyncio
import math
import random
from collections.abc import Callable
from statistics import median
from typing import TYPE_CHECKING, TypeVar

from music_assistant.controllers.streams.audio_analysis import SMART_FADES_ANALYSIS_DOMAIN
from music_assistant.controllers.streams.smart_fades.helpers import keys_compatible
from music_assistant.controllers.streams.smart_fades.planner.context import (
    TIME_STRETCH_BPM_PERCENTAGE_THRESHOLD,
)

if TYPE_CHECKING:
    from music_assistant_models.media_items import Track

    from music_assistant import MusicAssistant
    from music_assistant.models.audio_analysis import AudioAnalysisData

ItemT = TypeVar("ItemT")

# SFREQ-09: Keep this local. We only need a few sensible next choices, not a perfect route
# through the whole queue.
_LOOKAHEAD = 6
_MOVE_PENALTY = 0.03
_NEAR_TIE_DELTA = 0.08
_EDGE_WINDOW_SECONDS = 15.0

# SFREQ-07: Missing pieces stay at zero. Do not boost the pieces we do know just because
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
    """SFREQ-01: Reorder tracks only; a non-track item starts a new run."""
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
    """SFREQ-04: Reorder an accepted batch without changing which tracks are in it."""
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
    """SFREQ-09: Pick good local neighbours and keep randomness between close choices."""
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
        if pending:
            loaded = await asyncio.gather(*(_stored_analysis(mass, track) for track in pending))
            analysis_cache.update(zip(pending, loaded, strict=True))
        return [analysis_cache[remaining[index][1]] for index in indices]

    current_track = preceding_track
    current_analysis = await analysis_for(preceding_track) if preceding_track is not None else None
    ordered: list[ItemT] = []

    while remaining:
        window = list(range(min(_LOOKAHEAD, len(remaining))))
        window = _prefer_different_artist(window, remaining, current_track)

        if current_analysis is None:
            picked_index = window[0]
            picked_analysis = await analysis_for(remaining[picked_index][1])
        else:
            candidate_analyses = await analyses_for(window)
            scored = [
                (
                    _pair_score(current_analysis, analysis) - (_MOVE_PENALTY * index),
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
    """SFREQ-05: Keep the existing same-artist spacing when a local alternative exists."""
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
    """SFREQ-07: Read stored Smart Fades analysis only; never start analysis here."""
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
    """SFREQ-06/SFREQ-07: Score known compatibility; missing information adds zero."""
    if outgoing is None or incoming is None:
        return 0.0

    weighted = 0.0

    if (tempo := _tempo_score(outgoing.bpm, incoming.bpm)) is not None:
        weighted += _TEMPO_WEIGHT * tempo

    if (
        outgoing.key is not None
        and outgoing.mode is not None
        and incoming.key is not None
        and incoming.mode is not None
    ):
        # SFREQ-06: A key clash lowers the score; it does not remove the track.
        key_score = (
            1.0
            if keys_compatible(outgoing.key, outgoing.mode, incoming.key, incoming.mode)
            else -0.35
        )
        weighted += _KEY_WEIGHT * key_score

    if (energy := _energy_score(outgoing, incoming)) is not None:
        weighted += _ENERGY_WEIGHT * energy

    return weighted


def _tempo_score(outgoing_bpm: float | None, incoming_bpm: float | None) -> float | None:
    """SFREQ-06: Score tempo distance, including half/double-time matches."""
    if not outgoing_bpm or not incoming_bpm or outgoing_bpm <= 0 or incoming_bpm <= 0:
        return None

    ratio = incoming_bpm / outgoing_bpm
    diff_percent = (
        min(
            abs(1.0 - ratio),
            abs(1.0 - ratio * 2.0),
            abs(1.0 - ratio / 2.0),
        )
        * 100.0
    )
    limit = TIME_STRETCH_BPM_PERCENTAGE_THRESHOLD

    if diff_percent <= limit:
        return 1.0 - (diff_percent / limit)

    # SFREQ-06: A tempo clash makes the pair less attractive; it does not remove the track.
    return -min(1.0, (diff_percent - limit) / (limit * 2.0))


def _energy_score(
    outgoing: AudioAnalysisData,
    incoming: AudioAnalysisData,
) -> float | None:
    """SFREQ-06: Compare outgoing-tail energy with incoming-head energy."""
    out_level = _edge_energy(outgoing, from_start=False)
    in_level = _edge_energy(incoming, from_start=True)
    if out_level is None or in_level is None:
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
