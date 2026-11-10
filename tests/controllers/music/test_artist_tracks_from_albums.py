"""Tests for the tracks of an artist that a provider lists through the artist's releases."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest
from music_assistant_models.enums import AlbumType, ProviderFeature
from music_assistant_models.media_items import Album, ProviderMapping, Track

from music_assistant.controllers.music.constants import ARTIST_ALBUM_LOOKUP_BATCH
from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

pytestmark = pytest.mark.asyncio

_PROVIDER = "streaming_inst"


def _release(name: str, album_type: AlbumType = AlbumType.ALBUM) -> Album:
    return Album(
        item_id=name, provider=_PROVIDER, name=name, album_type=album_type, provider_mappings=set()
    )


def _track(name: str) -> Track:
    mapping = ProviderMapping(
        item_id=name, provider_domain="streaming", provider_instance=_PROVIDER
    )
    return Track(item_id=name, provider=_PROVIDER, name=name, provider_mappings={mapping})


def _provider_without_artist_tracks() -> MagicMock:
    """Return a provider that has to fall back to enumerating the artist's albums."""
    provider = MagicMock(spec=MusicProvider)
    provider.available = True
    provider.supports_feature = MagicMock(
        side_effect=lambda feature: feature != ProviderFeature.ARTIST_TRACKS
    )
    return provider


def _tracklists(
    tracklists: dict[str, list[str]], fetched: list[str]
) -> Callable[[Album], Awaitable[list[Track]]]:
    """Return a fake tracklist fetch of the given names, noting each release it is asked for."""

    async def _album_tracks(album: Album) -> list[Track]:
        fetched.append(album.item_id)
        return [_track(name) for name in tracklists[album.item_id]]

    return _album_tracks


async def _artist_track_names(
    mass: MusicAssistant,
    releases: list[Album],
    album_tracks: Callable[[Album], Awaitable[list[Track]]],
) -> list[str]:
    """Return the names of the tracks listed for an artist with the given releases."""
    with (
        patch.object(mass, "get_provider", return_value=_provider_without_artist_tracks()),
        patch.object(mass.music.artists, "get_provider_artist_albums", return_value=releases),
        patch.object(mass.music.albums, "provider_album_tracks", side_effect=album_tracks),
    ):
        listing = await mass.music.artists.get_provider_artist_tracks("artist1", _PROVIDER)
    return [track.name for track in listing.items]


async def test_artist_tracks_leave_out_the_singles_an_album_lists(mass: MusicAssistant) -> None:
    """A single whose song is on an album is not fetched, a song only out as a single is."""
    releases = [
        _release("Hit", AlbumType.SINGLE),
        _release("Album"),
        _release("Standalone", AlbumType.SINGLE),
        _release("Best Of", AlbumType.COMPILATION),
    ]
    tracklists = {
        "Hit": ["Hit"],
        "Album": ["Hit", "Deep Cut"],
        "Standalone": ["Standalone"],
        "Best Of": ["Hit", "Deep Cut"],
    }
    fetched: list[str] = []
    names = await _artist_track_names(mass, releases, _tracklists(tracklists, fetched))
    # with albums to list, the compilation is not fetched either
    assert fetched == ["Album", "Standalone"]
    assert names == ["Hit", "Deep Cut", "Standalone"]


async def test_artist_tracks_of_an_artist_with_only_compilations(mass: MusicAssistant) -> None:
    """An artist with nothing but compilations still lists the tracks of the compilations."""
    fetched: list[str] = []
    names = await _artist_track_names(
        mass,
        [_release("Best Of", AlbumType.COMPILATION)],
        _tracklists({"Best Of": ["Hit"]}, fetched),
    )
    assert fetched == ["Best Of"]
    assert names == ["Hit"]


async def test_artist_tracks_fetch_a_few_albums_at_a_time(mass: MusicAssistant) -> None:
    """A large discography is fetched a few albums at a time, its tracks in album order."""
    in_flight = 0
    most_in_flight = 0

    async def _album_tracks(album: Album) -> list[Track]:
        nonlocal in_flight, most_in_flight
        in_flight += 1
        most_in_flight = max(most_in_flight, in_flight)
        # let the other fetches of the batch start before this one answers
        await asyncio.sleep(0)
        in_flight -= 1
        return [_track(f"{album.item_id} track")]

    releases = [_release(f"Album {number}") for number in range(12)]
    names = await _artist_track_names(mass, releases, _album_tracks)
    assert most_in_flight == ARTIST_ALBUM_LOOKUP_BATCH
    assert names == [f"Album {number} track" for number in range(12)]
