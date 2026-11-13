"""
Tests for the order an artist plays in (``default_enqueue_order_artist``).

The order setting plays the artist shuffled (the default), or plays the artist's albums one after
the other, at random or oldest first. The album orders play the artist's library albums and leave
out singles. Each album plays as it does on its own.
The tests resolve an artist through a real MusicAssistant with a real SQLite database.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from music_assistant_models.enums import AlbumType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.helpers import set_global_cache_values
from music_assistant_models.media_items import Album, Artist, ProviderMapping, Track
from music_assistant_models.unique_list import UniqueList

from music_assistant.controllers.player_queues import media_resolver
from music_assistant.controllers.player_queues.constants import (
    CONF_DEFAULT_ENQUEUE_ORDER_ARTIST,
    CONF_DEFAULT_ENQUEUE_SELECT_ALBUM,
    CONF_DEFAULT_ENQUEUE_SELECT_ARTIST,
    ArtistOrder,
)

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


@pytest.fixture(autouse=True)
async def _providers_available(mass: MusicAssistant) -> None:  # noqa: ARG001
    """Make the test providers available, so their tracks are playable."""
    # the server's boot sets the providers it loaded, so this must run after it
    await set_global_cache_values({"available_providers": {"library", "streaming", "other"}})


def _library_mapping() -> set[ProviderMapping]:
    """Create a single library provider mapping with a unique provider item id."""
    return {
        ProviderMapping(
            item_id=uuid4().hex,
            provider_domain="library",
            provider_instance="library",
            in_library=True,
        )
    }


def _track(name: str) -> Track:
    """Build a playable track as a provider returns it."""
    return Track(item_id=name, provider="test", name=name, provider_mappings=_library_mapping())


def _names(tracks: list[Track]) -> list[str]:
    """Return the track names in order."""
    return [track.name for track in tracks]


async def _add_artist(mass: MusicAssistant) -> Artist:
    """Add a library artist."""
    return await mass.music.artists.add_item_to_library(
        Artist(item_id="0", provider="library", name="ABBA", provider_mappings=_library_mapping())
    )


async def _add_album(
    mass: MusicAssistant,
    artist: Artist,
    name: str,
    year: int | None,
    songs: tuple[str, ...] = (),
    version: str = "",
    album_type: AlbumType = AlbumType.ALBUM,
) -> Album:
    """
    Add a library album by the artist, with three tracks added out of order.

    The tracks are named after the album and their number, unless ``songs`` gives their names.
    """
    album = await mass.music.albums.add_item_to_library(
        Album(
            item_id="0",
            provider="library",
            name=name,
            version=version,
            year=year,
            album_type=album_type,
            provider_mappings=_library_mapping(),
            artists=UniqueList([artist]),
        )
    )
    for number in (3, 1, 2):
        await mass.music.tracks.add_item_to_library(
            Track(
                item_id="0",
                provider="library",
                name=songs[number - 1] if songs else f"{name} {number}",
                provider_mappings=_library_mapping(),
                artists=UniqueList([artist]),
                album=album,
                disc_number=1,
                track_number=number,
            )
        )
    return album


async def _artist_with_albums(mass: MusicAssistant) -> Artist:
    """Add a library artist with a later and an earlier album."""
    artist = await _add_artist(mass)
    await _add_album(mass, artist, "Voulez-Vous", 1979)
    await _add_album(mass, artist, "Waterloo", 1974)
    return artist


def _stub_album_tracks(mass: MusicAssistant, albums: list[Album]) -> AsyncMock:
    """
    Let the album tracks lookup answer two tracks for each of the given albums.

    The tracks are named after the album and their number. Returns the lookup.
    """
    names = {album.item_id: album.name for album in albums}
    lookup = AsyncMock(
        side_effect=lambda item_id, *_, **__: [_track(f"{names[item_id]} {n}") for n in (1, 2)]
    )
    mass.music.albums.tracks = lookup  # type: ignore[method-assign]
    return lookup


def _set_order(mass: MusicAssistant, order: str) -> None:
    """Set the artist order option."""
    mass.config.set_raw_core_config_value("player_queues", CONF_DEFAULT_ENQUEUE_ORDER_ARTIST, order)


def _set_selection(mass: MusicAssistant, selection: str) -> None:
    """Set the artist selection option."""
    mass.config.set_raw_core_config_value(
        "player_queues", CONF_DEFAULT_ENQUEUE_SELECT_ARTIST, selection
    )


async def test_artist_by_release_plays_its_albums_oldest_first(mass: MusicAssistant) -> None:
    """An artist set to release order plays its albums oldest first, each in track order."""
    artist = await _artist_with_albums(mass)
    _set_order(mass, ArtistOrder.ALBUMS_BY_RELEASE)

    result = await mass.player_queues._media_resolver.get_artist_tracks(artist)

    assert _names(result) == [
        "Waterloo 1",
        "Waterloo 2",
        "Waterloo 3",
        "Voulez-Vous 1",
        "Voulez-Vous 2",
        "Voulez-Vous 3",
    ]


async def test_albums_without_a_year_play_after_the_dated_ones(mass: MusicAssistant) -> None:
    """An album with no year, or a year of 0, can't be placed, so it plays last."""
    artist = await _add_artist(mass)
    await _add_album(mass, artist, "Undated", None, ("Undated 1", "Undated 2", "Undated 3"))
    await _add_album(mass, artist, "Zero", 0, ("Zero 1", "Zero 2", "Zero 3"))
    await _add_album(mass, artist, "Dated", 2001)
    _set_order(mass, ArtistOrder.ALBUMS_BY_RELEASE)

    result = await mass.player_queues._media_resolver.get_artist_tracks(artist)

    assert _names(result)[:3] == ["Dated 1", "Dated 2", "Dated 3"]
    assert len(result) == 9


