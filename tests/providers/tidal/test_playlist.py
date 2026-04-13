"""Test Tidal Playlist Manager."""

from unittest.mock import AsyncMock, Mock, patch

import pytest

from music_assistant.providers.tidal.playlist import TidalPlaylistManager


@pytest.fixture
def provider_mock() -> Mock:
    """Return a mock provider."""
    provider = Mock()
    provider.auth.user_id = "12345"
    provider.api = AsyncMock()
    provider.logger = Mock()
    return provider


@pytest.fixture
def playlist_manager(provider_mock: Mock) -> TidalPlaylistManager:
    """Return a TidalPlaylistManager instance."""
    return TidalPlaylistManager(provider_mock)


@patch("music_assistant.providers.tidal.playlist.parse_playlist")
async def test_create_playlist(
    mock_parse_playlist: Mock, playlist_manager: TidalPlaylistManager, provider_mock: Mock
) -> None:
    """Test create_playlist."""
    provider_mock.api.post.return_value = {"uuid": "1", "title": "Test Playlist"}
    mock_parse_playlist.return_value = Mock(item_id="1")

    playlist = await playlist_manager.create("Test Playlist")

    assert playlist.item_id == "1"
    provider_mock.api.post.assert_called_with(
        "users/12345/playlists",
        data={"title": "Test Playlist", "description": ""},
        as_form=True,
    )
    mock_parse_playlist.assert_called_once()


async def test_add_playlist_tracks(
    playlist_manager: TidalPlaylistManager, provider_mock: Mock
) -> None:
    """Test add_playlist_tracks."""
    # Mock get response with ETag
    provider_mock.api.get.return_value = ({"numberOfTracks": 5}, "etag_123")

    await playlist_manager.add_tracks("1", ["track_1", "track_2"])

    provider_mock.api.get.assert_called_with("playlists/1", return_etag=True)
    provider_mock.api.post.assert_called_with(
        "playlists/1/items",
        data={
            "onArtifactNotFound": "SKIP",
            "trackIds": "track_1,track_2",
            "toIndex": 5,
            "onDupes": "SKIP",
        },
        as_form=True,
        headers={"If-None-Match": "etag_123"},
    )


async def test_remove_playlist_tracks(
    playlist_manager: TidalPlaylistManager, provider_mock: Mock
) -> None:
    """Test remove_playlist_tracks."""
    # Mock get response with ETag
    provider_mock.api.get.return_value = ({}, "etag_123")

    # Positions are 1-based in MA, converted to 0-based for Tidal
    await playlist_manager.remove_tracks("1", (1, 3))

    provider_mock.api.get.assert_called_with("playlists/1", return_etag=True)
    provider_mock.api.delete.assert_called_with(
        "playlists/1/items/0,2",
        headers={"If-None-Match": "etag_123"},
    )
