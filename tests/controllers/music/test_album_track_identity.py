"""Regression coverage for identifier-first album listings and safe backfill."""

from collections import defaultdict
from dataclasses import replace
from itertools import permutations
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
from music_assistant_models.enums import ExternalID
from music_assistant_models.media_items import ProviderMapping

from music_assistant.controllers.music.media import album_tracks

from .helpers import create_album, create_track

if TYPE_CHECKING:
    from music_assistant_models.media_items import Track

    from music_assistant.mass import MusicAssistant


def entry(provider: str, item_id: str, position: int = 1, isrc: str | None = None) -> Track:
    """Build a listing entry without the fixture's default recording ID."""
    track = create_track(provider, item_id, name="Allegro")
    track.external_ids = {(ExternalID.ISRC, isrc)} if isrc else set()
    track.track_number = position
    return track


def unplayable(track: Track) -> Track:
    """Mark a listing entry as one its provider can not play."""
    track.provider_mappings = {
        replace(mapping, available=False) for mapping in track.provider_mappings
    }
    return track


def listings(tracks: list[Track]) -> list[list[Track]]:
    """Group entries into one listing per provider, as the controller fetches them."""
    by_provider: dict[str, list[Track]] = defaultdict(list)
    for track in tracks:
        by_provider[track.provider].append(track)
    return list(by_provider.values())


def select(library: list[Track], tracks: list[Track]) -> list[Track]:
    """Select from entries given flat, one listing per provider."""
    return album_tracks.select_album_tracks(library, listings(tracks))


def backfills(library: list[Track], tracks: list[Track]) -> list[tuple[Track, Track]]:
    """Find backfills from entries given flat, one listing per provider."""
    return album_tracks.album_track_backfills(library, listings(tracks))


def test_two_listings_of_one_provider_are_two_sources() -> None:
    """Two provider albums one instance is matched as are collapsed like any two listings."""
    first = [entry("a", "one", 1, "GBAYC2100001"), entry("a", "two", 2, "GBAYC2100002")]
    second = [entry("a", "three", 1, "GBAYC2100001"), entry("a", "four", 2, "GBAYC2100002")]
    selected = album_tracks.select_album_tracks([], [first, second])
    assert sorted(track.track_number for track in selected) == [1, 2]
    # a track both albums list under one provider id is one entry as well
    twice = album_tracks.select_album_tracks([], [[entry("a", "one", 1)], [entry("a", "one", 1)]])
    assert [track.item_id for track in twice] == ["one"]


def test_a_listing_of_the_librarys_own_rows_adds_nothing() -> None:
    """A provider handing back the library rows (a filesystem does) leaves them the slots."""
    row = entry("library", "42", 3, "GBAYC2100001")
    row.provider_mappings = entry("fs", "path").provider_mappings
    handed_back = entry("library", "42", 3, "GBAYC2100001")
    handed_back.provider_mappings = set(row.provider_mappings)
    remote = entry("b", "two", 0, "GBAYC2100001")
    assert album_tracks.select_album_tracks([row], [[handed_back], [remote]]) == []
    assert album_tracks.album_track_backfills([row], [[handed_back], [remote]]) == []


@pytest.mark.parametrize(("disc", "count"), [(1, 1), (2, 2)])
def test_unknown_disc(disc: int, count: int) -> None:
    """Disc zero means disc one, never an arbitrary multidisc wildcard."""
    left, right = entry("a", "one"), entry("b", "two")
    left.disc_number, right.disc_number = 0, disc
    assert len(select([], [left, right])) == count


@pytest.mark.parametrize("position", [0, 1, 2])
def test_repeated_unknown_titles_are_preserved(position: int) -> None:
    """Distinct IDs in one source must never disappear through title fallback."""
    tracks = [entry("a", "one", 0), entry("a", "two", 0), entry("b", "three", position)]
    assert len(select([], tracks)) == 3


@pytest.mark.parametrize("position", [0, 1])
def test_unambiguous_cross_provider_title(position: int) -> None:
    """A single missing entry can match a single entry from another source."""
    tracks = [entry("a", "one", 0), entry("b", "two", position)]
    assert len(select([], tracks)) == 1


def test_playable_unplaced_copy_takes_an_unplayable_placed_slot() -> None:
    """Without identifiers, a playable copy replaces an unplayable placed one, at its position."""
    placed = entry("a", "one", 3)
    placed.provider_mappings = {
        ProviderMapping(item_id="one", provider_domain="a", provider_instance="a", available=False)
    }
    copy = entry("b", "two", 0)
    selected = select([], [placed, copy])
    assert [track.item_id for track in selected] == ["two"]
    assert (selected[0].disc_number, selected[0].track_number) == (placed.disc_number, 3)


