"""Assembly of the listings belonging to one library album: identifiers first, positions second."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, TypeVar

from music_assistant_models.enums import ExternalID

from music_assistant.helpers.external_ids import is_valid_isrc, normalize_external_id

if TYPE_CHECKING:
    from music_assistant_models.media_items import Track

_Key = TypeVar("_Key")


def select_album_tracks(library: list[Track], listings: Sequence[Sequence[Track]]) -> list[Track]:
    """
    Select the provider entries to list next to the library rows of an album.

    A library row always keeps its slot. A provider entry joins an existing slot by
    provider ID, then by ISRC, then by position or, without one, by a title no listing
    has twice; a listing is never collapsed onto itself, and a provider id or ISRC that
    one listing has more than once identifies nothing.

    :param library: The album's library rows.
    :param listings: The album's tracklists, one per provider album it is fetched as.
    """
    entries = _entries(library, listings)
    slots = _Slots()
    # the slot an identifier names is the one of the first entry carrying it, so the
    # entries are taken in a fixed order rather than the order the providers answered in:
    # the placed ones first, so the slots exist by the time the others look for theirs
    for track in sorted(entries.providers, key=lambda track: _matching_order(track, entries)):
        isrcs = entries.usable_isrcs[id(track)]
        if isrcs.intersection(entries.library_isrcs):
            # the library row is this recording's slot, wherever the provider lists it
            continue
        ids = entries.usable_ids[id(track)]
        if entries.library_ids.intersection(ids):
            continue
        source = entries.source_of[id(track)]
        title = entries.title_of[id(track)]
        position = _position(track) if track.track_number else None
        slot = next((slots.by_id[key] for key in ids if key in slots.by_id), None)
        if slot is None:
            slot = next((slots.by_isrc[isrc] for isrc in isrcs if isrc in slots.by_isrc), None)
        if slot is None and position is not None:
            if position in entries.occupied:
                continue
            slot = slots.by_position.get(position)
        elif slot is None and title in entries.unique_titles:
            # a title names an entry only as far as the ISRCs do not say otherwise
            library_isrcs = entries.library_isrcs_by_title.get(title)
            if library_isrcs is not None and not _contradict(isrcs, library_isrcs):
                # the library row is this title's slot
                continue
            slot = slots.by_title.get(title)
            if slot is not None and _contradict(isrcs, slots.isrcs[slot]):
                slot = None
        if slot is not None and source in slots.sources[slot]:
            # a listing is authoritative about itself: two of its entries stay two
            slot = None
        order = entries.order_of[id(track)]
        if slot is None:
            slot = slots.add(track, order)
        elif _preference(track) < _preference(slots.tracks[slot]):
            if position is None:
                # the playable copy takes the slot's position along with the slot
                track.disc_number = slots.tracks[slot].disc_number
                track.track_number = slots.tracks[slot].track_number
            slots.tracks[slot] = track
        slots.index(
            slot,
            source,
            order,
            ids,
            isrcs,
            position,
            title if title in entries.unique_titles else None,
        )
    return slots.listed()


def album_track_backfills(
    library: list[Track], listings: Sequence[Sequence[Track]]
) -> list[tuple[Track, Track]]:
    """
    Find conservative, unambiguous missing library positions to persist.

    :param library: The album's library rows.
    :param listings: The album's tracklists, one per provider album it is fetched as.
    """
    # an identifier a listing carries twice names nothing; one that listings agree on
    # is corroboration, which the positions are held to below
    by_id: dict[tuple[str, str], list[Track] | None] = {}
    by_isrc: dict[str, list[Track] | None] = {}
    by_title: dict[tuple[int, str, str], list[Track] | None] = {}
    for listing in listings:
        seen_ids: set[tuple[str, str]] = set()
        seen_isrcs: set[str] = set()
        seen_titles: set[tuple[int, str, str]] = set()
        for track in listing:
            if track.provider == "library":
                continue
            for key in _ids(track):
                _note(by_id, key, track, seen_ids)
            for isrc in _isrcs(track):
                _note(by_isrc, isrc, track, seen_isrcs)
            _note(by_title, _title(track), track, seen_titles)
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
        if not matches or any(group is None for group in matches):
            continue
        sources = [source for group in matches if group for source in group]
        positions = {_position(source) for source in sources}
        if len(positions) != 1:
            continue
        source = min(sources, key=_preference)
        if not source.track_number or _position(source) in occupied:
            continue
        # every candidate must agree with the row, not only the one chosen: a position
        # another recording claims as well is no repair
        if any(
            _contradict_ids(ids, _ids(candidate)) or _contradict(isrcs, _isrcs(candidate))
            for candidate in sources
        ):
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


@dataclass(frozen=True)
class _Entries:
    """The provider entries of an album, with the library's and their identifiers judged."""

    providers: list[Track]
    source_of: dict[int, int]
    order_of: dict[int, tuple[int, int]]
    usable_ids: dict[int, set[tuple[str, str]]]
    usable_isrcs: dict[int, set[str]]
    title_of: dict[int, tuple[int, str, str]]
    unique_titles: set[tuple[int, str, str]]
    library_ids: set[tuple[str, str]]
    library_isrcs: set[str]
    library_isrcs_by_title: dict[tuple[int, str, str], set[str]]
    occupied: set[tuple[int, int]]


