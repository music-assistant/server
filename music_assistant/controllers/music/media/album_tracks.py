"""Assembly of the listings belonging to one library album: identifiers first, positions second."""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import TYPE_CHECKING

from music_assistant_models.enums import ExternalID

from music_assistant.helpers.external_ids import is_valid_isrc, normalize_external_id

if TYPE_CHECKING:
    from music_assistant_models.media_items import Track


def select_album_tracks(library: list[Track], providers: list[Track]) -> list[Track]:
    """
    Select the provider entries to list next to the library rows of an album.

    A library row always keeps its slot. A provider entry joins an existing slot by
    provider ID, then by ISRC, then by position or, without one, by a title no source
    lists twice; a source's own listing is never collapsed, and an ISRC that one source
    lists more than once identifies nothing.

    :param library: The album's library rows.
    :param providers: The album's tracklists as the providers list them.
    """
    occupied = {_position(track) for track in library if track.track_number}
    library_ids = {key for track in library for key in _ids(track)}
    usable_isrcs = _unique_isrcs_per_source(library + providers)
    library_isrcs = {isrc for track in library for isrc in usable_isrcs[id(track)]}
    title_of = {id(track): _title(track) for track in library + providers}
    unique_titles = _unique_titles(library + providers, title_of)
    library_titles = {title_of[id(track)] for track in library}
    slots: list[Track] = []
    slot_sources: list[set[str]] = []
    slot_by_isrc: dict[str, int] = {}
    slot_by_position: dict[tuple[int, int], int] = {}
    slot_by_title: dict[tuple[int, str, str], int] = {}
    # the slot an identifier names is the one of the first entry carrying it, so the
    # entries are taken in a fixed order rather than the order the providers answered in:
    # the placed ones first, so the slots exist by the time the others look for theirs
    for track in sorted(providers, key=lambda track: (not track.track_number, _preference(track))):
        if library_ids.intersection(_ids(track)):
            continue
        isrcs = usable_isrcs[id(track)]
        if isrcs.intersection(library_isrcs):
            # the library row is this recording's slot, wherever the provider lists it
            continue
        title = title_of[id(track)]
        position = _position(track) if track.track_number else None
        slot = next((slot_by_isrc[isrc] for isrc in isrcs if isrc in slot_by_isrc), None)
        if slot is None and position is not None:
            if position in occupied:
                continue
            slot = slot_by_position.get(position)
        elif slot is None and title in unique_titles:
            if title in library_titles:
                # the library row is this title's slot
                continue
            slot = slot_by_title.get(title)
        if slot is not None and track.provider in slot_sources[slot]:
            # a source's own listing is authoritative: two of its entries stay two
            slot = None
        if slot is None:
            slot = len(slots)
            slots.append(track)
            slot_sources.append(set())
        elif _preference(track) < _preference(slots[slot]):
            if position is None:
                # the playable copy takes the slot's position along with the slot
                track.disc_number = slots[slot].disc_number
                track.track_number = slots[slot].track_number
            slots[slot] = track
        slot_sources[slot].add(track.provider)
        if position is not None:
            slot_by_position.setdefault(position, slot)
        if title in unique_titles:
            slot_by_title.setdefault(title, slot)
        for isrc in isrcs:
            slot_by_isrc.setdefault(isrc, slot)
    return slots


