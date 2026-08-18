"""Tests that a single unusable Plex item does not abort a library sync."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import plexapi.exceptions
import pytest
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.media_items import Track

from music_assistant.providers.plex import PlexProvider
from music_assistant.providers.plex.helpers import SUPPORTED_FEATURES

LIBRARY_TYPE_AUDIOBOOKS = "audiobooks"
LIBRARY_TYPE_MUSIC = "music"
LIBRARY_TYPE_PODCASTS = "podcasts"

# both listings guard the same destructive failure, so they are covered the same way
SPOKEN_LIBRARIES = [
    (LIBRARY_TYPE_AUDIOBOOKS, "get_library_audiobooks", "_parse_audiobook"),
    (LIBRARY_TYPE_PODCASTS, "get_library_podcasts", "_parse_podcast"),
]
SPOKEN_LIBRARY_LISTINGS = [(library, generator) for library, generator, _ in SPOKEN_LIBRARIES]
SPOKEN_LIBRARY_PARSERS = [(library, parser) for library, _, parser in SPOKEN_LIBRARIES]


class _FakePlexData:
    """Stub for the cached xml payload of a plex object."""

    def __init__(self, attrib: dict[str, str]) -> None:
        self.attrib = attrib

    def findall(self, _tag: str) -> list[Any]:
        return []


class _FakePlexTrack:
    """
    Minimal PlexTrack stub carrying what the track parser reads.

    Leaving both original_title and grandparent_key empty reproduces a track that Plex
    holds no artist for, which the parser rejects with an InvalidDataError.
    """

    def __init__(
        self,
        key: str = "/library/metadata/1",
        title: str = "Track 1",
        original_title: str | None = None,
        grandparent_key: str | None = "/library/metadata/10",
    ) -> None:
        self.key = key
        self.title = title
        self.originalTitle = original_title
        self.grandparentKey = grandparent_key
        self.grandparentTitle = "Artist 1"
        self.parentKey = "/library/metadata/100"
        self.parentTitle = "Album 1"
        self.parentIndex = 1
        self.trackNumber = 1
        self.duration = 180000
        self.genres: list[Any] = []
        self.moods: list[Any] = []
        media = MagicMock()
        media.container = "mp3"
        self.media = [media]
        self._data = _FakePlexData({"title": title, "key": key})

    def getWebURL(self, baseurl: str) -> str:  # noqa: N802
        return f"{baseurl}/web/index.html#!/server/item/{self.key}"

    def firstAttr(self, *attrs: str) -> str | None:  # noqa: N802
        return None


class _FakePlexAlbum:
    """Minimal PlexAlbum stub for the audiobook library."""

    def __init__(
        self,
        key: str = "/library/metadata/500",
        title: str = "Audiobook 1",
        year: int | None = None,
    ) -> None:
        self.key = key
        self.title = title
        self.year = year
        self.studio = None
        self.parentTitle = "Author 1"
        self.grandparentTitle = None
        self.summary = ""
        self._data = _FakePlexData({"title": title, "key": key})

    def getWebURL(self, baseurl: str) -> str:  # noqa: N802
        return f"{baseurl}/web/index.html#!/server/item/{self.key}"

    def firstAttr(self, *attrs: str) -> str | None:  # noqa: N802
        return None


def _make_provider(library_type: str = LIBRARY_TYPE_MUSIC) -> Any:
    """Create a minimal PlexProvider instance for testing."""
    mock_mass = MagicMock()
    mock_config = MagicMock()
    mock_config.instance_id = "plex_instance_1"
    config_values: dict[str, Any] = {"library_type": library_type, "log_level": "INFO"}
    mock_config.get_value = lambda key: config_values.get(key)

    setup_data = {"library_type": library_type, "token": "local_auth"}
    mock_mass.config.get = lambda key, default=None: (
        setup_data if str(key).endswith("/setup_data") else default
    )
    mock_mass.config.get_raw_provider_config_value = lambda _instance_id, _key: None
    mock_mass.config.decrypt_string = lambda value: value

    mock_manifest = MagicMock()
    mock_manifest.type = "music"
    mock_manifest.domain = "plex"

    provider = PlexProvider(mock_mass, mock_manifest, mock_config, SUPPORTED_FEATURES)
    provider._baseurl = "http://localhost:32400"
    provider._plex_server = MagicMock()
    provider._plex_library = MagicMock()
    return provider


async def test_library_tracks_skips_track_without_artist() -> None:
    """A track that Plex holds no artist for is skipped, the rest still syncs."""
    provider = _make_provider()
    batches = [[_FakePlexTrack(key="/1", grandparent_key=None), _FakePlexTrack(key="/2")], []]
    provider._plex_library.searchTracks = MagicMock(side_effect=batches)

    tracks = [track async for track in provider.get_library_tracks()]

    assert [track.item_id for track in tracks] == ["/2"]


async def test_library_artists_skips_item_without_valid_id() -> None:
    """An artist Plex holds no usable id for is skipped, the rest still syncs."""
    provider = _make_provider()
    good_artist = MagicMock()
    provider._plex_library.all = MagicMock(return_value=[MagicMock(), good_artist])

    async def _parse_artist(plex_artist: Any) -> Any:
        if plex_artist is good_artist:
            return "parsed"
        raise InvalidDataError("Artist does not have a valid ID")

    provider._parse_artist = _parse_artist

    artists = [artist async for artist in provider.get_library_artists()]

    assert artists == ["parsed"]


@pytest.mark.parametrize(
    "error",
    [
        plexapi.exceptions.Unauthorized("invalid token"),
        plexapi.exceptions.NotFound("gone"),
        ConnectionError("server gone"),
    ],
)
async def test_library_tracks_does_not_swallow_server_errors(error: Exception) -> None:
    """
    An error that is not about the item itself aborts the sync instead of skipping a track.

    Skipping would drop the track from this run, and the sync deletion pass then removes
    it from the library even though it is still on the server.
    """
    provider = _make_provider()
    # a terminating second batch, so widening the caught errors fails the test instead
    # of looping on the same batch forever
    provider._plex_library.searchTracks = MagicMock(side_effect=[[_FakePlexTrack()], []])

    async def _parse_track(_plex_track: Any) -> Any:
        raise error

    provider._parse_track = _parse_track

    with pytest.raises(type(error)):
        _ = [track async for track in provider.get_library_tracks()]


@pytest.mark.parametrize(("library_type", "generator", "parse_method"), SPOKEN_LIBRARIES)
async def test_spoken_library_skips_unparsable_item(
    library_type: str, generator: str, parse_method: str
) -> None:
    """An audiobook or podcast that cannot be parsed is skipped, the rest still syncs."""
    provider = _make_provider(library_type)
    good_album = _FakePlexAlbum(key="/501")
    provider._plex_library.albums = MagicMock(return_value=[_FakePlexAlbum(), good_album])

    async def _parse(plex_album: Any, **_kwargs: Any) -> Any:
        if plex_album is good_album:
            return "parsed"
        raise InvalidDataError("no title")

    setattr(provider, parse_method, _parse)

    items = [item async for item in getattr(provider, generator)()]

    assert items == ["parsed"]


@pytest.mark.parametrize(("library_type", "generator"), SPOKEN_LIBRARY_LISTINGS)
async def test_spoken_library_does_not_swallow_listing_error(
    library_type: str, generator: str
) -> None:
    """
    A failure to list the audiobook or podcast library aborts the sync.

    Yielding nothing would look exactly like an emptied library, which makes the sync
    deletion pass remove every item that is still on the server.
    """
    provider = _make_provider(library_type)
    provider._plex_library.albums = MagicMock(side_effect=ConnectionError("server gone"))

    with pytest.raises(ConnectionError):
        _ = [item async for item in getattr(provider, generator)()]


@pytest.mark.parametrize(("library_type", "parse_method"), SPOKEN_LIBRARY_PARSERS)
async def test_spoken_item_ignores_out_of_range_year(library_type: str, parse_method: str) -> None:
    """A year Plex cannot express as a date is ignored instead of failing the item."""
    provider = _make_provider(library_type)

    parsed = await getattr(provider, parse_method)(_FakePlexAlbum(year=19999))

    assert parsed.metadata.release_date is None


async def test_album_tracks_skips_unparsable_track() -> None:
    """An unusable track no longer costs the whole tracklist of an album."""
    provider = _make_provider()
    bad_track, good_track = _FakePlexTrack(key="/1"), _FakePlexTrack(key="/2")
    plex_album = MagicMock()
    plex_album.tracks = MagicMock(return_value=[bad_track, good_track])
    provider._get_data = AsyncMock(return_value=plex_album)

    async def _parse_track(plex_track: Any) -> Any:
        if plex_track is bad_track:
            raise InvalidDataError("No artist was found for track")
        return "parsed"

    provider._parse_track = _parse_track

    get_album_tracks: Any = PlexProvider.get_album_tracks.__wrapped__  # type: ignore[attr-defined]

    assert await get_album_tracks(provider, "/library/metadata/100") == ["parsed"]


async def test_playlist_tracks_skips_unparsable_track() -> None:
    """
    An unusable track no longer makes the whole playlist unplayable.

    The remaining tracks keep consecutive positions, so the skip does not leave a hole in
    the queue built from this playlist.
    """
    provider = _make_provider()
    tracks = [_FakePlexTrack(key="/1"), _FakePlexTrack(key="/2"), _FakePlexTrack(key="/3")]
    plex_playlist = MagicMock()
    plex_playlist.items = MagicMock(return_value=tracks)
    provider._get_data = AsyncMock(return_value=plex_playlist)

    async def _parse_track(plex_track: Any) -> Any:
        if plex_track is tracks[0]:
            raise InvalidDataError("No artist was found for track")
        return Track(
            item_id=plex_track.key,
            provider=provider.instance_id,
            name=plex_track.title,
            provider_mappings=set(),
        )

    provider._parse_track = _parse_track

    get_playlist_tracks: Any = PlexProvider.get_playlist_tracks.__wrapped__  # type: ignore[attr-defined]
    result = await get_playlist_tracks(provider, "/playlists/1")

    assert [track.item_id for track in result] == ["/2", "/3"]
    assert [track.position for track in result] == [1, 2]
