"""Test Tidal Media Manager."""

import json
import pathlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import RetriesExhausted
from music_assistant_models.media_items import ItemMapping

from music_assistant.providers.tidal.jsonapi import JsonApiDocument
from music_assistant.providers.tidal.media import TidalMediaManager

FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures" / "v2"


def _load_doc(name: str) -> JsonApiDocument:
    with open(FIXTURES_DIR / name) as f:
        return JsonApiDocument(json.load(f))


@pytest.fixture
def provider_mock() -> Mock:
    """Return a mock provider."""
    provider = Mock()
    provider.domain = "tidal"
    provider.instance_id = "tidal_instance"
    provider.auth.user_id = "12345"
    provider.auth.country_code = "US"
    provider.api = AsyncMock()
    provider.api.get.return_value = {}
    provider.api.paginate = MagicMock()

    async def async_iter(*_args: Any, **_kwargs: Any) -> Any:
        for item in provider.api.paginate.return_value:
            yield item

    provider.api.paginate.side_effect = async_iter
    provider.api.paginate.return_value = []

    provider.logger = Mock()

    def get_item_mapping(media_type: MediaType, key: str, name: str) -> ItemMapping:
        return ItemMapping(
            media_type=media_type,
            item_id=key,
            provider=provider.instance_id,
            name=name,
        )

    provider.get_item_mapping.side_effect = get_item_mapping

    return provider


@pytest.fixture
def media_manager(provider_mock: Mock) -> TidalMediaManager:
    """Return a TidalMediaManager instance."""
    return TidalMediaManager(provider_mock)


@patch("music_assistant.providers.tidal.media.parse_artist")
@patch("music_assistant.providers.tidal.media.parse_album")
@patch("music_assistant.providers.tidal.media.parse_track")
@patch("music_assistant.providers.tidal.media.parse_playlist")
async def test_search(
    mock_parse_playlist: Mock,
    mock_parse_track: Mock,
    mock_parse_album: Mock,
    mock_parse_artist: Mock,
    media_manager: TidalMediaManager,
    provider_mock: Mock,
) -> None:
    """Test search."""
    provider_mock.api.get.return_value = {
        "artists": {"items": [{"id": 1}]},
        "albums": {"items": [{"id": 1}]},
        "tracks": {"items": [{"id": 1}]},
        "playlists": {"items": [{"uuid": "1"}]},
    }

    mock_parse_artist.return_value = Mock(item_id="1", media_type=MediaType.ARTIST)
    mock_parse_album.return_value = Mock(item_id="1", media_type=MediaType.ALBUM)
    mock_parse_track.return_value = Mock(item_id="1", media_type=MediaType.TRACK)
    mock_parse_playlist.return_value = Mock(item_id="1", media_type=MediaType.PLAYLIST)

    results = await media_manager.search(
        "query", [MediaType.ARTIST, MediaType.ALBUM, MediaType.TRACK, MediaType.PLAYLIST]
    )

    assert len(results.artists) == 1
    assert len(results.albums) == 1
    assert len(results.tracks) == 1
    assert len(results.playlists) == 1

    mock_parse_artist.assert_called()
    mock_parse_album.assert_called()
    mock_parse_track.assert_called()
    mock_parse_playlist.assert_called()

    provider_mock.api.get.assert_called_with(
        "search",
        params={
            "query": "query",
            "types": "artists,albums,tracks,playlists",
            "limit": 5,
        },
    )


@patch("music_assistant.providers.tidal.media.parse_artist_v2")
async def test_get_artist(
    mock_parse_artist: Mock, media_manager: TidalMediaManager, provider_mock: Mock
) -> None:
    """Test get_artist uses the official JSON:API endpoint."""
    doc = Mock()
    provider_mock.api.get_jsonapi.return_value = doc
    mock_parse_artist.return_value = Mock(item_id="1")

    artist = await media_manager.get_artist("1")

    assert artist.item_id == "1"
    provider_mock.api.get_jsonapi.assert_called_with(
        "artists/1", include=["profileArt", "biography"]
    )
    mock_parse_artist.assert_called_once_with(provider_mock, doc, doc.data)