def _entries(library: list[Track], listings: Sequence[Sequence[Track]]) -> _Entries:
    """
    Judge the identifiers of an album's entries ahead of selecting them.

    :param library: The album's library rows.
    :param listings: The album's tracklists, one per provider album it is fetched as.
    """
    # a provider may hand back the library's own rows for the album (a filesystem
    # provider does), which are not entries of their own
    providers = [track for listing in listings for track in listing if track.provider != "library"]
    source_of = _source_of(library, listings)
    usable_isrcs = _listed_once(
        {id(track): _isrcs(track) for track in library + providers}, source_of
    )
    title_of = {id(track): _title(track) for track in library + providers}
    usable_ids = _listed_once({id(track): _ids(track) for track in providers}, source_of)
    library_isrcs_by_title: dict[tuple[int, str, str], set[str]] = defaultdict(set)
    for track in library:
        library_isrcs_by_title[title_of[id(track)]].update(usable_isrcs[id(track)])
    # a provider's own copy of a library row (by id) says which recording the row is: its
    # ISRCs count as the row's, so another provider's copy of that recording knows it too
    library_row_by_id = Counter(key for track in library for key in _ids(track))
    library_title_by_id = {
        key: title_of[id(track)]
        for track in library
        for key in _ids(track)
        if library_row_by_id[key] == 1
    }
    for track in providers:
        for key in usable_ids[id(track)]:
            if (row_title := library_title_by_id.get(key)) is not None:
                library_isrcs_by_title[row_title].update(usable_isrcs[id(track)])
    return _Entries(
        providers=providers,
        source_of=source_of,
        order_of={
            id(track): (source, index)
            for source, listing in enumerate(listings)
            for index, track in enumerate(listing)
        },
        usable_ids=usable_ids,
        usable_isrcs=usable_isrcs,
        title_of=title_of,
        unique_titles=_unique_titles(library + providers, title_of, source_of),
        library_ids=set(library_row_by_id),
        library_isrcs={isrc for isrcs in library_isrcs_by_title.values() for isrc in isrcs},
        library_isrcs_by_title=dict(library_isrcs_by_title),
        occupied={_position(track) for track in library if track.track_number},
    )


@dataclass
class _Slots:
    """The slots filled so far: the entry listed for each, and what names it."""

    tracks: list[Track] = field(default_factory=list)
    sources: list[set[int]] = field(default_factory=list)
    isrcs: list[set[str]] = field(default_factory=list)
    order: list[tuple[int, int]] = field(default_factory=list)
    by_id: dict[tuple[str, str], int] = field(default_factory=dict)
    by_isrc: dict[str, int] = field(default_factory=dict)
    by_position: dict[tuple[int, int], int] = field(default_factory=dict)
    by_title: dict[tuple[int, str, str], int] = field(default_factory=dict)

    def add(self, track: Track, order: tuple[int, int]) -> int:
        """
        Open a slot for an entry and return its index.

        :param track: The entry.
        :param order: The entry's place in the listings, which the slot is listed by.
        """
        self.tracks.append(track)
        self.sources.append(set())
        self.isrcs.append(set())
        self.order.append(order)
        return len(self.tracks) - 1

    def listed(self) -> list[Track]:
        """Return the slots' entries in the order the listings had them."""
        # the matching takes the entries in an order of its own; without positions to
        # sort by, the listing's order is the album's
        return [
            self.tracks[slot]
            for slot in sorted(range(len(self.tracks)), key=self.order.__getitem__)
        ]

    def index(
        self,
        slot: int,
        source: int,
        order: tuple[int, int],
        ids: set[tuple[str, str]],
        isrcs: set[str],
        position: tuple[int, int] | None,
        title: tuple[int, str, str] | None,
    ) -> None:
        """
        Record the listing and identifiers of an entry on the slot it joined.

        :param slot: The slot the entry joined.
        :param source: The listing the entry belongs to.
        :param order: The entry's place in the listings.
        :param ids: The provider ids the entry carries.
        :param isrcs: The usable ISRCs the entry carries.
        :param position: The entry's disc and track position, if it has one.
        :param title: The entry's title, if it is one that names an entry.
        """
        # the slot an identifier names is the one of the first entry carrying it
        self.sources[slot].add(source)
        self.isrcs[slot].update(isrcs)
        self.order[slot] = min(self.order[slot], order)
        for key in ids:
            self.by_id.setdefault(key, slot)
        for isrc in isrcs:
            self.by_isrc.setdefault(isrc, slot)
        if position is not None:
            self.by_position.setdefault(position, slot)
        if title is not None:
            self.by_title.setdefault(title, slot)


