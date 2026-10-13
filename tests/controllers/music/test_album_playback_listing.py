"""
Tests for the listing an album is played from.

Each source of an album lists its tracks under an item id of its own. The playback listing keeps,
per provider instance, the id it lists each track under, on the evidence the album page trusts:
the provider id, then the ISRC, then the position or a unique title.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest
from music_assistant_models.enums import ExternalID
from music_assistant_models.helpers import set_global_cache_values
from music_assistant_models.media_items import ProviderMapping, Track

from music_assistant.controllers.music.media.album_tracks import (
    album_listing,
    mapped_album_listing,
    select_album_tracks,
)

from .helpers import create_album, create_track

if TYPE_CHECKING:
    from music_assistant_models.media_items import Album

    from music_assistant.mass import MusicAssistant

FILESYSTEM = "filesystem_local--a"
QOBUZ = "qobuz--a"
SPOTIFY = "spotify--a"
TIDAL = "tidal--a"


def _isrc(number: int) -> str:
    """Return a valid ISRC, distinct per number."""
    return f"GBAYC21{number:05d}"


def _mapping(instance: str, item_id: str) -> ProviderMapping:
    """Build a mapping of a track on the given provider instance."""
    return ProviderMapping(
        item_id=item_id,
        provider_domain=instance.split("--", maxsplit=1)[0],
        provider_instance=instance,
    )


def _row(
    row_id: str,
    position: int,
    *mappings: ProviderMapping,
    isrc: str | None = None,
    name: str | None = None,
) -> Track:
    """Build a library row of the album at the given position."""
    return Track(
        item_id=row_id,
        provider="library",
        name=name or f"Song {position}",
        external_ids={(ExternalID.ISRC, isrc)} if isrc else set(),
        provider_mappings=set(mappings),
        disc_number=1,
        track_number=position,
    )


def _entry(
    instance: str,
    item_id: str,
    position: int,
    isrc: str | None = None,
    name: str | None = None,
) -> Track:
    """Build an entry of a provider's listing of the album; position 0 is no position."""
    return Track(
        item_id=item_id,
        provider=instance,
        name=name or f"Song {position}",
        external_ids={(ExternalID.ISRC, isrc)} if isrc else set(),
        provider_mappings={_mapping(instance, item_id)},
        disc_number=1 if position else 0,
        track_number=position,
    )


def test_a_row_with_two_copies_on_one_instance_is_listed_under_the_albums_entry() -> None:
    """A track the library also has from a compilation is listed under the album's own entry."""
    row = _row("1", 1, _mapping(SPOTIFY, "a_compilation"), _mapping(SPOTIFY, "b_album"))

    listing = album_listing([row], [(SPOTIFY, [_entry(SPOTIFY, "b_album", 1)])])

    assert listing.tracks == [row]
    assert listing.listed_ids == {SPOTIFY: {row.uri: "b_album"}}
    # without the album's listing, the lowest of the track's ids stands for it
    assert mapped_album_listing([row]).listed_ids == {SPOTIFY: {row.uri: "a_compilation"}}


@pytest.mark.parametrize(
    ("handed_back", "listed"),
    [
        (["Album (2009)/01.flac"], "Album (2009)/01.flac"),
        # an album id that is no folder hands back all of the track's files
        (["Album (2009)/01.flac", "Album (1969)/01.flac"], "Album (1969)/01.flac"),
    ],
)
def test_a_library_row_handed_back_is_listed_under_its_own_copy(
    handed_back: list[str], listed: str
) -> None:
    """A filesystem listing hands back the library rows, each with its files of the album."""
    row = _row(
        "1",
        1,
        _mapping(FILESYSTEM, "Album (1969)/01.flac"),
        _mapping(FILESYSTEM, "Album (2009)/01.flac"),
        _mapping(TIDAL, "t1"),
    )
    copy = replace(row, provider_mappings={_mapping(FILESYSTEM, path) for path in handed_back})

    listing = album_listing([row], [(FILESYSTEM, [copy])])

    assert listing.tracks == [row]
    assert listing.listed_ids == {FILESYSTEM: {row.uri: listed}}