async def test_artist_random_albums_plays_each_album_in_track_order(mass: MusicAssistant) -> None:
    """An artist set to random albums plays each album in track order."""
    artist = await _artist_with_albums(mass)
    _set_order(mass, ArtistOrder.RANDOM_ALBUMS)

    result = await mass.player_queues._media_resolver.get_artist_tracks(artist)

    waterloo = ["Waterloo 1", "Waterloo 2", "Waterloo 3"]
    voulez_vous = ["Voulez-Vous 1", "Voulez-Vous 2", "Voulez-Vous 3"]
    assert _names(result) in (waterloo + voulez_vous, voulez_vous + waterloo)


async def test_random_albums_varies_the_album_order(mass: MusicAssistant) -> None:
    """The albums do not always come out in the same order."""
    artist = await _add_artist(mass)
    for index in range(4):
        await _add_album(mass, artist, f"Album {index}", 2000 + index)
    _set_order(mass, ArtistOrder.RANDOM_ALBUMS)
    resolver = mass.player_queues._media_resolver

    orders = {tuple(_names(await resolver.get_artist_tracks(artist))) for _ in range(20)}

    assert len(orders) > 1


async def test_artist_order_defaults_to_shuffled_tracks(mass: MusicAssistant) -> None:
    """Without an order set, the artist's library songs play shuffled."""
    artist = await _add_artist(mass)
    await _add_album(mass, artist, "Arrival", 1976, ("When I Kissed", "Dancing Queen", "My Love"))
    await _add_album(mass, artist, "Gold", 1992, ("Dancing Queen", "Knowing Me", "Take a Chance"))

    assert mass.player_queues._media_resolver.get_artist_order() is ArtistOrder.SHUFFLED_TRACKS
    result = await mass.player_queues._media_resolver.get_artist_tracks(artist)

    assert sorted(_names(result)) == [
        "Dancing Queen",
        "Dancing Queen",
        "Knowing Me",
        "My Love",
        "Take a Chance",
        "When I Kissed",
    ]


async def test_an_unknown_order_falls_back_to_shuffled_tracks(mass: MusicAssistant) -> None:
    """A stored order this version does not know plays the artist shuffled."""
    _set_order(mass, "newest_first")

    assert mass.player_queues._media_resolver.get_artist_order() is ArtistOrder.SHUFFLED_TRACKS