def test_playable_unplaced_isrc_copy_takes_an_unplayable_placed_slot() -> None:
    """A playable copy of a placed recording replaces it by ISRC, whatever its title."""
    placed = entry("a", "one", 3, "GBAYC2100001")
    placed.provider_mappings = {
        ProviderMapping(item_id="one", provider_domain="a", provider_instance="a", available=False)
    }
    copy = entry("b", "two", 0, "GBAYC2100001")
    copy.name = "Allegro (Remastered)"
    selected = select([], [placed, copy])
    assert [track.item_id for track in selected] == ["two"]
    assert selected[0].track_number == 3


def test_unplaced_entries_sharing_an_isrc_are_one_entry() -> None:
    """Two sources' entries without a position but with one ISRC are one recording."""
    first = entry("a", "one", 0, "GBAYC2100001")
    second = entry("b", "two", 0, "GBAYC2100001")
    second.name = "Allegro (Remastered)"
    selected = select([], [first, second])
    assert [track.item_id for track in selected] == ["one"]
    # one source listing the ISRC twice identifies nothing: both of its entries stay
    same_source = entry("a", "two", 0, "GBAYC2100001")
    same_source.name = "Allegro (Remastered)"
    assert len(select([], [first, same_source])) == 2


def test_a_copy_joining_by_title_keeps_its_sources_entries_apart() -> None:
    """A copy whose title names a slot its own source already fills stays an entry of its own."""
    symphony = unplayable(entry("a", "a1", 1, "GBAYC2100001"))
    symphony.name = "Allegro (Symphony 1)"
    allegro = entry("a", "a2", 0, "GBAYC2100002")
    other = entry("b", "b1", 1, "GBAYC2100001")
    selected = select([], [symphony, allegro, other])
    assert [track.item_id for track in selected] == ["b1", "a2"]


def test_a_copy_that_took_a_slot_by_title_names_its_recording() -> None:
    """A copy that took a slot by title holds the slot for its ISRC as well."""
    placed = unplayable(entry("a", "one", 1))
    allegro = entry("b", "two", 0, "GBAYC2100001")
    remaster = entry("c", "three", 0, "GBAYC2100001")
    remaster.name = "Allegro (Remastered)"
    selected = select([], [placed, allegro, remaster])
    assert [track.item_id for track in selected] == ["two"]
    assert selected[0].track_number == 1


@pytest.mark.parametrize("order", list(permutations(range(3))))
def test_a_playable_copy_takes_a_slot_two_unplayable_copies_fill(order: tuple[int, ...]) -> None:
    """Two unplayable placed copies and a playable one without a position are one entry."""
    copies = [
        unplayable(entry("a", "one", 1)),
        unplayable(entry("b", "two", 1)),
        entry("c", "three", 0),
    ]
    selected = select([], [copies[index] for index in order])
    assert [track.item_id for track in selected] == ["three"]
    assert selected[0].track_number == 1


def test_unknown_title_does_not_choose_repeated_position() -> None:
    """One unknown movement cannot be assigned to either of two positions."""
    tracks = [entry("a", "one", 0), entry("b", "two", 1), entry("c", "three", 2)]
    assert len(select([], tracks)) == 3


@pytest.mark.parametrize("order", list(permutations(range(3))))
def test_sources_at_one_position_collapse_in_any_order(order: tuple[int, ...]) -> None:
    """Entries of different sources at one position share a slot, whatever order they come in."""
    editions = [
        entry("a", "one", 1, "GBAYC2100001"),
        entry("b", "two", 1, "GBAYC2100002"),
        entry("c", "three", 1, "GBAYC2100003"),
    ]
    tracks = [editions[index] for index in order]
    selected = select([], tracks)
    assert [track.item_id for track in selected] == ["one"]


def test_repeated_isrc_within_a_listing_identifies_nothing() -> None:
    """A source reusing one ISRC keeps both entries; another source then matches by position."""
    reused = [entry("a", "one", 1, "GBAYC2100001"), entry("a", "five", 5, "GBAYC2100001")]
    assert len(select([], reused)) == 2
    other = entry("b", "two", 1, "GBAYC2100001")
    assert len(select([], [*reused, other])) == 2


@pytest.mark.parametrize("position", [1, 9])
def test_shared_isrc_matches_across_positions(position: int) -> None:
    """Another source's entry with the same ISRC is the same recording wherever it lists it."""
    tracks = [entry("a", "one", 2, "GBAYC2100001"), entry("b", "two", position, "GBAYC2100001")]
    assert len(select([], tracks)) == 1


def test_positionless_copy_of_a_placed_recording_is_not_listed_by_title() -> None:
    """An entry without a position whose ISRC a slot already holds does not fall back to title."""
    placed = entry("a", "one", 3, "GBAYC2100001")
    copy = entry("b", "two", 0, "GBAYC2100001")
    copy.name = "Allegro (Remastered)"
    assert select([], [placed, copy]) == [placed]


def test_library_recording_suppresses_its_provider_copy_by_isrc() -> None:
    """A provider copy of a recording the library holds is not listed again, at any position."""
    library = entry("library", "42", 0, "GBAYC2100001")
    library.provider_mappings = set()
    source = entry("a", "new", 7, "GBAYC2100001")
    assert select([library], [source]) == []


