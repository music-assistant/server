"""Test Tidal Media Manager."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import ItemMapping

from music_assistant.providers.tidal.media import TidalMediaManager


@pytest.fixture
def provider_mock() -> Mock:
    """Return a mock provider."""
    provider = Mock()
    provider.domain = "tidal"
    provider.instance_id = "tidal_instance"
    provider.auth.user_id = "12345"
    provider.auth.country_code = "US"
    provider.api = AsyncMock()
    provider.api.get_data.return_value = {}
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
    provider_mock.api.get_data.return_value = {
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

    provider_mock.api.get_data.assert_called_with(
        "search",
        params={
            "query": "query",
            "types": "artists,albums,tracks,playlists",
            "limit": 5,
        },
    )


@patch("music_assistant.providers.tidal.media.parse_artist")
async def test_get_artist(
    mock_parse_artist: Mock, media_manager: TidalMediaManager, provider_mock: Mock
) -> None:
    """Test get_artist."""
    provider_mock.api.get_data.return_value = {"id": 1, "name": "Test Artist"}
    mock_parse_artist.return_value = Mock(item_id="1")

    artist = await media_manager.get_artist("1")

    assert artist.item_id == "1"
    provider_mock.api.get_data.assert_called_with("artists/1")
    mock_parse_artist.assert_called_once()


@patch("music_assistant.providers.tidal.media.parse_album")
async def test_get_album(
    mock_parse_album: Mock, media_manager: TidalMediaManager, provider_mock: Mock
) -> None:
    """Test get_album."""
    provider_mock.api.get_data.return_value = {"id": 1, "title": "Test Album"}
    mock_parse_album.return_value = Mock(item_id="1")

    album = await media_manager.get_album("1")

    assert album.item_id == "1"
    provider_mock.api.get_data.assert_called_with("albums/1")
    mock_parse_album.assert_called_once()


@patch("music_assistant.providers.tidal.media.parse_track")
async def test_get_track(
    mock_parse_track: Mock, media_manager: TidalMediaManager, provider_mock: Mock
) -> None:
    """Test get_track."""
    provider_mock.api.get_data.side_effect = [
        {"id": 1, "title": "Test Track"},  # Track data
        {"lyrics": "Test Lyrics"},  # Lyrics data
    ]
    mock_parse_track.return_value = Mock(item_id="1")

    track = await media_manager.get_track("1")

    assert track.item_id == "1"
    assert provider_mock.api.get_data.call_count == 2
    provider_mock.api.get_data.assert_any_call("tracks/1")
    provider_mock.api.get_data.assert_any_call("tracks/1/lyrics")
    mock_parse_track.assert_called_once()


@patch("music_assistant.providers.tidal.media.parse_playlist")
async def test_get_playlist(
    mock_parse_playlist: Mock, media_manager: TidalMediaManager, provider_mock: Mock
) -> None:
    """Test get_playlist."""
    provider_mock.api.get_data.return_value = {"uuid": "1", "title": "Test Playlist"}
    mock_parse_playlist.return_value = Mock(item_id="1")

    playlist = await media_manager.get_playlist("1")

    assert playlist.item_id == "1"
    provider_mock.api.get_data.assert_called_with("playlists/1")
    mock_parse_playlist.assert_called_once()


@patch("music_assistant.providers.tidal.media.parse_track")
async def test_get_album_tracks(
    mock_parse_track: Mock, media_manager: TidalMediaManager, provider_mock: Mock
) -> None:
    """Test get_album_tracks."""
    provider_mock.api.get_data.return_value = {"items": [{"id": 1}]}
    mock_parse_track.return_value = Mock(item_id="1")

    tracks = await media_manager.get_album_tracks("1")

    assert len(tracks) == 1
    assert tracks[0].item_id == "1"
    provider_mock.api.get_data.assert_called_with(
        "albums/1/tracks",
        params={"limit": 250},
    )


@patch("music_assistant.providers.tidal.media.parse_album")
async def test_get_artist_albums(
    mock_parse_album: Mock, media_manager: TidalMediaManager, provider_mock: Mock
) -> None:
    """Test get_artist_albums."""
    provider_mock.api.get_data.return_value = {"items": [{"id": 1}]}
    mock_parse_album.return_value = Mock(item_id="1")

    albums = await media_manager.get_artist_albums("1")

    assert len(albums) == 1
    assert albums[0].item_id == "1"
    provider_mock.api.get_data.assert_called_with(
        "artists/1/albums",
        params={"limit": 250},
    )


@patch("music_assistant.providers.tidal.media.parse_track")
async def test_get_artist_toptracks(
    mock_parse_track: Mock, media_manager: TidalMediaManager, provider_mock: Mock
) -> None:
    """Test get_artist_toptracks."""
    provider_mock.api.get_data.return_value = {"items": [{"id": 1}]}
    mock_parse_track.return_value = Mock(item_id="1")

    tracks = await media_manager.get_artist_toptracks("1")

    assert len(tracks) == 1
    assert tracks[0].item_id == "1"
    provider_mock.api.get_data.assert_called_with(
        "artists/1/toptracks",
        params={"limit": 10, "offset": 0},
    )


@patch("music_assistant.providers.tidal.media.parse_track")
async def test_get_playlist_tracks(
    mock_parse_track: Mock, media_manager: TidalMediaManager, provider_mock: Mock
) -> None:
    """Test get_playlist_tracks."""
    provider_mock.api.get_data.return_value = {"items": [{"id": 1}]}
    mock_parse_track.return_value = Mock(item_id="1")

    tracks = await media_manager.get_playlist_tracks("1")

    assert len(tracks) == 1
    assert tracks[0].item_id == "1"
    provider_mock.api.get_data.assert_called_with(
        "playlists/1/tracks",
        params={"limit": 200, "offset": 0},
    )