async def test_a_song_on_two_albums_plays_on_both(mass: MusicAssistant) -> None:
    """Each album plays as it does on its own, so a song on two albums plays on both."""
    artist = await _add_artist(mass)
    await _add_album(mass, artist, "Arrival", 1976, ("When I Kissed", "Dancing Queen", "My Love"))
    await _add_album(mass, artist, "Gold", 1992, ("Dancing Queen", "Knowing Me", "Take a Chance"))
    _set_order(mass, ArtistOrder.ALBUMS_BY_RELEASE)

    result = await mass.player_queues._media_resolver.get_artist_tracks(artist)

    assert _names(result) == [
        "When I Kissed",
        "Dancing Queen",
        "My Love",
        "Dancing Queen",
        "Knowing Me",
        "Take a Chance",
    ]


async def test_same_named_albums_and_editions_all_play(mass: MusicAssistant) -> None:
    """Self-titled albums of different years, and an edition of the same year, each play."""
    artist = await _add_artist(mass)
    await _add_album(mass, artist, "ABBA", 1980, ("Intro", "Andante", "Elaine"))
    await _add_album(mass, artist, "ABBA", 1975, ("Intro", "Mamma Mia", "SOS"))
    await _add_album(mass, artist, "ABBA", 1975, ("Intro", "Mamma Mia", "Bonus"), "Deluxe")
    _set_order(mass, ArtistOrder.ALBUMS_BY_RELEASE)

    result = await mass.player_queues._media_resolver.get_artist_tracks(artist)

    assert _names(result) == [
        "Intro",
        "Mamma Mia",
        "SOS",
        "Intro",
        "Mamma Mia",
        "Bonus",
        "Intro",
        "Andante",
        "Elaine",
    ]


async def test_library_albums_leave_out_only_singles(mass: MusicAssistant) -> None:
    """A library compilation was saved by the user, so it plays; only singles are left out."""
    artist = await _add_artist(mass)
    await _add_album(mass, artist, "Waterloo", 1974)
    await _add_album(mass, artist, "Single", 1975, album_type=AlbumType.SINGLE)
    await _add_album(mass, artist, "Gold", 1992, album_type=AlbumType.COMPILATION)
    await _add_album(mass, artist, "Untyped", 2008, album_type=AlbumType.UNKNOWN)
    _set_order(mass, ArtistOrder.ALBUMS_BY_RELEASE)

    result = await mass.player_queues._media_resolver.get_artist_tracks(artist)

    assert _names(result) == [
        "Waterloo 1",
        "Waterloo 2",
        "Waterloo 3",
        "Gold 1",
        "Gold 2",
        "Gold 3",
        "Untyped 1",
        "Untyped 2",
        "Untyped 3",
    ]


async def test_an_artist_with_only_singles_plays_shuffled(mass: MusicAssistant) -> None:
    """Without an album to play, the artist's tracks play shuffled instead."""
    artist = await _add_artist(mass)
    await _add_album(mass, artist, "Single", 1975, album_type=AlbumType.SINGLE)
    _set_order(mass, ArtistOrder.ALBUMS_BY_RELEASE)

    result = await mass.player_queues._media_resolver.get_artist_tracks(artist)

    assert sorted(_names(result)) == ["Single 1", "Single 2", "Single 3"]


async def test_top_tracks_plays_them_shuffled(mass: MusicAssistant) -> None:
    """With top tracks selected, an album order plays the top tracks, with no album looked up."""
    artist = await _artist_with_albums(mass)
    mass.music.artists.top_tracks = AsyncMock(return_value=[_track("Waterloo")])  # type: ignore[method-assign]
    album_tracks = mass.music.albums.tracks = AsyncMock()  # type: ignore[method-assign]
    _set_selection(mass, "top_tracks")
    _set_order(mass, ArtistOrder.ALBUMS_BY_RELEASE)

    result = await mass.player_queues._media_resolver.get_artist_tracks(artist)

    assert _names(result) == ["Waterloo"]
    album_tracks.assert_not_called()


async def test_prefer_library_without_albums_plays_the_top_tracks(mass: MusicAssistant) -> None:
    """Prefer library with no library album to play falls back to the top tracks."""
    artist = await _add_artist(mass)
    mass.music.artists.top_tracks = AsyncMock(return_value=[_track("Waterloo")])  # type: ignore[method-assign]
    _set_selection(mass, "prefer_library")
    _set_order(mass, ArtistOrder.ALBUMS_BY_RELEASE)

    result = await mass.player_queues._media_resolver.get_artist_tracks(artist)

    assert _names(result) == ["Waterloo"]