def test_a_bonus_track_is_listed_under_the_entry_each_source_has() -> None:
    """A track only the sources have is listed once, under the entry each source has of it."""
    row = _row("1", 1, _mapping(TIDAL, "t1"), _mapping(QOBUZ, "q1"), isrc=_isrc(1))
    tidal = [_entry(TIDAL, "t1", 1, _isrc(1)), _entry(TIDAL, "t2", 2, _isrc(2))]
    qobuz = [_entry(QOBUZ, "q1", 1, _isrc(1)), _entry(QOBUZ, "q2", 2, _isrc(2))]

    listing = album_listing([row], [(TIDAL, tidal), (QOBUZ, qobuz)])

    assert len(listing.tracks) == 2
    bonus = listing.tracks[1]
    assert listing.tracks[0] is row
    # the bonus track is listed as one source's entry, which counts under its own id
    assert (bonus.provider, bonus.item_id) in {(TIDAL, "t2"), (QOBUZ, "q2")}
    assert listing.listed_ids == {
        TIDAL: {row.uri: "t1", bonus.uri: "t2"},
        QOBUZ: {row.uri: "q1", bonus.uri: "q2"},
    }


@pytest.mark.parametrize(
    "entry",
    [
        # the same recording, listed at another position
        _entry(TIDAL, "t2", 5, _isrc(2), name="Bonus edit"),
        # no ISRC, at the row's position
        _entry(TIDAL, "t2", 2, name="Andante (live)"),
        # neither an ISRC nor a position, under the row's title
        _entry(TIDAL, "t2", 0, name="Andante"),
    ],
    ids=["isrc", "position", "title"],
)
def test_an_entry_is_listed_for_the_row_its_evidence_names(entry: Track) -> None:
    """An entry the library row has no mapping of is matched to it as the album page does."""
    row = _row("1", 2, _mapping(FILESYSTEM, "02.flac"), isrc=_isrc(2), name="Andante")

    listing = album_listing([row], [(TIDAL, [entry])])

    assert listing.tracks == [row]
    assert listing.listed_ids == {TIDAL: {row.uri: "t2"}}


def test_another_recording_at_a_rows_position_is_not_listed_for_it() -> None:
    """An entry at the row's position whose ISRC names another recording is no copy of the row."""
    row = _row("1", 2, _mapping(FILESYSTEM, "02.flac"), isrc=_isrc(2))

    listing = album_listing([row], [(TIDAL, [_entry(TIDAL, "t9", 2, _isrc(9))])])

    assert listing.tracks == [row]
    assert listing.listed_ids == {}


def test_the_tracks_are_the_ones_the_album_page_selects() -> None:
    """The listing's tracks are the library rows followed by what the album page lists next to them."""

    def _listings() -> list[tuple[str, list[Track]]]:
        return [
            (TIDAL, [_entry(TIDAL, "t1", 1, _isrc(1)), _entry(TIDAL, "t3", 3, _isrc(3))]),
            (QOBUZ, [_entry(QOBUZ, "q3", 3, _isrc(3)), _entry(QOBUZ, "q4", 0, name="Hidden")]),
        ]

    library = [_row("1", 1, _mapping(TIDAL, "t1"), isrc=_isrc(1))]

    listing = album_listing(library, _listings())

    selected = select_album_tracks(library, [tracks for _, tracks in _listings()])
    assert [track.uri for track in listing.tracks] == [track.uri for track in [*library, *selected]]


@pytest.fixture(name="mass")
def mass_fixture(music_mass_with_cache: MusicAssistant) -> MusicAssistant:
    """Run on a library-only instance with a cache store: album listings are cached."""
    return music_mass_with_cache


def _album_track(instance: str, position: int) -> Track:
    """Return the album's track at the given position, as the given instance has it."""
    track = create_track(instance, f"t{position}", name=f"Song {position}", isrc=_isrc(position))
    track.disc_number, track.track_number = 1, position
    return track


async def _library_album(mass: MusicAssistant, instance: str, *positions: int) -> Album:
    """Add an album of the given instance to the library, with its tracks at the given positions."""
    album = await mass.music.albums.add_item_to_library(create_album(instance, "album_1"))
    for position in positions:
        track = _album_track(instance, position)
        track.album = create_album(instance, "album_1")
        await mass.music.tracks.add_item_to_library(track)
    await set_global_cache_values({"available_providers": {instance}})
    return album


