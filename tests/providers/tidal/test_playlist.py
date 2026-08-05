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
    provider.redirect_cached_id = AsyncMock(side_effect=lambda item_id: item_id)
    provider.resolve_live_track_id = AsyncMock(return_value=None)
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
    assert next(iter(playlist.provider_mappings)).is_unique is True
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
    """Test add_tracks with a single track id, all accepted, no healing needed."""
    provider_mock.api.write_jsonapi.return_value = {"data": [{"type": "tracks", "id": "track_1"}]}

    await playlist_manager.add_tracks("1", ["track_1"])

    provider_mock.api.write_jsonapi.assert_called_once_with(
        "POST",
        "playlists/1/relationships/items",
        {"data": [{"type": "tracks", "id": "track_1"}]},
    )
    provider_mock.resolve_live_track_id.assert_not_called()


async def test_add_playlist_tracks_multiple(
    playlist_manager: TidalPlaylistManager, provider_mock: Mock
) -> None:
    """Test add_tracks batches multiple track ids into a single POST, all accepted."""
    provider_mock.api.write_jsonapi.return_value = {
        "data": [{"type": "tracks", "id": "track_1"}, {"type": "tracks", "id": "track_2"}]
    }

    await playlist_manager.add_tracks("1", ["track_1", "track_2"])

    provider_mock.api.write_jsonapi.assert_called_once_with(
        "POST",
        "playlists/1/relationships/items",
        {"data": [{"type": "tracks", "id": "track_1"}, {"type": "tracks", "id": "track_2"}]},
    )
    provider_mock.resolve_live_track_id.assert_not_called()


async def test_add_playlist_tracks_failure(
    playlist_manager: TidalPlaylistManager, provider_mock: Mock
) -> None:
    """Test add_tracks raises ResourceTemporarilyUnavailable on a client error."""
    provider_mock.api.write_jsonapi.side_effect = ClientError()

    with pytest.raises(ResourceTemporarilyUnavailable):
        await playlist_manager.add_tracks("1", ["track_1"])


async def test_add_playlist_tracks_never_reactively_heals(
    playlist_manager: TidalPlaylistManager, provider_mock: Mock
) -> None:
    """
    Test add_tracks does not resolve/retry when a sent id is absent from the response.

    The playlist items-add response has no per-item skip signal and its data is the
    paginated listing (appended tracks are not on page one). Diffing it would re-POST a
    track that actually succeeded, duplicating it; add_tracks must never do that.
    """
    provider_mock.api.write_jsonapi.return_value = {"data": [{"type": "tracks", "id": "track_1"}]}
    provider_mock.resolve_live_track_id = AsyncMock(return_value="track_2_live")

    await playlist_manager.add_tracks("1", ["track_1", "track_2"])

    provider_mock.resolve_live_track_id.assert_not_called()
    assert provider_mock.api.write_jsonapi.call_count == 1


async def test_add_playlist_tracks_preemptive_redirect(
    playlist_manager: TidalPlaylistManager, provider_mock: Mock
) -> None:
    """Test a cached redirect is applied before sending, avoiding a reactive resolve."""

    async def _redirect(item_id: str) -> str:
        return "track_1_live" if item_id == "track_1" else item_id

    provider_mock.redirect_cached_id = AsyncMock(side_effect=_redirect)
    provider_mock.api.write_jsonapi.return_value = {
        "data": [{"type": "tracks", "id": "track_1_live"}]
    }

    await playlist_manager.add_tracks("1", ["track_1"])

    provider_mock.api.write_jsonapi.assert_called_once_with(
        "POST",
        "playlists/1/relationships/items",
        {"data": [{"type": "tracks", "id": "track_1_live"}]},
    )
    provider_mock.resolve_live_track_id.assert_not_called()


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