@patch("music_assistant.providers.tidal.media.parse_album_v2")
async def test_get_album(
    mock_parse_album: Mock, media_manager: TidalMediaManager, provider_mock: Mock
) -> None:
    """Test get_album uses the official JSON:API endpoint."""
    doc = Mock()
    provider_mock.api.get_jsonapi.return_value = doc
    mock_parse_album.return_value = Mock(item_id="1")

    album = await media_manager.get_album("1")

    assert album.item_id == "1"
    provider_mock.api.get_jsonapi.assert_called_with(
        "albums/1", include=["artists", "coverArt", "genres"]
    )
    mock_parse_album.assert_called_once_with(provider_mock, doc, doc.data)


@patch("music_assistant.providers.tidal.media.parse_track_v2")
async def test_get_track(
    mock_parse_track: Mock, media_manager: TidalMediaManager, provider_mock: Mock
) -> None:
    """Test get_track fetches the official track and applies unofficial lyrics."""
    doc = Mock()
    provider_mock.api.get_jsonapi.return_value = doc
    track = Mock(item_id="1")
    track.metadata = Mock()
    mock_parse_track.return_value = track
    provider_mock.api.get.return_value = {"lyrics": "Test Lyrics", "subtitles": "Synced"}

    result = await media_manager.get_track("1")

    assert result.item_id == "1"
    provider_mock.api.get_jsonapi.assert_called_with(
        "tracks/1", include=["artists", "albums", "albums.coverArt", "genres", "credits"]
    )
    provider_mock.api.get.assert_called_with("tracks/1/lyrics")
    assert track.metadata.lyrics == "Test Lyrics"
    assert track.metadata.lrc_lyrics == "Synced"


@patch("music_assistant.providers.tidal.media.parse_track_v2")
async def test_get_track_tolerates_lyrics_failure(
    mock_parse_track: Mock, media_manager: TidalMediaManager, provider_mock: Mock
) -> None:
    """Test get_track still returns the track when the lyrics lookup fails."""
    doc = Mock()
    provider_mock.api.get_jsonapi.return_value = doc
    track = Mock(item_id="1")
    mock_parse_track.return_value = track
    provider_mock.api.get.side_effect = RetriesExhausted("lyrics lookup failed")

    result = await media_manager.get_track("1")

    assert result.item_id == "1"
    mock_parse_track.assert_called_once_with(provider_mock, doc, doc.data)


async def test_get_album_tracks(media_manager: TidalMediaManager, provider_mock: Mock) -> None:
    """Test album tracks read from the official relationship endpoint with ordering."""
    doc = _load_doc("album_items.json")

    async def _pages(*_a: Any, **_k: Any) -> Any:
        yield doc

    provider_mock.api.paginate_jsonapi = _pages

    tracks = await media_manager.get_album_tracks("58756127")

    assert len(tracks) == 11
    assert tracks[0].name == "7 Years"
    assert tracks[0].track_number == 1
    assert tracks[0].disc_number == 1
    assert tracks[0].album is not None


async def test_get_artist_albums(media_manager: TidalMediaManager, provider_mock: Mock) -> None:
    """Test artist albums read from the official relationship endpoint."""
    doc = _load_doc("artist_albums.json")

    async def _pages(*_a: Any, **_k: Any) -> Any:
        yield doc

    provider_mock.api.paginate_jsonapi = _pages

    albums = await media_manager.get_artist_albums("4184211")

    assert len(albums) == 20
    assert all(album.item_id for album in albums)