def _listed_tracks(instance: str, *positions: int) -> list[Track]:
    """Return a provider's listing of the album, its entries at the given positions."""
    return [_album_track(instance, position) for position in positions]


async def test_a_sibling_account_lists_for_an_unavailable_mapped_instance(
    mass: MusicAssistant,
) -> None:
    """The listing counts under the account that served it, which plays the same item ids."""
    album = await _library_album(mass, "spotify_a", 1)
    get_provider = mass.get_provider

    def _resolve(provider_id: str, *args: Any, **kwargs: Any) -> Any:
        # the mapped account is unavailable, another account of the service answers for it
        if provider_id == "spotify_a":
            return SimpleNamespace(instance_id="spotify_b")
        return get_provider(provider_id, *args, **kwargs)

    with (
        patch.object(mass, "get_provider", side_effect=_resolve),
        patch.object(
            mass.music.albums,
            "_get_provider_album_tracks",
            AsyncMock(side_effect=lambda *_: _listed_tracks("spotify_b", 1)),
        ),
    ):
        listing = await mass.music.albums.playback_listing(album.item_id, "library")

    assert [track.name for track in listing.tracks] == ["Song 1"]
    assert listing.listed_ids == {"spotify_b": {listing.tracks[0].uri: "t1"}}


async def test_the_tracks_to_play_are_the_album_tracks_listing(mass: MusicAssistant) -> None:
    """The tracks to play are the album's tracks listing, in its order."""
    album = await _library_album(mass, "qobuz_1", 2)
    fetch = AsyncMock(side_effect=lambda *_: _listed_tracks("qobuz_1", 3, 2, 1))

    with patch.object(mass.music.albums, "_get_provider_album_tracks", fetch):
        listed = await mass.music.albums.tracks(album.item_id, "library")
        listing = await mass.music.albums.playback_listing(album.item_id, "library")

    assert [track.uri for track in listing.tracks] == [track.uri for track in listed]
    assert [track.name for track in listing.tracks] == ["Song 1", "Song 2", "Song 3"]
    assert listing.listed_ids == {"qobuz_1": {track.uri: f"t{track.name[-1]}" for track in listed}}


async def test_only_the_library_tracks_are_listed_under_their_own_copies(
    mass: MusicAssistant,
) -> None:
    """With only the library tracks there is no listing to fetch: a track's lowest id counts."""
    album = await _library_album(mass, "qobuz_1", 1)
    row = (await mass.music.albums.get_library_album_tracks(album.item_id))[0]
    await mass.music.tracks.add_provider_mappings(
        row.item_id,
        [ProviderMapping(item_id="t0", provider_domain="qobuz", provider_instance="qobuz_1")],
    )
    fetch = AsyncMock(side_effect=lambda *_: _listed_tracks("qobuz_1", 1, 2))

    with patch.object(mass.music.albums, "_get_provider_album_tracks", fetch):
        listing = await mass.music.albums.playback_listing(
            album.item_id, "library", in_library_only=True
        )

    fetch.assert_not_awaited()
    assert [track.uri for track in listing.tracks] == [row.uri]
    assert listing.listed_ids == {"qobuz_1": {row.uri: "t0"}}


async def test_an_album_outside_the_library_lists_each_track_under_its_own_id(
    mass: MusicAssistant,
) -> None:
    """An album that is not in the library plays as its provider lists it."""
    fetch = AsyncMock(side_effect=lambda *_: _listed_tracks("qobuz_1", 2, 1))
    with (
        patch.object(mass, "get_provider", return_value=SimpleNamespace(instance_id="qobuz_1")),
        patch.object(mass.music.albums, "_get_provider_album_tracks", fetch),
        patch.object(mass.music.albums, "_backfill_album_on_tracks", AsyncMock()),
    ):
        listing = await mass.music.albums.playback_listing("album_x", "qobuz_1")

    assert [track.item_id for track in listing.tracks] == ["t1", "t2"]
    assert listing.listed_ids == {"qobuz_1": {track.uri: track.item_id for track in listing.tracks}}