@pytest.mark.parametrize("order", list(ArtistOrder))
async def test_an_artist_outside_the_library_plays_its_top_tracks(
    mass: MusicAssistant, order: ArtistOrder
) -> None:
    """An artist that is not in the library plays its top tracks, in any order."""
    artist = Artist(
        item_id="abba",
        provider="streaming",
        name="ABBA",
        provider_mappings={
            ProviderMapping(
                item_id="abba", provider_domain="streaming", provider_instance="streaming"
            )
        },
    )
    mass.music.artists.top_tracks = AsyncMock(return_value=[_track("Waterloo")])  # type: ignore[method-assign]
    albums = mass.music.artists.albums = AsyncMock(return_value=[])  # type: ignore[method-assign]
    tracks = mass.music.artists.tracks = AsyncMock(return_value=[])  # type: ignore[method-assign]
    _set_order(mass, order)

    result = await mass.player_queues._media_resolver.get_artist_tracks(artist)

    assert _names(result) == ["Waterloo"]
    albums.assert_not_awaited()
    tracks.assert_not_awaited()


@pytest.mark.parametrize("selection", ["library_tracks", "library_album_tracks", "prefer_library"])
async def test_an_album_order_plays_only_library_albums(
    mass: MusicAssistant, selection: str
) -> None:
    """An album order plays the artist's library albums, never the albums a provider lists."""
    artist = await _add_artist(mass)
    await _add_album(mass, artist, "Waterloo", 1974)
    artist.provider_mappings.add(
        ProviderMapping(item_id="abba", provider_domain="streaming", provider_instance="streaming")
    )
    provider_albums = AsyncMock(return_value=[])
    mass.music.artists.get_provider_artist_albums = provider_albums  # type: ignore[method-assign]
    _set_selection(mass, selection)
    _set_order(mass, ArtistOrder.ALBUMS_BY_RELEASE)

    result = await mass.player_queues._media_resolver.get_artist_tracks(artist)

    assert _names(result) == ["Waterloo 1", "Waterloo 2", "Waterloo 3"]
    provider_albums.assert_not_awaited()


@pytest.mark.parametrize("selection", ["library_tracks", "prefer_library", "library_album_tracks"])
@pytest.mark.parametrize(
    ("album_setting", "in_library_only"), [("library_tracks", True), ("all_tracks", False)]
)
async def test_an_album_plays_as_it_does_on_its_own(
    mass: MusicAssistant, selection: str, album_setting: str, in_library_only: bool
) -> None:
    """The album setting decides the tracks of each album, whatever the artist selection."""
    artist = await _add_artist(mass)
    await _add_album(mass, artist, "Waterloo", 1974)
    lookup = AsyncMock(return_value=[_track("Waterloo 1")])
    mass.music.albums.tracks = lookup  # type: ignore[method-assign]
    mass.config.set_raw_core_config_value(
        "player_queues", CONF_DEFAULT_ENQUEUE_SELECT_ALBUM, album_setting
    )
    _set_selection(mass, selection)
    _set_order(mass, ArtistOrder.ALBUMS_BY_RELEASE)

    await mass.player_queues._media_resolver.get_artist_tracks(artist)

    assert lookup.call_args.kwargs["in_library_only"] is in_library_only


async def test_a_failing_album_lookup_keeps_the_other_albums(mass: MusicAssistant) -> None:
    """When one album's tracks can't be looked up, the other albums still play."""
    artist = await _add_artist(mass)
    voulez_vous = await _add_album(mass, artist, "Voulez-Vous", 1979)
    waterloo = await _add_album(mass, artist, "Waterloo", 1974)
    lookup = _stub_album_tracks(mass, [voulez_vous, waterloo])
    provider_tracks = lookup.side_effect

    def _tracks(item_id: str, *args: object, **kwargs: object) -> list[Track]:
        if item_id == waterloo.item_id:
            raise MediaNotFoundError("provider is offline")
        return provider_tracks(item_id, *args, **kwargs)  # type: ignore[no-any-return]

    lookup.side_effect = _tracks
    _set_order(mass, ArtistOrder.ALBUMS_BY_RELEASE)

    result = await mass.player_queues._media_resolver.get_artist_tracks(artist)

    assert _names(result) == ["Voulez-Vous 1", "Voulez-Vous 2"]


