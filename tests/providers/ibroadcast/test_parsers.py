"""Tests for the iBroadcast parsers."""

from __future__ import annotations

from typing import Any
from unittest.mock import Mock

import pytest
from music_assistant_models.enums import MediaType

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
        #: kinds whose artwork lookup raises, as the real client does when it finds none
        self.missing_artwork: set[str] = set()
        #: url the stream lookup hands back, before the provider rewrites its bitrate
        self.stream_url = "https://stream.ibroadcast.com/128/file.mp3?Expires=1&Signature=abc"

    def _artwork(self, item_id: int, kind: str) -> str:
        self.artwork_ids.append(item_id)
        if kind in self.missing_artwork:
            msg = f"No artwork found for {kind} with id {item_id}"
            raise ValueError(msg)
        return f"https://artwork/{kind}"

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

    async def get_full_stream_url(self, track_id: Any, platform: str) -> str:
        """Return the stream url for a track by its numeric id."""
        self._key(track_id)
        return self.stream_url

    async def get_artist_artwork_url(self, artist_id: Any) -> str:
        """Record the id it was handed and return an artist artwork url."""
        return self._artwork(self._key(artist_id), "artist")

    async def get_album_artwork_url(self, album_id: Any) -> str:
        """Record the id it was handed and return an album artwork url."""
        return self._artwork(self._key(album_id), "album")

    async def get_track_artwork_url(self, track_id: Any) -> str:
        """Record the id it was handed and return a track artwork url."""
        return self._artwork(self._key(track_id), "track")

    async def get_playlist_artwork_url(self, playlist_id: Any) -> str:
        """Record the id it was handed and return a playlist artwork url."""
        return self._artwork(self._key(playlist_id), "playlist")


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


async def test_track_without_an_album_is_parsed(provider: IBroadcastProvider) -> None:
    """Not every upload belongs to an album, and the rest of the track still parses."""
    provider._client.tracks = {1001: {**TRACK, "album_id": 0}}

    tracks = [item async for item in provider.get_library_tracks()]

    assert [track.item_id for track in tracks] == ["1001"]
    assert tracks[0].album is None


async def test_track_without_artwork_is_parsed(provider: IBroadcastProvider) -> None:
    """The client raises when a track carries no artwork, which is no reason to drop it."""
    provider._client.missing_artwork.add("track")

    tracks = [item async for item in provider.get_library_tracks()]

    assert [track.item_id for track in tracks] == ["1001"]
    assert not tracks[0].metadata.images


async def test_album_without_artwork_is_parsed(provider: IBroadcastProvider) -> None:
    """The client raises when no track of an album carries artwork."""
    provider._client.missing_artwork.add("album")

    albums = [item async for item in provider.get_library_albums()]

    assert [album.item_id for album in albums] == ["101"]
    assert not albums[0].metadata.images


async def test_playlist_without_artwork_is_parsed(provider: IBroadcastProvider) -> None:
    """An empty playlist has no track to take artwork from."""
    provider._client.missing_artwork.add("playlist")

    playlists = [item async for item in provider.get_library_playlists()]

    assert [playlist.item_id for playlist in playlists] == ["5001"]
    assert not playlists[0].metadata.images


async def test_one_unreadable_track_does_not_end_the_listing(provider: IBroadcastProvider) -> None:
    """A listing that gives up part way leaves the rest of the library unsynced."""
    provider._client.tracks = {1001: {**TRACK, "album_id": 0}, 1002: {**TRACK, "track_id": 1002}}

    tracks = [item async for item in provider.get_library_tracks()]

    assert [track.item_id for track in tracks] == ["1001", "1002"]


async def test_stream_url_asks_for_the_original_upload(provider: IBroadcastProvider) -> None:
    """The bitrate segment defaults to 128kbps, so it is swapped for the original format."""
    provider._client.stream_url = (
        "https://stream.ibroadcast.com/128/file.mp3?Expires=1&Signature=abc"
    )

    details = await provider.get_stream_details("1001", MediaType.TRACK)

    assert details.path == "https://stream.ibroadcast.com/orig/file.mp3?Expires=1&Signature=abc"


async def test_stream_url_without_a_bitrate_segment_is_left_alone(
    provider: IBroadcastProvider,
) -> None:
    """Only a numeric first segment is a bitrate, and the rest of the url has to survive."""
    provider._client.stream_url = "https://stream.ibroadcast.com/orig/file.mp3?Expires=1"

    details = await provider.get_stream_details("1001", MediaType.TRACK)

    assert details.path == "https://stream.ibroadcast.com/orig/file.mp3?Expires=1"