async def test_get_artist_toptracks(media_manager: TidalMediaManager, provider_mock: Mock) -> None:
    """Test artist top tracks read from the official relationship endpoint."""
    provider_mock.api.get_jsonapi.return_value = _load_doc("artist_toptracks.json")

    tracks = await media_manager.get_artist_toptracks("4184211")

    assert len(tracks) == 20
    provider_mock.api.get_jsonapi.assert_called_with(
        "artists/4184211/relationships/tracks",
        params={"collapseBy": "FINGERPRINT"},
        include=["tracks.artists", "tracks.albums.coverArt"],
    )


async def test_get_similar_tracks_respects_limit(
    media_manager: TidalMediaManager, provider_mock: Mock
) -> None:
    """Test similar tracks are read from the official endpoint and capped to the limit."""
    provider_mock.api.get_jsonapi.return_value = _load_doc("similar_tracks.json")

    tracks = await media_manager.get_similar_tracks("58756128", limit=5)

    assert len(tracks) == 5


async def test_get_similar_artists(media_manager: TidalMediaManager, provider_mock: Mock) -> None:
    """Test similar artists read from the official relationship endpoint."""
    provider_mock.api.get_jsonapi.return_value = _load_doc("similar_artists.json")

    artists = await media_manager.get_similar_artists("4184211")

    assert len(artists) == 20
    provider_mock.api.get_jsonapi.assert_called_with(
        "artists/4184211/relationships/similarArtists",
        include=["similarArtists.profileArt"],
    )


@patch("music_assistant.providers.tidal.media.parse_playlist")
async def test_get_playlist(
    mock_parse_playlist: Mock, media_manager: TidalMediaManager, provider_mock: Mock
) -> None:
    """Test get_playlist."""
    provider_mock.api.get.return_value = {"uuid": "1", "title": "Test Playlist"}
    mock_parse_playlist.return_value = Mock(item_id="1")

    playlist = await media_manager.get_playlist("1")

    assert playlist.item_id == "1"
    provider_mock.api.get.assert_called_with("playlists/1")
    mock_parse_playlist.assert_called_once()


@patch("music_assistant.providers.tidal.media.parse_track")
async def test_get_playlist_tracks(
    mock_parse_track: Mock, media_manager: TidalMediaManager, provider_mock: Mock
) -> None:
    """Test get_playlist_tracks."""
    provider_mock.api.get.return_value = {"items": [{"id": 1}]}
    mock_parse_track.return_value = Mock(item_id="1")

    tracks = await media_manager.get_playlist_tracks("1")

    assert len(tracks) == 1
    assert tracks[0].item_id == "1"
    provider_mock.api.get.assert_called_with(
        "playlists/1/tracks",
        params={"limit": 200, "offset": 0},
    )


async def test_get_playlist_favorite_tracks(
    media_manager: TidalMediaManager, provider_mock: Mock
) -> None:
    """Test get_playlist returns the virtual favorite tracks playlist without an API call."""
    playlist = await media_manager.get_playlist("favorite_tracks")

    assert playlist.item_id == "favorite_tracks"
    assert playlist.name == "Favorite Tracks"
    assert not playlist.is_editable
    provider_mock.api.get.assert_not_called()


@patch("music_assistant.providers.tidal.media.parse_track")
async def test_get_playlist_tracks_favorite_tracks(
    mock_parse_track: Mock, media_manager: TidalMediaManager, provider_mock: Mock
) -> None:
    """Test get_playlist_tracks returns favorite tracks ordered by date descending."""
    provider_mock.api.get.return_value = {"items": [{"item": {"id": 1}}]}
    mock_parse_track.return_value = Mock(item_id="1")

    tracks = await media_manager.get_playlist_tracks("favorite_tracks", page=0)

    assert len(tracks) == 1
    assert tracks[0].item_id == "1"
    provider_mock.api.get.assert_called_with(
        "users/12345/favorites/tracks",
        params={"limit": 200, "offset": 0, "order": "DATE", "orderDirection": "DESC"},
    )
