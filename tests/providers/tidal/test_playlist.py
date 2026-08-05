"""Test Tidal Playlist Manager."""

from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp.client_exceptions import ClientError
from music_assistant_models.errors import ResourceTemporarilyUnavailable

from music_assistant.providers.tidal.playlist import TidalPlaylistManager


@pytest.fixture
def provider_mock() -> Mock:
    """Return a mock provider."""
    provider = Mock()
    provider.domain = "tidal"
    provider.instance_id = "tidal_instance"
    provider.auth.user_id = "12345"
    provider.auth.user.profile_name = "Some User"
    provider.auth.user.user_name = "someuser"
    provider.api = AsyncMock()
    provider.logger = Mock()
    return provider


@pytest.fixture
def playlist_manager(provider_mock: Mock) -> TidalPlaylistManager:
    """Return a TidalPlaylistManager instance."""
    return TidalPlaylistManager(provider_mock)


async def test_create_playlist(playlist_manager: TidalPlaylistManager, provider_mock: Mock) -> None:
    """Test create posts to the v1 user playlists endpoint and is marked editable."""
    provider_mock.api.post.return_value = {"uuid": "pl1", "title": "Test Playlist"}

    playlist = await playlist_manager.create("Test Playlist")

    assert playlist.item_id == "pl1"
    # A freshly created playlist is forced editable/owned regardless of the echo.
    assert playlist.is_editable is True
    assert playlist.owner == "Some User"
    assert next(iter(playlist.provider_mappings)).is_unique is True
    provider_mock.api.post.assert_called_with(
        "users/12345/playlists",
        data={"title": "Test Playlist", "description": ""},
        as_form=True,
    )


async def test_create_playlist_failure(
    playlist_manager: TidalPlaylistManager, provider_mock: Mock
) -> None:
    """Test create raises ResourceTemporarilyUnavailable on a client error."""
    provider_mock.api.post.side_effect = ClientError()

    with pytest.raises(ResourceTemporarilyUnavailable):
        await playlist_manager.create("Test Playlist")


async def test_add_playlist_tracks_single(
    playlist_manager: TidalPlaylistManager, provider_mock: Mock
) -> None:
    """Test add_tracks appends via v1 items with the playlist ETag."""
    provider_mock.api.get_with_etag.return_value = ({"numberOfTracks": 3}, "etag-1")

    await playlist_manager.add_tracks("1", ["track_1"])

    provider_mock.api.get_with_etag.assert_called_once_with("playlists/1")
    provider_mock.api.post.assert_called_once_with(
        "playlists/1/items",
        data={
            "onArtifactNotFound": "SKIP",
            "trackIds": "track_1",
            "toIndex": 3,
            "onDupes": "SKIP",
        },
        as_form=True,
        headers={"If-None-Match": "etag-1"},
    )


async def test_add_playlist_tracks_multiple(
    playlist_manager: TidalPlaylistManager, provider_mock: Mock
) -> None:
    """Test add_tracks joins track ids and appends at the current end index."""
    provider_mock.api.get_with_etag.return_value = ({"numberOfTracks": 10}, "etag-2")

    await playlist_manager.add_tracks("1", ["track_1", "track_2"])

    provider_mock.api.post.assert_called_once_with(
        "playlists/1/items",
        data={
            "onArtifactNotFound": "SKIP",
            "trackIds": "track_1,track_2",
            "toIndex": 10,
            "onDupes": "SKIP",
        },
        as_form=True,
        headers={"If-None-Match": "etag-2"},
    )


async def test_add_playlist_tracks_without_etag(
    playlist_manager: TidalPlaylistManager, provider_mock: Mock
) -> None:
    """Test add_tracks omits the If-None-Match header when no ETag is returned."""
    provider_mock.api.get_with_etag.return_value = ({"numberOfTracks": 0}, "")

    await playlist_manager.add_tracks("1", ["track_1"])

    assert provider_mock.api.post.call_args.kwargs["headers"] == {}


async def test_add_playlist_tracks_failure(
    playlist_manager: TidalPlaylistManager, provider_mock: Mock
) -> None:
    """Test add_tracks raises ResourceTemporarilyUnavailable on a client error."""
    provider_mock.api.get_with_etag.side_effect = ClientError()

    with pytest.raises(ResourceTemporarilyUnavailable):
        await playlist_manager.add_tracks("1", ["track_1"])


async def test_remove_playlist_tracks(
    playlist_manager: TidalPlaylistManager, provider_mock: Mock
) -> None:
    """Test remove_tracks deletes v1 items by 0-based index with the playlist ETag."""
    provider_mock.api.get_with_etag.return_value = ({}, "etag-r")

    await playlist_manager.remove_tracks("1", (1, 3))

    provider_mock.api.get_with_etag.assert_called_once_with("playlists/1")
    provider_mock.api.delete.assert_called_once_with(
        "playlists/1/items/0,2",
        headers={"If-None-Match": "etag-r"},
    )


async def test_remove_playlist_tracks_single(
    playlist_manager: TidalPlaylistManager, provider_mock: Mock
) -> None:
    """Test a single 1-based position maps to its 0-based index."""
    provider_mock.api.get_with_etag.return_value = ({}, "etag-r")

    await playlist_manager.remove_tracks("1", (2,))

    provider_mock.api.delete.assert_called_once_with(
        "playlists/1/items/1",
        headers={"If-None-Match": "etag-r"},
    )


async def test_remove_playlist_tracks_without_etag(
    playlist_manager: TidalPlaylistManager, provider_mock: Mock
) -> None:
    """Test remove_tracks omits the If-None-Match header when no ETag is returned."""
    provider_mock.api.get_with_etag.return_value = ({}, "")

    await playlist_manager.remove_tracks("1", (1,))

    assert provider_mock.api.delete.call_args.kwargs["headers"] == {}


async def test_remove_playlist_tracks_empty_positions_skips_calls(
    playlist_manager: TidalPlaylistManager, provider_mock: Mock
) -> None:
    """Test remove_tracks with no positions returns before fetching the ETag or deleting."""
    await playlist_manager.remove_tracks("1", ())

    provider_mock.api.get_with_etag.assert_not_called()
    provider_mock.api.delete.assert_not_called()


async def test_remove_playlist_tracks_failure(
    playlist_manager: TidalPlaylistManager, provider_mock: Mock
) -> None:
    """Test remove_tracks raises ResourceTemporarilyUnavailable on a client error."""
    provider_mock.api.get_with_etag.return_value = ({}, "etag-r")
    provider_mock.api.delete.side_effect = ClientError()

    with pytest.raises(ResourceTemporarilyUnavailable):
        await playlist_manager.remove_tracks("1", (1,))