async def test_trashed_track_is_mapped_as_unavailable(provider: IBroadcastProvider) -> None:
    """A trashed track stays in the listing, but must not be offered for playback."""
    provider._client.tracks = {1001: {**TRACK, "trashed": True}}

    tracks = [item async for item in provider.get_library_tracks()]

    assert [mapping.available for track in tracks for mapping in track.provider_mappings] == [False]


@pytest.mark.parametrize(
    ("album_disc", "track_field", "expected_disc", "expected_track"),
    [
        (2, 1, 2, 1),
        (0, 201, 2, 1),
        (0, 1, 0, 1),
    ],
    ids=["album_disc_wins", "packed_disc_and_track", "plain_track_number"],
)
async def test_disc_and_track_numbers(
    provider: IBroadcastProvider,
    album_disc: int,
    track_field: int,
    expected_disc: int,
    expected_track: int,
) -> None:
    """Without a disc on the album, a track number over 99 packs the disc into its first digit."""
    provider._client.albums = {101: {**ALBUM, "disc": album_disc}}
    provider._client.tracks = {1001: {**TRACK, "track": track_field}}

    tracks = [item async for item in provider.get_library_tracks()]

    assert tracks[0].disc_number == expected_disc
    assert tracks[0].track_number == expected_track


@pytest.mark.parametrize("playlist_type", ["recently-played", "thumbsup"])
async def test_generated_playlists_are_not_listed(
    provider: IBroadcastProvider, playlist_type: str
) -> None:
    """The two playlists iBroadcast maintains itself do not belong in the library."""
    provider._client.playlists = {5001: {**PLAYLIST, "type": playlist_type}}

    playlists = [item async for item in provider.get_library_playlists()]

    assert playlists == []


async def test_unreadable_track_is_reported_as_skipped_with_a_text_id(
    provider: IBroadcastProvider,
) -> None:
    """The sync protects skipped id's from the deletion pass, which matches them as text."""
    broken = {**TRACK, "track_id": 1002}
    del broken["title"]
    provider._client.tracks = {1001: TRACK, 1002: broken}
    provider.report_skipped_sync_item = Mock()  # type: ignore[method-assign]

    tracks = [item async for item in provider.get_library_tracks()]

    assert [track.item_id for track in tracks] == ["1001"]
    media_type, item_id, _error = provider.report_skipped_sync_item.call_args.args
    assert media_type is MediaType.TRACK
    assert item_id == "1002"


async def test_various_artists_is_listed_when_an_album_uses_it(
    provider: IBroadcastProvider,
) -> None:
    """The sync treats this listing as every artist on the provider."""
    provider._client.albums = {101: {**ALBUM, "artist_id": 0}}

    artists = [item async for item in provider.get_library_artists()]

    assert [artist.item_id for artist in artists] == ["42", VARIOUS_ARTISTS_MBID]
    assert artists[-1].name == VARIOUS_ARTISTS_NAME


async def test_various_artists_is_listed_when_a_track_uses_it(
    provider: IBroadcastProvider,
) -> None:
    """A compilation track maps to Various Artists even when its album does not."""
    provider._client.tracks = {1001: {**TRACK, "artist_id": 0}}

    artists = [item async for item in provider.get_library_artists()]

    assert [artist.item_id for artist in artists] == ["42", VARIOUS_ARTISTS_MBID]


async def test_various_artists_is_not_listed_when_unused(provider: IBroadcastProvider) -> None:
    """A library where everything has a real artist has no Various Artists to map."""
    artists = [item async for item in provider.get_library_artists()]

    assert [artist.item_id for artist in artists] == ["42"]


async def test_listed_various_artists_matches_the_album_mapping(
    provider: IBroadcastProvider,
) -> None:
    """The listed id has to equal the one the albums map to, or the mapping reads as stale."""
    provider._client.albums = {101: {**ALBUM, "artist_id": 0}}

    artists = [item async for item in provider.get_library_artists()]
    albums = [item async for item in provider.get_library_albums()]

    listed = {mapping.item_id for artist in artists for mapping in artist.provider_mappings}
    assert {artist.item_id for album in albums for artist in album.artists} <= listed