def _matching_order(
    track: Track, entries: _Entries
) -> tuple[bool, tuple[bool, str, str], tuple[int, int], tuple[int, str, str], tuple[str, ...]]:
    """
    Return the order an entry is matched in: placed first, preferred next, then by what it carries.

    :param track: The entry.
    :param entries: The album's entries, with their identifiers judged.
    """
    # entries alike down to their provider id must still be told apart by what they
    # carry, never by the order the listings came in
    return (
        not track.track_number,
        _preference(track),
        _position(track),
        entries.title_of[id(track)],
        tuple(sorted(entries.usable_isrcs[id(track)])),
    )


def _contradict(isrcs: set[str], other: set[str]) -> bool:
    """Return whether two entries' usable ISRCs name different recordings."""
    return bool(isrcs) and bool(other) and isrcs.isdisjoint(other)


def _contradict_ids(ids: set[tuple[str, str]], other: set[tuple[str, str]]) -> bool:
    """Return whether two entries carry different ids on one and the same provider."""
    shared_scopes = {scope for scope, _ in ids} & {scope for scope, _ in other}
    return any(
        not {key for key in ids if key[0] == scope}.intersection(other) for scope in shared_scopes
    )


def _note(
    candidates: dict[_Key, list[Track] | None], key: _Key, track: Track, seen: set[_Key]
) -> None:
    """
    Record a listing's entry as a candidate for a key, which a second entry of it voids.

    :param candidates: The entries of the listings so far, by key; None for a voided key.
    :param key: The identifier the entry carries.
    :param track: The entry.
    :param seen: The keys the entry's own listing has carried so far.
    """
    if key in seen:
        candidates[key] = None
        return
    seen.add(key)
    if (known := candidates.get(key, [])) is not None:
        candidates[key] = [*known, track]


def _source_of(library: list[Track], listings: Sequence[Sequence[Track]]) -> dict[int, int]:
    """Return the listing each entry belongs to, by entry id; the library counts as one."""
    source_of = {id(track): -1 for track in library}
    for source, listing in enumerate(listings):
        source_of.update({id(track): source for track in listing})
    return source_of


def _unique_titles(
    tracks: list[Track], title_of: dict[int, tuple[int, str, str]], source_of: dict[int, int]
) -> set[tuple[int, str, str]]:
    """
    Return the titles that name an entry: listed once per listing, at one position at most.

    :param tracks: The entries of every listing, library rows included.
    :param title_of: The title of each entry, by its id.
    :param source_of: The listing of each entry, by its id.
    """
    # a title a listing has twice, or that is placed at two positions, is a repeated
    # movement rather than a copy
    sources: dict[tuple[int, str, str], Counter[int]] = defaultdict(Counter)
    positions: dict[tuple[int, str, str], set[tuple[int, int]]] = defaultdict(set)
    for track in tracks:
        title = title_of[id(track)]
        sources[title][source_of[id(track)]] += 1
        if track.track_number:
            positions[title].add(_position(track))
    return {
        title
        for title, counts in sources.items()
        if max(counts.values()) == 1 and len(positions[title]) <= 1
    }


def _listed_once(
    values_of: dict[int, set[_Key]], source_of: dict[int, int]
) -> dict[int, set[_Key]]:
    """
    Return, per entry, the identifiers of its own that its listing has exactly once.

    :param values_of: The identifiers of each entry, by its id.
    :param source_of: The listing of each entry, by its id.
    """
    seen: dict[int, Counter[_Key]] = defaultdict(Counter)
    for entry, values in values_of.items():
        seen[source_of[entry]].update(values)
    return {
        entry: {value for value in values if seen[source_of[entry]][value] == 1}
        for entry, values in values_of.items()
    }
