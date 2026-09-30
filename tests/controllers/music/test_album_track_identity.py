"""Regression coverage for position-first album listings and safe backfill."""

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
from music_assistant_models.enums import ExternalID

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


@pytest.mark.parametrize(("disc", "count"), [(1, 1), (2, 2)])
def test_unknown_disc(disc: int, count: int) -> None:
    """Disc zero means disc one, never an arbitrary multidisc wildcard."""
    left, right = entry("a", "one"), entry("b", "two")
    left.disc_number, right.disc_number = 0, disc
    assert len(album_tracks.select_album_tracks([], [left, right])) == count


@pytest.mark.parametrize("position", [0, 1, 2])
def test_repeated_unknown_titles_are_preserved(position: int) -> None:
    """Distinct IDs in one source must never disappear through title fallback."""
    tracks = [entry("a", "one", 0), entry("a", "two", 0), entry("b", "three", position)]
    assert len(album_tracks.select_album_tracks([], tracks)) == 3


@pytest.mark.parametrize("position", [0, 1])
def test_unambiguous_cross_provider_title(position: int) -> None:
    """A single missing entry can match a single entry from another source."""
    tracks = [entry("a", "one", 0), entry("b", "two", position)]
    assert len(album_tracks.select_album_tracks([], tracks)) == 1


def test_unknown_title_does_not_choose_repeated_position() -> None:
    """One unknown movement cannot be assigned to either of two positions."""
    tracks = [entry("a", "one", 0), entry("b", "two", 1), entry("c", "three", 2)]
    assert len(album_tracks.select_album_tracks([], tracks)) == 3


def test_library_slot_preferred_despite_identifier_drift() -> None:
    """Position can suppress a listing copy without authorizing a database repair."""
    library = entry("library", "42", 1, "GBAYC2100001")
    source = entry("a", "new", 1, "GBAYC2100002")
    library.provider_mappings = entry("a", "old").provider_mappings
    source.name = "Different title"
    assert album_tracks.select_album_tracks([library], [source]) == []
    assert album_tracks.album_track_backfills([library], [source]) == []


@pytest.mark.parametrize("count", [1000, 3000])
def test_repeated_titles_have_linear_index_work(count: int) -> None:
    """Large classical listings index each entry a bounded number of times."""
    tracks = [entry("a", str(index), index + 1) for index in range(count)]
    with patch.object(album_tracks, "_title", wraps=album_tracks._title) as title:
        assert len(album_tracks.select_album_tracks([], tracks)) == count
        assert album_tracks.album_track_backfills([], tracks) == []
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
    assert album_tracks.album_track_backfills(libraries, providers) == []