def test_library_slot_preferred_despite_identifier_drift() -> None:
    """Position can suppress a listing copy without authorizing a database repair."""
    library = entry("library", "42", 1, "GBAYC2100001")
    source = entry("a", "new", 1, "GBAYC2100002")
    library.provider_mappings = entry("a", "old").provider_mappings
    source.name = "Different title"
    assert select([library], [source]) == []
    assert backfills([library], [source]) == []


@pytest.mark.parametrize("count", [1000, 3000])
def test_repeated_titles_have_linear_index_work(count: int) -> None:
    """Large classical listings index each entry a bounded number of times."""
    tracks = [entry("a", str(index), index + 1) for index in range(count)]
    with patch.object(album_tracks, "_title", wraps=album_tracks._title) as title:
        assert len(select([], tracks)) == count
        assert backfills([], tracks) == []
    assert title.call_count == count * 2


async def test_duplicate_library_rows_do_not_append_exact_provider_copy(
    mass: MusicAssistant,
) -> None:
    """Keep existing library rows without adding or repairing their exact provider copy."""
    album = create_album("qobuz_1", "album")
    source = entry("qobuz_1", "current", 3)
    libraries = [entry("library", str(index), 0) for index in (41, 42)]
    for track in libraries:
        track.provider_mappings = source.provider_mappings
    with (
        patch.object(
            mass.music.albums, "get_library_item_by_prov_id", AsyncMock(return_value=album)
        ),
        patch.object(
            mass.music.albums, "get_library_album_tracks", AsyncMock(return_value=libraries)
        ),
        patch.object(
            mass.music.albums, "_get_provider_album_tracks", AsyncMock(return_value=[source])
        ),
        patch.object(mass.music.albums, "_set_album_track", AsyncMock()) as update,
    ):
        result = await mass.music.albums.tracks("album", "qobuz_1")
    assert result == libraries
    update.assert_not_awaited()


@pytest.mark.parametrize("identity", ["id", "isrc", "title", "conflict"])
async def test_position_backfill(mass: MusicAssistant, identity: str) -> None:
    """Only an unambiguous copy repairs a library position, including in memory."""
    album = create_album("qobuz_1", "album")
    album.item_id = "10"
    source = entry("qobuz_1", "current", 3, "GBAYC2100001")
    library = entry("library", "42", 0)
    library.provider_mappings = set()
    if identity == "id":
        library.provider_mappings = source.provider_mappings
    elif identity == "isrc":
        library.external_ids = {(ExternalID.ISRC, " gb-ayc-21-00001 ")}
    elif identity == "conflict":
        library.provider_mappings = entry("qobuz_1", "old").provider_mappings
        library.external_ids = source.external_ids
    with (
        patch.object(
            mass.music.albums, "get_library_item_by_prov_id", AsyncMock(return_value=album)
        ),
        patch.object(
            mass.music.albums, "get_library_album_tracks", AsyncMock(return_value=[library])
        ),
        patch.object(
            mass.music.albums, "_get_provider_album_tracks", AsyncMock(return_value=[source])
        ),
        patch.object(mass.music.albums, "_set_album_track", AsyncMock()) as update,
    ):
        result = await mass.music.albums.tracks("album", "qobuz_1")
    if identity == "conflict":
        update.assert_not_awaited()
        assert library.track_number == 0
    else:
        update.assert_awaited_once_with(db_id=10, db_track_id=42, track=source)
        assert result == [library]
        assert library.track_number == 3


@pytest.mark.parametrize("reason", ["id", "isrc", "title", "conflict", "occupied", "duplicate"])
def test_ambiguous_or_conflicting_backfill_is_rejected(reason: str) -> None:
    """Neither repeated identities, repeated titles nor conflicting ISRCs authorize writes."""
    library = entry("library", "42", 0)
    library.provider_mappings = set()
    first, second = entry("a", "one", 1), entry("a", "two", 2)
    libraries = [library]
    providers = [first, second]
    if reason == "id":
        second.provider_mappings = first.provider_mappings
        library.provider_mappings = first.provider_mappings
    elif reason == "isrc":
        for track in (library, first, second):
            track.external_ids = {(ExternalID.ISRC, "GBAYC2100001")}
    elif reason == "conflict":
        library.external_ids = {(ExternalID.ISRC, "GBAYC2100001")}
        first.external_ids = {(ExternalID.ISRC, "GBAYC2100002")}
        providers = [first]
    elif reason == "duplicate":
        duplicate = entry("library", "43", 9)
        duplicate.provider_mappings = library.provider_mappings = first.provider_mappings
        libraries.append(duplicate)
        providers = [first]
    elif reason == "occupied":
        libraries.append(entry("library", "43", 1))
        library.provider_mappings = first.provider_mappings
        providers = [first]
    assert backfills(libraries, providers) == []
