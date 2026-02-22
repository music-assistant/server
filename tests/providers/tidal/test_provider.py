"""Test Tidal Provider integration."""

from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import LoginFailed
from music_assistant_models.media_items import Album, Artist, Playlist, Track

from music_assistant.providers.tidal.provider import TidalProvider


@pytest.fixture
def mass_mock() -> Mock:
    """Return a mock MusicAssistant instance."""
    mass = Mock()
    mass.http_session = AsyncMock()
    mass.metadata.locale = "en_US"
    mass.cache.get = AsyncMock(return_value=None)
    mass.cache.set = AsyncMock()
    mass.cache.delete = AsyncMock()
    return mass


@pytest.fixture
def manifest_mock() -> Mock:
    """Return a mock provider manifest."""
    manifest = Mock()
    manifest.domain = "tidal"
    return manifest


@pytest.fixture
def config_mock() -> Mock:
    """Return a mock provider config."""
    config = Mock()
    config.name = "Tidal Test"
    config.instance_id = "tidal_test"
    config.enabled = True
    config.get_value.side_effect = lambda key: {
        "auth_token": "mock_access_token",
        "refresh_token": "mock_refresh_token",
        "expiry_time": 1234567890,
        "user_id": "12345",
        "log_level": "INFO",
    }.get(key, "INFO" if "log" in key else None)
    return config


@pytest.fixture
def provider(mass_mock: Mock, manifest_mock: Mock, config_mock: Mock) -> TidalProvider:
    """Return a TidalProvider instance."""
    return TidalProvider(mass_mock, manifest_mock, config_mock)


async def test_provider_initialization(
    mass_mock: Mock, manifest_mock: Mock, config_mock: Mock
) -> None:
    """Test provider initialization creates all managers."""
    provider = TidalProvider(mass_mock, manifest_mock, config_mock)

    assert provider.auth is not None
    assert provider.api is not None
    assert provider.library is not None
    assert provider.media is not None
    assert provider.playlists is not None
    assert provider.recommendations_manager is not None
    assert provider.streaming is not None


async def test_handle_async_init_success(provider: TidalProvider) -> None:
    """Test successful async initialization."""
    with (
        patch.object(provider.auth, "initialize", new_callable=AsyncMock) as mock_init,
        patch.object(provider.api, "get", new_callable=AsyncMock) as mock_get,
        patch.object(provider, "get_user", new_callable=AsyncMock) as mock_get_user,
        patch.object(provider.auth, "update_user_info", new_callable=AsyncMock),
    ):
        mock_init.return_value = True
        mock_get.return_value = ({"userId": "12345", "sessionId": "session_123"}, None)
        mock_get_user.return_value = {"id": "12345", "username": "testuser"}

        await provider.handle_async_init()

        mock_init.assert_called_once()
        mock_get.assert_called_with("sessions")


async def test_handle_async_init_missing_auth() -> None:
    """Test async initialization fails with missing auth."""
    mass = Mock()
    mass.http_session = AsyncMock()
    mass.metadata.locale = "en_US"

    manifest = Mock()
    manifest.domain = "tidal"

    config = Mock()
    config.name = "Tidal Test"
    config.instance_id = "tidal_test"
    config.enabled = True
    config.get_value.side_effect = lambda key: "INFO" if "log" in key else None  # Missing auth data

    provider = TidalProvider(mass, manifest, config)

    with pytest.raises(LoginFailed, match="Missing authentication data"):
        await provider.handle_async_init()


async def test_handle_async_init_auth_failed(provider: TidalProvider) -> None:
    """Test async initialization fails when auth initialize fails."""
    with patch.object(provider.auth, "initialize", new_callable=AsyncMock) as mock_init:
        mock_init.return_value = False

        with pytest.raises(LoginFailed, match="Failed to authenticate with Tidal"):
            await provider.handle_async_init()


async def test_search_delegates_to_media(provider: TidalProvider) -> None:
    """Test search delegates to media manager."""
    with patch.object(provider.media, "search", new_callable=AsyncMock) as mock_search:
        mock_search.return_value = Mock()

        await provider.search("test query", [MediaType.ARTIST], limit=10)

        mock_search.assert_called_with("test query", [MediaType.ARTIST], 10)


