"""Test Tidal Playlist Manager."""

from typing import Any
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
    """Test create posts to the official playlists endpoint and is marked editable."""
    provider_mock.api.write_jsonapi.return_value = {
        "data": {
            "type": "playlists",
            "id": "pl1",
            "attributes": {
                "name": "Test Playlist",
                "description": "",
                "playlistType": "USER",
            },
        }
    }

    playlist = await playlist_manager.create("Test Playlist")

    assert playlist.item_id == "pl1"
    assert playlist.is_editable is True
    assert playlist.owner == "Some User"
    provider_mock.api.write_jsonapi.assert_called_with(
        "POST",
        "playlists",
        {
            "data": {
                "type": "playlists",
                "attributes": {
                    "name": "Test Playlist",
                    "description": "",
                    "accessType": "UNLISTED",
                },
            }
        },
    )


async def test_create_playlist_failure(
    playlist_manager: TidalPlaylistManager, provider_mock: Mock
) -> None:
    """Test create raises ResourceTemporarilyUnavailable on a client error."""
    provider_mock.api.write_jsonapi.side_effect = ClientError()

    with pytest.raises(ResourceTemporarilyUnavailable):
        await playlist_manager.create("Test Playlist")


async def test_add_playlist_tracks_single(
    playlist_manager: TidalPlaylistManager, provider_mock: Mock
) -> None:
    """Test add_tracks with a single track id."""
    await playlist_manager.add_tracks("1", ["track_1"])

    provider_mock.api.write_jsonapi.assert_called_with(
        "POST",
        "playlists/1/relationships/items",
        {"data": [{"type": "tracks", "id": "track_1"}]},
    )


async def test_add_playlist_tracks_multiple(
    playlist_manager: TidalPlaylistManager, provider_mock: Mock
) -> None:
    """Test add_tracks batches multiple track ids into a single POST."""
    await playlist_manager.add_tracks("1", ["track_1", "track_2"])

    provider_mock.api.write_jsonapi.assert_called_with(
        "POST",
        "playlists/1/relationships/items",
        {"data": [{"type": "tracks", "id": "track_1"}, {"type": "tracks", "id": "track_2"}]},
    )


async def test_add_playlist_tracks_failure(
    playlist_manager: TidalPlaylistManager, provider_mock: Mock
) -> None:
    """Test add_tracks raises ResourceTemporarilyUnavailable on a client error."""
    provider_mock.api.write_jsonapi.side_effect = ClientError()

    with pytest.raises(ResourceTemporarilyUnavailable):
        await playlist_manager.add_tracks("1", ["track_1"])


def _entries_page(entries: list[dict[str, Any]]) -> Any:
    """Build an async generator function yielding a single page of entries."""

    async def _pages(*_a: Any, **_k: Any) -> Any:
        yield Mock(data_list=entries)

    return _pages


async def test_remove_playlist_tracks(
    playlist_manager: TidalPlaylistManager, provider_mock: Mock
) -> None:
    """Test remove_tracks resolves 1-based positions to their itemId and deletes them."""
    entries = [
        {"type": "tracks", "id": "track_1", "meta": {"itemId": "uuid-1"}},
        {"type": "tracks", "id": "track_2", "meta": {"itemId": "uuid-2"}},
        {"type": "tracks", "id": "track_3", "meta": {"itemId": "uuid-3"}},
    ]
    provider_mock.api.paginate_jsonapi = _entries_page(entries)

    await playlist_manager.remove_tracks("1", (1, 3))

    provider_mock.api.write_jsonapi.assert_called_with(
        "DELETE",
        "playlists/1/relationships/items",
        {
            "data": [
                {"type": "tracks", "id": "track_1", "meta": {"itemId": "uuid-1"}},
                {"type": "tracks", "id": "track_3", "meta": {"itemId": "uuid-3"}},
            ]
        },
    )


async def test_remove_playlist_tracks_duplicate_track_ids(
    playlist_manager: TidalPlaylistManager, provider_mock: Mock
) -> None:
    """Test remove_tracks targets the correct occurrence when a track repeats."""
    entries = [
        {"type": "tracks", "id": "track_1", "meta": {"itemId": "uuid-1"}},
        {"type": "tracks", "id": "track_1", "meta": {"itemId": "uuid-2"}},
        {"type": "tracks", "id": "track_1", "meta": {"itemId": "uuid-3"}},
    ]
    provider_mock.api.paginate_jsonapi = _entries_page(entries)

    # Position 2 (1-based) is the second occurrence of track_1: uuid-2.
    await playlist_manager.remove_tracks("1", (2,))

    provider_mock.api.write_jsonapi.assert_called_with(
        "DELETE",
        "playlists/1/relationships/items",
        {"data": [{"type": "tracks", "id": "track_1", "meta": {"itemId": "uuid-2"}}]},
    )


async def test_remove_playlist_tracks_out_of_range_position(
    playlist_manager: TidalPlaylistManager, provider_mock: Mock
) -> None:
    """Test remove_tracks skips out-of-range positions and does not write when empty."""
    entries = [{"type": "tracks", "id": "track_1", "meta": {"itemId": "uuid-1"}}]
    provider_mock.api.paginate_jsonapi = _entries_page(entries)

    await playlist_manager.remove_tracks("1", (5,))

    provider_mock.api.write_jsonapi.assert_not_called()


async def test_remove_playlist_tracks_failure(
    playlist_manager: TidalPlaylistManager, provider_mock: Mock
) -> None:
    """Test remove_tracks raises ResourceTemporarilyUnavailable on a client error."""
    entries = [{"type": "tracks", "id": "track_1", "meta": {"itemId": "uuid-1"}}]
    provider_mock.api.paginate_jsonapi = _entries_page(entries)
    provider_mock.api.write_jsonapi.side_effect = ClientError()

    with pytest.raises(ResourceTemporarilyUnavailable):
        await playlist_manager.remove_tracks("1", (1,))