async def test_an_unexpected_album_lookup_error_is_raised(mass: MusicAssistant) -> None:
    """An error that isn't a provider failure is a bug, so it isn't hidden."""
    artist = await _add_artist(mass)
    lookup = _stub_album_tracks(mass, [await _add_album(mass, artist, "Waterloo", 1974)])
    lookup.side_effect = RuntimeError("bug")
    _set_order(mass, ArtistOrder.ALBUMS_BY_RELEASE)

    with pytest.raises(RuntimeError, match="bug"):
        await mass.player_queues._media_resolver.get_artist_tracks(artist)


async def test_album_lookups_run_in_small_batches(mass: MusicAssistant) -> None:
    """A big discography is looked up a few albums at a time, and still plays in order."""
    artist = await _add_artist(mass)
    albums = [
        await _add_album(mass, artist, f"Album {index:02}", 1970 + index) for index in range(12)
    ]
    lookup = _stub_album_tracks(mass, albums)
    provider_tracks = lookup.side_effect
    running = 0
    peak = 0

    async def _tracks(item_id: str, *args: object, **kwargs: object) -> list[Track]:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0)
        running -= 1
        return provider_tracks(item_id, *args, **kwargs)  # type: ignore[no-any-return]

    lookup.side_effect = _tracks
    _set_order(mass, ArtistOrder.ALBUMS_BY_RELEASE)

    result = await mass.player_queues._media_resolver.get_artist_tracks(artist)

    assert 1 < peak <= media_resolver.ALBUM_LOOKUP_BATCH
    assert _names(result)[::2] == [f"Album {index:02} 1" for index in range(12)]


def _unsaved_mapping() -> set[ProviderMapping]:
    """Create a provider mapping for an item MA knows but the user didn't save."""
    return {
        ProviderMapping(
            item_id=uuid4().hex,
            provider_domain="library",
            provider_instance="library",
            in_library=False,
        )
    }


async def _add_song(
    mass: MusicAssistant, artist: Artist, album: Album, name: str, saved: bool = True
) -> None:
    """Add a song by the artist on the given album, saved or only known to MA."""
    await mass.music.tracks.add_item_to_library(
        Track(
            item_id="0",
            provider="library",
            name=name,
            provider_mappings=_library_mapping() if saved else _unsaved_mapping(),
            artists=UniqueList([artist]),
            album=album,
            disc_number=1,
            track_number=9,
        )
    )


@pytest.mark.parametrize("selection", ["library_tracks", "prefer_library", "library_album_tracks"])
@pytest.mark.parametrize("album_setting", ["library_tracks", "all_tracks"])
async def test_the_artist_plays_each_album_as_the_album_button_does(
    mass: MusicAssistant, selection: str, album_setting: str
) -> None:
    """Each album of the artist plays the same tracks as playing that album on its own."""
    artist = await _add_artist(mass)
    waterloo = await _add_album(mass, artist, "Waterloo", 1974)
    arrival = await _add_album(mass, artist, "Arrival", 1976)
    await _add_song(mass, artist, arrival, "Arrival unsaved", saved=False)
    resolver = mass.player_queues._media_resolver
    mass.config.set_raw_core_config_value(
        "player_queues", CONF_DEFAULT_ENQUEUE_SELECT_ALBUM, album_setting
    )
    _set_selection(mass, selection)
    _set_order(mass, ArtistOrder.ALBUMS_BY_RELEASE)

    result = await resolver.get_artist_tracks(artist)

    album_buttons = [
        *await resolver.get_album_tracks(waterloo, None),
        *await resolver.get_album_tracks(arrival, None),
    ]
    assert _names(result) == _names(album_buttons)
    assert "Arrival unsaved" in _names(result)