def album_track_backfills(
    library: list[Track], providers: list[Track]
) -> list[tuple[Track, Track]]:
    """Find conservative, unambiguous missing library positions to persist."""
    by_id: dict[tuple[str, str], Track | None] = {}
    by_isrc: dict[str, Track | None] = {}
    by_title: dict[tuple[int, str, str], Track | None] = {}
    for track in providers:
        for key in _ids(track):
            by_id[key] = None if key in by_id else track
        for isrc in _isrcs(track):
            by_isrc[isrc] = None if isrc in by_isrc else track
        title = _title(track)
        by_title[title] = None if title in by_title else track
    proposals: list[tuple[Track, Track]] = []
    library_ids = Counter(key for track in library for key in _ids(track))
    library_isrcs = Counter(key for track in library for key in _isrcs(track))
    library_titles = Counter(_title(track) for track in library)
    occupied = {_position(track) for track in library if track.track_number}
    for track in library:
        if track.track_number:
            continue
        ids = _ids(track)
        isrcs = _isrcs(track)
        if any(library_ids[key] > 1 for key in ids) or any(library_isrcs[key] > 1 for key in isrcs):
            continue
        matches = [by_id[key] for key in ids if key in by_id]
        if not matches:
            matches = [by_isrc[key] for key in isrcs if key in by_isrc]
        if not matches and library_titles[_title(track)] == 1:
            matches = [by_title.get(_title(track))]
        if not matches or any(source is None for source in matches):
            continue
        sources = [source for source in matches if source is not None]
        positions = {_position(source) for source in sources}
        if len(positions) != 1:
            continue
        source = min(sources, key=_preference)
        if not source.track_number or _position(source) in occupied:
            continue
        source_ids = _ids(source)
        shared_scopes = {scope for scope, _ in ids} & {scope for scope, _ in source_ids}
        if any(
            not {key for key in ids if key[0] == scope}.intersection(source_ids)
            for scope in shared_scopes
        ):
            continue
        source_isrcs = _isrcs(source)
        if isrcs and source_isrcs and not isrcs.intersection(source_isrcs):
            continue
        proposals.append((track, source))
    claims = Counter(_position(source) for _, source in proposals)
    return [(track, source) for track, source in proposals if claims[_position(source)] == 1]


def _ids(track: Track) -> set[tuple[str, str]]:
    ids = {
        (mapping.provider_instance, mapping.item_id)
        for mapping in track.provider_mappings
        if mapping.item_id
    }
    if track.provider != "library" and track.item_id:
        ids.add((track.provider, track.item_id))
    return ids


def _isrcs(track: Track) -> set[str]:
    return {
        normalize_external_id(ExternalID.ISRC, value)
        for kind, value in track.external_ids
        if kind == ExternalID.ISRC and is_valid_isrc(value)
    }


def _position(track: Track) -> tuple[int, int]:
    return track.disc_number or 1, track.track_number


def _title(track: Track) -> tuple[int, str, str]:
    return track.disc_number or 1, track.name.lower(), track.version.lower()


def _preference(track: Track) -> tuple[bool, str, str]:
    return not track.available, track.provider, track.item_id


def _unique_titles(
    tracks: list[Track], title_of: dict[int, tuple[int, str, str]]
) -> set[tuple[int, str, str]]:
    """
    Return the titles that name an entry: listed once per source, at one position at most.

    :param tracks: The entries of every listing, library rows included.
    :param title_of: The title of each entry, by its id.
    """
    # a title a source lists twice, or that is placed at two positions, is a repeated
    # movement rather than a copy
    sources: dict[tuple[int, str, str], Counter[str]] = defaultdict(Counter)
    positions: dict[tuple[int, str, str], set[tuple[int, int]]] = defaultdict(set)
    for track in tracks:
        title = title_of[id(track)]
        sources[title][track.provider] += 1
        if track.track_number:
            positions[title].add(_position(track))
    return {
        title
        for title, counts in sources.items()
        if max(counts.values()) == 1 and len(positions[title]) <= 1
    }


def _unique_isrcs_per_source(tracks: list[Track]) -> dict[int, set[str]]:
    """Return, per track, the ISRCs its own source lists exactly once."""
    isrcs_of = {id(track): _isrcs(track) for track in tracks}
    seen: dict[str, Counter[str]] = defaultdict(Counter)
    for track in tracks:
        seen[track.provider].update(isrcs_of[id(track)])
    return {
        id(track): {isrc for isrc in isrcs_of[id(track)] if seen[track.provider][isrc] == 1}
        for track in tracks
    }