async def test_get_similar_tracks_delegates_to_media(provider: TidalProvider) -> None:
    """Test get_similar_tracks delegates to media manager."""
    with patch.object(provider.media, "get_similar_tracks", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = []

        result = await provider.get_similar_tracks("123", limit=30)

        mock_get.assert_called_with("123", 30)
        assert result == []


async def test_get_artist_delegates_to_media(provider: TidalProvider) -> None:
    """Test get_artist delegates to media manager."""
    with patch.object(provider.media, "get_artist", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = Mock(spec=Artist)

        result = await provider.get_artist("123")

        mock_get.assert_called_with("123")
        assert result is not None


async def test_get_album_delegates_to_media(provider: TidalProvider) -> None:
    """Test get_album delegates to media manager."""
    with patch.object(provider.media, "get_album", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = Mock(spec=Album)

        result = await provider.get_album("123")

        mock_get.assert_called_with("123")
        assert result is not None


async def test_get_track_delegates_to_media(provider: TidalProvider) -> None:
    """Test get_track delegates to media manager."""
    with patch.object(provider.media, "get_track", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = Mock(spec=Track)

        result = await provider.get_track("123")

        mock_get.assert_called_with("123")
        assert result is not None


async def test_get_playlist_delegates_to_media(provider: TidalProvider) -> None:
    """Test get_playlist delegates to media manager."""
    with patch.object(provider.media, "get_playlist", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = Mock(spec=Playlist)

        result = await provider.get_playlist("123")

        mock_get.assert_called_with("123")
        assert result is not None


async def test_get_album_tracks_delegates_to_media(provider: TidalProvider) -> None:
    """Test get_album_tracks delegates to media manager."""
    with patch.object(provider.media, "get_album_tracks", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = []

        result = await provider.get_album_tracks("123")

        mock_get.assert_called_with("123")
        assert result == []


async def test_get_artist_albums_delegates_to_media(provider: TidalProvider) -> None:
    """Test get_artist_albums delegates to media manager."""
    with patch.object(provider.media, "get_artist_albums", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = []

        result = await provider.get_artist_albums("123")

        mock_get.assert_called_with("123")
        assert result == []


async def test_get_artist_toptracks_delegates_to_media(provider: TidalProvider) -> None:
    """Test get_artist_toptracks delegates to media manager."""
    with patch.object(provider.media, "get_artist_toptracks", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = []

        await provider.get_artist_toptracks("123")

        mock_get.assert_called_with("123")


async def test_get_playlist_tracks_delegates_to_media(provider: TidalProvider) -> None:
    """Test get_playlist_tracks delegates to media manager."""
    with patch.object(provider.media, "get_playlist_tracks", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = []

        await provider.get_playlist_tracks("123", page=2)

        mock_get.assert_called_with("123", 2)


async def test_get_stream_details_delegates_to_streaming(provider: TidalProvider) -> None:
    """Test get_stream_details delegates to streaming manager."""
    with patch.object(provider.streaming, "get_stream_details", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = Mock()

        result = await provider.get_stream_details("123")

        mock_get.assert_called_with("123")
        assert result is not None


async def test_get_item_mapping(provider: TidalProvider) -> None:
    """Test get_item_mapping creates correct ItemMapping."""
    mapping = provider.get_item_mapping(MediaType.ARTIST, "123", "Test Artist")

    assert mapping.media_type == MediaType.ARTIST
    assert mapping.item_id == "123"
    assert mapping.provider == provider.instance_id
    assert mapping.name == "Test Artist"


async def test_get_library_artists_delegates_to_library(provider: TidalProvider) -> None:
    """Test get_library_artists delegates to library manager."""

    async def mock_generator() -> AsyncGenerator[Any, None]:
        yield Mock(spec=Artist)
        yield Mock(spec=Artist)

    with patch.object(provider.library, "get_artists", return_value=mock_generator()):
        artists = []
        async for artist in provider.get_library_artists():
            artists.append(artist)

        assert len(artists) == 2


async def test_get_library_albums_delegates_to_library(provider: TidalProvider) -> None:
    """Test get_library_albums delegates to library manager."""

    async def mock_generator() -> AsyncGenerator[Any, None]:
        yield Mock(spec=Album)

    with patch.object(provider.library, "get_albums", return_value=mock_generator()):
        albums = []
        async for album in provider.get_library_albums():
            albums.append(album)

        assert len(albums) == 1


async def test_get_library_tracks_delegates_to_library(provider: TidalProvider) -> None:
    """Test get_library_tracks delegates to library manager."""

    async def mock_generator() -> AsyncGenerator[Any, None]:
        yield Mock(spec=Track)
        yield Mock(spec=Track)
        yield Mock(spec=Track)

    with patch.object(provider.library, "get_tracks", return_value=mock_generator()):
        tracks = []
        async for track in provider.get_library_tracks():
            tracks.append(track)

        assert len(tracks) == 3


async def test_get_library_playlists_delegates_to_library(provider: TidalProvider) -> None:
    """Test get_library_playlists delegates to library manager."""

    async def mock_generator() -> AsyncGenerator[Any, None]:
        yield Mock(spec=Playlist)

    with patch.object(provider.library, "get_playlists", return_value=mock_generator()):
        playlists = []
        async for playlist in provider.get_library_playlists():
            playlists.append(playlist)

        assert len(playlists) == 1


async def test_library_add_delegates_to_library(provider: TidalProvider) -> None:
    """Test library_add delegates to library manager."""
    with patch.object(provider.library, "add_item", new_callable=AsyncMock) as mock_add:
        mock_add.return_value = True
        item = Mock()

        result = await provider.library_add(item)

        assert result is True
        mock_add.assert_called_with(item)


async def test_library_remove_delegates_to_library(provider: TidalProvider) -> None:
    """Test library_remove delegates to library manager."""
    with patch.object(provider.library, "remove_item", new_callable=AsyncMock) as mock_remove:
        mock_remove.return_value = True

        result = await provider.library_remove("123", MediaType.TRACK)

        assert result is True
        mock_remove.assert_called_with("123", MediaType.TRACK)


async def test_create_playlist_delegates_to_playlists(provider: TidalProvider) -> None:
    """Test create_playlist delegates to playlist manager."""
    with patch.object(provider.playlists, "create", new_callable=AsyncMock) as mock_create:
        mock_create.return_value = Mock(spec=Playlist)

        await provider.create_playlist("New Playlist")

        mock_create.assert_called_with("New Playlist")


async def test_add_playlist_tracks_delegates_to_playlists(provider: TidalProvider) -> None:
    """Test add_playlist_tracks delegates to playlist manager."""
    with patch.object(provider.playlists, "add_tracks", new_callable=AsyncMock) as mock_add:
        await provider.add_playlist_tracks("123", ["track1", "track2"])

        mock_add.assert_called_with("123", ["track1", "track2"])


async def test_remove_playlist_tracks_delegates_to_playlists(provider: TidalProvider) -> None:
    """Test remove_playlist_tracks delegates to playlist manager."""
    with patch.object(provider.playlists, "remove_tracks", new_callable=AsyncMock) as mock_remove:
        await provider.remove_playlist_tracks("123", (1, 2, 3))

        mock_remove.assert_called_with("123", (1, 2, 3))


async def test_recommendations_delegates_to_recommendations_manager(
    provider: TidalProvider,
) -> None:
    """Test recommendations delegates to recommendations manager."""
    with patch.object(
        provider.recommendations_manager, "get_recommendations", new_callable=AsyncMock
    ) as mock_get:
        mock_get.return_value = []

        await provider.recommendations()

        mock_get.assert_called_once()


async def test_get_user(provider: TidalProvider) -> None:
    """Test get_user fetches user data."""
    with patch.object(provider.api, "get_data", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = {"id": "123", "username": "testuser"}

        user = await provider.get_user("123")

        assert user["id"] == "123"
        mock_get.assert_called_with("users/123")