@pytest.mark.parametrize("selection", ["library_tracks", "prefer_library"])
async def test_a_saved_song_does_not_bring_its_unsaved_album(
    mass: MusicAssistant, selection: str
) -> None:
    """A library selection plays the saved albums, not the album of a song saved on its own."""
    artist = await _add_artist(mass)
    await _add_album(mass, artist, "Waterloo", 1974)
    arrival = await mass.music.albums.add_item_to_library(
        Album(
            item_id="0",
            provider="library",
            name="Arrival",
            year=1976,
            album_type=AlbumType.ALBUM,
            provider_mappings=_unsaved_mapping(),
            artists=UniqueList([artist]),
        )
    )
    await _add_song(mass, artist, arrival, "Dancing Queen")
    _set_selection(mass, selection)
    _set_order(mass, ArtistOrder.ALBUMS_BY_RELEASE)

    resolver = mass.player_queues._media_resolver

    assert _names(await resolver.get_artist_tracks(artist)) == [
        "Waterloo 1",
        "Waterloo 2",
        "Waterloo 3",
    ]
    # the song is the artist's, so shuffled tracks play it
    _set_order(mass, ArtistOrder.SHUFFLED_TRACKS)
    assert "Dancing Queen" in _names(await resolver.get_artist_tracks(artist))


async def test_a_song_on_another_artists_album_is_left_out(mass: MusicAssistant) -> None:
    """An album order plays the artist's own albums, not the compilations it appears on."""
    artist = await _add_artist(mass)
    await _add_album(mass, artist, "Waterloo", 1974)
    various = await mass.music.artists.add_item_to_library(
        Artist(
            item_id="0",
            provider="library",
            name="Various Artists",
            provider_mappings=_library_mapping(),
        )
    )
    hits = await _add_album(mass, various, "Hits", 1980, album_type=AlbumType.COMPILATION)
    await _add_song(mass, artist, hits, "Dancing Queen")
    _set_selection(mass, "library_tracks")
    _set_order(mass, ArtistOrder.ALBUMS_BY_RELEASE)

    resolver = mass.player_queues._media_resolver

    assert _names(await resolver.get_artist_tracks(artist)) == [
        "Waterloo 1",
        "Waterloo 2",
        "Waterloo 3",
    ]
    # the song is the artist's, so shuffled tracks play it
    _set_order(mass, ArtistOrder.SHUFFLED_TRACKS)
    assert "Dancing Queen" in _names(await resolver.get_artist_tracks(artist))


async def test_an_artist_with_albums_never_falls_back_to_shuffled_songs(
    mass: MusicAssistant,
) -> None:
    """
    Albums that come back empty play nothing, as they do on their own.

    The artist still counts as playing albums, which turns the queue's shuffle off.
    """
    artist = await _add_artist(mass)
    # a saved streaming album stores no tracks
    await mass.music.albums.add_item_to_library(
        Album(
            item_id="0",
            provider="library",
            name="Arrival",
            year=1976,
            album_type=AlbumType.ALBUM,
            provider_mappings=_library_mapping(),
            artists=UniqueList([artist]),
        )
    )
    various = await mass.music.artists.add_item_to_library(
        Artist(
            item_id="0", provider="library", name="Various", provider_mappings=_library_mapping()
        )
    )
    await _add_song(mass, artist, await _add_album(mass, various, "Hits", 1980), "Dancing Queen")
    mass.config.set_raw_core_config_value(
        "player_queues", CONF_DEFAULT_ENQUEUE_SELECT_ALBUM, "library_tracks"
    )
    _set_selection(mass, "library_tracks")
    _set_order(mass, ArtistOrder.ALBUMS_BY_RELEASE)
    resolver = mass.player_queues._media_resolver

    resolved = await resolver.resolve_artist_tracks(artist)
    assert resolved.tracks == []
    assert resolved.plays_albums is True


async def test_albums_that_all_fail_to_load_play_nothing(mass: MusicAssistant) -> None:
    """When every album lookup fails, the artist plays nothing instead of shuffled songs."""
    artist = await _add_artist(mass)
    lookup = _stub_album_tracks(mass, [await _add_album(mass, artist, "Waterloo", 1974)])
    lookup.side_effect = MediaNotFoundError("provider is offline")
    _set_order(mass, ArtistOrder.ALBUMS_BY_RELEASE)

    assert await mass.player_queues._media_resolver.get_artist_tracks(artist) == []
