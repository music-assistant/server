"""Tests that the iBroadcast parsers hand out item id's the library can match."""

from __future__ import annotations

from typing import Any
from unittest.mock import Mock

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


class FakeIBroadcastClient:
    """
    Stand-in for IBroadcastClient that indexes its library by number, as the real one does.

    A lookup rejects a non-numeric id instead of coercing it, so a provider that hands the
    library a string fails here rather than quietly finding nothing. That is stricter than
    the real client, which coerces, and deliberately so: the provider is meant to keep the
    id in the library's own type and only turn it into text for the media models.
    """

    def __init__(self) -> None:
        """Load the fake library, keyed by number as the real client keys it."""
        self.artists: dict[int, dict[str, Any]] = {ARTIST["artist_id"]: ARTIST}
        self.albums: dict[int, dict[str, Any]] = {ALBUM["album_id"]: ALBUM}
        self.tracks: dict[int, dict[str, Any]] = {TRACK["track_id"]: TRACK}
        self.playlists: dict[int, dict[str, Any]] = {PLAYLIST["playlist_id"]: PLAYLIST}
        #: id's the artwork lookups were called with, in call order
        self.artwork_ids: list[Any] = []

    @staticmethod
    def _key(item_id: Any) -> int:
        if not isinstance(item_id, int):
            msg = f"library is indexed by number, got {item_id!r}"
            raise TypeError(msg)
        return item_id

    async def get_artists(self) -> dict[int, dict[str, Any]]:
        """Return every artist in the fake library."""
        return self.artists

    async def get_albums(self) -> dict[int, dict[str, Any]]:
        """Return every album in the fake library."""
        return self.albums

    async def get_tracks(self) -> dict[int, dict[str, Any]]:
        """Return every track in the fake library."""
        return self.tracks

    async def get_playlists(self) -> dict[int, dict[str, Any]]:
        """Return every playlist in the fake library."""
        return self.playlists

    async def get_artist(self, artist_id: Any) -> dict[str, Any]:
        """Look up one artist by its numeric id."""
        return self.artists.get(self._key(artist_id), {})

    async def get_album(self, album_id: Any) -> dict[str, Any]:
        """Look up one album by its numeric id."""
        return self.albums.get(self._key(album_id), {})

    async def get_track(self, track_id: Any) -> dict[str, Any]:
        """Look up one track by its numeric id."""
        return self.tracks.get(self._key(track_id), {})

    async def get_playlist(self, playlist_id: Any) -> dict[str, Any]:
        """Look up one playlist by its numeric id."""
        return self.playlists.get(self._key(playlist_id), {})

    async def get_artist_artwork_url(self, artist_id: Any) -> str:
        """Record the id it was handed and return an artist artwork url."""
        self.artwork_ids.append(self._key(artist_id))
        return "https://artwork/artist"

    async def get_album_artwork_url(self, album_id: Any) -> str:
        """Record the id it was handed and return an album artwork url."""
        self.artwork_ids.append(self._key(album_id))
        return "https://artwork/album"

    async def get_track_artwork_url(self, track_id: Any) -> str:
        """Record the id it was handed and return a track artwork url."""
        self.artwork_ids.append(self._key(track_id))
        return "https://artwork/track"

    async def get_playlist_artwork_url(self, playlist_id: Any) -> str:
        """Record the id it was handed and return a playlist artwork url."""
        self.artwork_ids.append(self._key(playlist_id))
        return "https://artwork/playlist"


@pytest.fixture
def provider() -> IBroadcastProvider:
    """Create an iBroadcast provider backed by a number keyed fake api client."""
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
    result._client = FakeIBroadcastClient()
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


@pytest.mark.parametrize(
    ("listing", "expected_ids"),
    [
        ("get_library_artists", [42]),
        ("get_library_albums", [101]),
        ("get_library_tracks", [1001]),
        ("get_library_playlists", [5001]),
    ],
)
async def test_artwork_is_looked_up_by_number(
    provider: IBroadcastProvider, listing: str, expected_ids: list[int]
) -> None:
    """The library is indexed by number, so the provider must not hand it the text id."""
    [item async for item in getattr(provider, listing)()]

    assert provider._client.artwork_ids == expected_ids


async def test_library_playlist_id_is_text(provider: IBroadcastProvider) -> None:
    """A numeric playlist id never matches the text id stored against the library item."""
    playlists = [item async for item in provider.get_library_playlists()]

    assert [playlist.item_id for playlist in playlists] == ["5001"]


async def test_various_artists_album_maps_to_the_various_artists_id(
    provider: IBroadcastProvider,
) -> None:
    """An album without an artist is attributed to Various Artists, which uses a mbid."""
    provider._client.albums = {102: {**ALBUM, "album_id": 102, "artist_id": 0}}

    albums = [item async for item in provider.get_library_albums()]

    assert [artist.item_id for album in albums for artist in album.artists] == [
        VARIOUS_ARTISTS_MBID
    ]
    assert [artist.name for album in albums for artist in album.artists] == [VARIOUS_ARTISTS_NAME]


async def test_compilation_track_is_attributed_to_various_artists(
    provider: IBroadcastProvider,
) -> None:
    """Artist id 0 means the track has no single artist, and has no artist object to read."""
    provider._client.tracks = {1001: {**TRACK, "artist_id": 0}}

    tracks = [item async for item in provider.get_library_tracks()]

    assert [artist.name for track in tracks for artist in track.artists] == [VARIOUS_ARTISTS_NAME]
    assert [artist.item_id for track in tracks for artist in track.artists] == [
        VARIOUS_ARTISTS_MBID
    ]


async def test_compilation_track_is_not_skipped(provider: IBroadcastProvider) -> None:
    """Reading the missing artist object first made the whole track fail to parse."""
    provider._client.tracks = {1001: {**TRACK, "artist_id": 0}}
    provider.report_skipped_sync_item = Mock()  # type: ignore[method-assign]

    tracks = [item async for item in provider.get_library_tracks()]

    assert [track.item_id for track in tracks] == ["1001"]
    provider.report_skipped_sync_item.assert_not_called()


@pytest.mark.parametrize(
    "listing",
    [
        "get_library_artists",
        "get_library_albums",
        "get_library_tracks",
        "get_library_playlists",
    ],
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