async def test_remove_playlist_tracks_ignores_videos(
    playlist_manager: TidalPlaylistManager, provider_mock: Mock
) -> None:
    """Test remove_tracks counts only tracks, as MA's positions come from the track listing."""
    entries = [
        {"type": "tracks", "id": "track_1", "meta": {"itemId": "uuid-1"}},
        {"type": "videos", "id": "video_1", "meta": {"itemId": "uuid-video"}},
        {"type": "tracks", "id": "track_2", "meta": {"itemId": "uuid-2"}},
    ]
    provider_mock.api.paginate_jsonapi = _entries_page(entries)

    # Position 2 is the second TRACK, not the video sitting at relationship index 2.
    await playlist_manager.remove_tracks("1", (2,))

    provider_mock.api.write_jsonapi.assert_called_with(
        "DELETE",
        "playlists/1/relationships/items",
        {"data": [{"type": "tracks", "id": "track_2", "meta": {"itemId": "uuid-2"}}]},
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


async def test_remove_playlist_tracks_skips_entry_missing_item_id(
    playlist_manager: TidalPlaylistManager, provider_mock: Mock
) -> None:
    """Test remove_tracks skips an entry lacking meta.itemId without raising."""
    entries: list[dict[str, Any]] = [
        {"type": "tracks", "id": "track_1"},  # missing "meta" entirely
        {"type": "tracks", "id": "track_2", "meta": {"itemId": "uuid-2"}},
    ]
    provider_mock.api.paginate_jsonapi = _entries_page(entries)

    await playlist_manager.remove_tracks("1", (1, 2))

    provider_mock.api.write_jsonapi.assert_called_with(
        "DELETE",
        "playlists/1/relationships/items",
        {"data": [{"type": "tracks", "id": "track_2", "meta": {"itemId": "uuid-2"}}]},
    )


async def test_remove_playlist_tracks_failure(
    playlist_manager: TidalPlaylistManager, provider_mock: Mock
) -> None:
    """Test remove_tracks raises ResourceTemporarilyUnavailable on a client error."""
    entries = [{"type": "tracks", "id": "track_1", "meta": {"itemId": "uuid-1"}}]
    provider_mock.api.paginate_jsonapi = _entries_page(entries)
    provider_mock.api.write_jsonapi.side_effect = ClientError()

    with pytest.raises(ResourceTemporarilyUnavailable):
        await playlist_manager.remove_tracks("1", (1,))


async def test_remove_playlist_tracks_empty_positions_skips_pagination(
    playlist_manager: TidalPlaylistManager, provider_mock: Mock
) -> None:
    """Test remove_tracks with no positions returns before paginating or writing."""
    provider_mock.api.paginate_jsonapi = Mock(side_effect=AssertionError("should not paginate"))

    await playlist_manager.remove_tracks("1", ())

    provider_mock.api.write_jsonapi.assert_not_called()


async def test_remove_playlist_tracks_bounded_pagination(
    playlist_manager: TidalPlaylistManager, provider_mock: Mock
) -> None:
    """Test remove_tracks stops paginating once enough entries are collected."""
    page_1 = [
        {"type": "tracks", "id": "track_1", "meta": {"itemId": "uuid-1"}},
        {"type": "tracks", "id": "track_2", "meta": {"itemId": "uuid-2"}},
    ]
    page_2 = [
        {"type": "tracks", "id": "track_3", "meta": {"itemId": "uuid-3"}},
    ]
    consumed_pages: list[int] = []

    async def _pages(*_a: Any, **_k: Any) -> Any:
        consumed_pages.append(1)
        yield Mock(data_list=page_1)
        consumed_pages.append(2)
        yield Mock(data_list=page_2)

    provider_mock.api.paginate_jsonapi = _pages

    await playlist_manager.remove_tracks("1", (2,))

    assert consumed_pages == [1]
    provider_mock.api.write_jsonapi.assert_called_with(
        "DELETE",
        "playlists/1/relationships/items",
        {"data": [{"type": "tracks", "id": "track_2", "meta": {"itemId": "uuid-2"}}]},
    )
