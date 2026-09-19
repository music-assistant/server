"""Tests for the iBroadcast parsers."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from music_assistant.constants import VARIOUS_ARTISTS_MBID, VARIOUS_ARTISTS_NAME
from music_assistant.providers.ibroadcast import SUPPORTED_FEATURES, IBroadcastProvider

# the api returns every id as a number, while the library stores it as text
ARTIST: dict[str, Any] = {"artist_id": 42, "name": "Some Artist", "artwork_id": 7}
ALBUM: dict[str, Any] = {
    "album_id": 101,
    "name": "Some Album",
    "year": 2020,
    "artist_id": 42,
    "disc": 0,
    "tracks": [1001],
}
TRACK: dict[str, Any] = {
    "track_id": 1001,
    "title": "Some Track",
    "trashed": False,
    "album_id": 101,
    "track": 1,
    "length": 180,
    "artist_id": 42,
    "artists_additional": [],
    "genre": "Rock",
    "genres_additional": None,
}
PLAYLIST: dict[str, Any] = {"playlist_id": 5001, "name": "Some Playlist", "type": "normal"}


@pytest.fixture
def provider() -> IBroadcastProvider:
    """Create an iBroadcast provider with a mocked api client."""
    mass = Mock()
    manifest = Mock()
    manifest.domain = "ibroadcast"
    config = Mock()
    config.instance_id = "ibroadcast--test123"
    config.name = "iBroadcast Test"
    config.enabled = True
    config.get_value.return_value = "GLOBAL"
    result = IBroadcastProvider(mass, manifest, config, SUPPORTED_FEATURES)
    result._user_id = "user"
    client = Mock()
    client.get_artist = AsyncMock(return_value=ARTIST)
    client.get_album = AsyncMock(return_value=ALBUM)
    client.get_artists = AsyncMock(return_value={ARTIST["artist_id"]: ARTIST})
    client.get_albums = AsyncMock(return_value={ALBUM["album_id"]: ALBUM})
    client.get_tracks = AsyncMock(return_value={TRACK["track_id"]: TRACK})
    client.get_playlists = AsyncMock(return_value={PLAYLIST["playlist_id"]: PLAYLIST})
    client.get_artist_artwork_url = AsyncMock(return_value="https://artwork/artist")
    client.get_album_artwork_url = AsyncMock(return_value="https://artwork/album")
    client.get_track_artwork_url = AsyncMock(return_value="https://artwork/track")
    client.get_playlist_artwork_url = AsyncMock(return_value="https://artwork/playlist")
    result._client = client
    return result


async def test_library_artist_id_is_text(provider: IBroadcastProvider) -> None:
    """A numeric artist id never matches the text id stored against the library item."""
    artists = [item async for item in provider.get_library_artists()]

    assert [artist.item_id for artist in artists] == ["42"]
    assert [mapping.item_id for artist in artists for mapping in artist.provider_mappings] == ["42"]


async def test_library_album_id_is_text(provider: IBroadcastProvider) -> None:
    """A numeric album id never matches the text id stored against the library item."""
    albums = [item async for item in provider.get_library_albums()]

    assert [album.item_id for album in albums] == ["101"]
    assert [mapping.item_id for album in albums for mapping in album.provider_mappings] == ["101"]
    assert [artist.item_id for album in albums for artist in album.artists] == ["42"]


async def test_library_track_id_is_text(provider: IBroadcastProvider) -> None:
    """A numeric track id never matches the text id stored against the library item."""
    tracks = [item async for item in provider.get_library_tracks()]

    assert [track.item_id for track in tracks] == ["1001"]
    assert [mapping.item_id for track in tracks for mapping in track.provider_mappings] == ["1001"]
    assert [artist.item_id for track in tracks for artist in track.artists] == ["42"]
    assert [track.album.item_id for track in tracks if track.album] == ["101"]


async def test_track_artwork_is_looked_up_by_number(provider: IBroadcastProvider) -> None:
    """The client indexes its library by number, so the text id has to be converted back."""
    [item async for item in provider.get_library_tracks()]

    provider._client.get_track_artwork_url.assert_awaited_once_with(1001)


async def test_various_artists_album_keeps_its_artist_id(provider: IBroadcastProvider) -> None:
    """An album without an artist is attributed to Various Artists, which uses a mbid."""
    provider._client.get_albums = AsyncMock(
        return_value={102: {**ALBUM, "album_id": 102, "artist_id": 0}}
    )

    albums = [item async for item in provider.get_library_albums()]

    assert all(isinstance(artist.item_id, str) for album in albums for artist in album.artists)


async def test_compilation_track_is_attributed_to_various_artists(
    provider: IBroadcastProvider,
) -> None:
    """Artist id 0 means the track has no single artist, and has no artist object to read."""
    provider._client.get_tracks = AsyncMock(return_value={1001: {**TRACK, "artist_id": 0}})
    provider._client.get_artist = AsyncMock(return_value={})

    tracks = [item async for item in provider.get_library_tracks()]

    assert [artist.name for track in tracks for artist in track.artists] == [VARIOUS_ARTISTS_NAME]
    assert [artist.item_id for track in tracks for artist in track.artists] == [
        VARIOUS_ARTISTS_MBID
    ]


async def test_compilation_track_is_not_skipped(provider: IBroadcastProvider) -> None:
    """Reading the missing artist object first made the whole track fail to parse."""
    provider._client.get_tracks = AsyncMock(return_value={1001: {**TRACK, "artist_id": 0}})
    provider._client.get_artist = AsyncMock(return_value={})
    provider.report_skipped_sync_item = Mock()  # type: ignore[method-assign]

    tracks = [item async for item in provider.get_library_tracks()]

    assert [track.item_id for track in tracks] == ["1001"]
    provider.report_skipped_sync_item.assert_not_called()


@pytest.mark.parametrize(
    "listing", ["get_library_artists", "get_library_albums", "get_library_tracks"]
)
async def test_listed_item_ids_are_all_text(provider: IBroadcastProvider, listing: str) -> None:
    """
    The sync compares listed id's against the provider mappings table, which holds text.

    A non-text id matches nothing there, which makes the sync drop every mapping this
    provider has and delete the library items that carry no other one.
    """
    items = [item async for item in getattr(provider, listing)()]

    assert items
    assert all(isinstance(item.item_id, str) for item in items)


async def test_track_without_an_album_is_parsed(provider: IBroadcastProvider) -> None:
    """Not every upload belongs to an album, and the rest of the track still parses."""
    provider._client.get_tracks = AsyncMock(return_value={1001: {**TRACK, "album_id": 0}})

    tracks = [item async for item in provider.get_library_tracks()]

    assert [track.item_id for track in tracks] == ["1001"]
    assert tracks[0].album is None


async def test_track_without_artwork_is_parsed(provider: IBroadcastProvider) -> None:
    """The client raises when a track carries no artwork, which is no reason to drop it."""
    provider._client.get_track_artwork_url = AsyncMock(side_effect=ValueError("no artwork"))

    tracks = [item async for item in provider.get_library_tracks()]

    assert [track.item_id for track in tracks] == ["1001"]
    assert not tracks[0].metadata.images


async def test_album_without_artwork_is_parsed(provider: IBroadcastProvider) -> None:
    """The client raises when no track of an album carries artwork."""
    provider._client.get_album_artwork_url = AsyncMock(side_effect=ValueError("no artwork"))

    albums = [item async for item in provider.get_library_albums()]

    assert [album.item_id for album in albums] == ["101"]
    assert not albums[0].metadata.images


async def test_playlist_without_artwork_is_parsed(provider: IBroadcastProvider) -> None:
    """An empty playlist has no track to take artwork from."""
    provider._client.get_playlist_artwork_url = AsyncMock(side_effect=ValueError("no artwork"))

    playlists = [item async for item in provider.get_library_playlists()]

    assert [playlist.item_id for playlist in playlists] == ["5001"]
    assert not playlists[0].metadata.images


async def test_one_unreadable_track_does_not_end_the_listing(provider: IBroadcastProvider) -> None:
    """A listing that gives up part way leaves the rest of the library unsynced."""
    provider._client.get_tracks = AsyncMock(
        return_value={1001: {**TRACK, "album_id": 0}, 1002: {**TRACK, "track_id": 1002}}
    )

    tracks = [item async for item in provider.get_library_tracks()]

    assert [track.item_id for track in tracks] == ["1001", "1002"]
