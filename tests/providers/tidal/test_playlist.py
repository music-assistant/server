"""Test Tidal Playlist Manager."""

from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp.client_exceptions import ClientError
from music_assistant_models.errors import InvalidDataError, ResourceTemporarilyUnavailable

from music_assistant.providers.tidal.playlist import TidalPlaylistManager


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
    assert playlist.owner == "Test User"
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


async def test_add_playlist_tracks_redirects_cached_stale_ids(
    playlist_manager: TidalPlaylistManager, provider_mock: Mock
) -> None:
    """
    Test a cache-known stale id is redirected to its live id before the v1 add.

    v1 silently skips dead ids (onArtifactNotFound=SKIP), so without the cache-only
    redirect a churned id would be dropped; with it, the live id is sent instead.
    """

    async def _redirect(item_id: str) -> str:
        return "track_1_live" if item_id == "track_1" else item_id

    provider_mock.redirect_cached_id = AsyncMock(side_effect=_redirect)
    provider_mock.api.get_with_etag.return_value = ({"numberOfTracks": 0}, "etag-3")

    await playlist_manager.add_tracks("1", ["track_1", "track_2"])

    assert provider_mock.api.post.call_args.kwargs["data"]["trackIds"] == "track_1_live,track_2"


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


@pytest.mark.parametrize(
    ("from_position", "to_position", "from_index", "to_index"),
    [
        (2, 4, 1, 3),  # down
        (4, 2, 3, 1),  # up
        (3, 1, 2, 0),  # to top
        (1, 5, 0, 4),  # to bottom
    ],
)
async def test_move_playlist_track(
    playlist_manager: TidalPlaylistManager,
    provider_mock: Mock,
    from_position: int,
    to_position: int,
    from_index: int,
    to_index: int,
) -> None:
    """Test move_track posts the 0-based source index and final 0-based toIndex."""
    provider_mock.api.get_with_etag.return_value = ({"numberOfTracks": 5}, "etag-m")

    await playlist_manager.move_track("1", from_position, to_position)

    provider_mock.api.get_with_etag.assert_called_once_with("playlists/1")
    provider_mock.api.post.assert_called_once_with(
        f"playlists/1/items/{from_index}",
        data={"toIndex": to_index},
        as_form=True,
        headers={"If-None-Match": "etag-m"},
    )


async def test_move_playlist_track_without_etag(
    playlist_manager: TidalPlaylistManager, provider_mock: Mock
) -> None:
    """Test move_track omits the If-None-Match header when no ETag is returned."""
    provider_mock.api.get_with_etag.return_value = ({"numberOfTracks": 3}, "")

    await playlist_manager.move_track("1", 1, 2)

    assert provider_mock.api.post.call_args.kwargs["headers"] == {}


@pytest.mark.parametrize(("from_position", "to_position"), [(0, 2), (2, 0), (4, 1), (1, 4)])
async def test_move_playlist_track_out_of_range(
    playlist_manager: TidalPlaylistManager,
    provider_mock: Mock,
    from_position: int,
    to_position: int,
) -> None:
    """Test move_track rejects positions outside the playlist without posting."""
    provider_mock.api.get_with_etag.return_value = ({"numberOfTracks": 3}, "etag-m")

    with pytest.raises(InvalidDataError):
        await playlist_manager.move_track("1", from_position, to_position)

    provider_mock.api.post.assert_not_called()


async def test_move_playlist_track_failure(
    playlist_manager: TidalPlaylistManager, provider_mock: Mock
) -> None:
    """Test move_track raises ResourceTemporarilyUnavailable on a client error."""
    provider_mock.api.get_with_etag.return_value = ({"numberOfTracks": 3}, "etag-m")
    provider_mock.api.post.side_effect = ClientError()

    with pytest.raises(ResourceTemporarilyUnavailable):
        await playlist_manager.move_track("1", 1, 2)


@pytest.mark.parametrize(
    ("positions", "to_position", "indices", "to_index"),
    [
        ([2, 3], 4, "1,2", 3),  # block down
        ([5, 1], 1, "0,4", 0),  # scattered to top, sent in playlist order
        ([4, 5], 1, "3,4", 0),  # block to top
        ([1, 2], 4, "0,1", 3),  # block to bottom
        ([1, 3], None, "0,2", 3),  # no target moves the block to the end
    ],
)
async def test_move_playlist_tracks(
    playlist_manager: TidalPlaylistManager,
    provider_mock: Mock,
    positions: list[int],
    to_position: int | None,
    indices: str,
    to_index: int,
) -> None:
    """Test move_tracks posts every 0-based index in one call with the block's final index."""
    provider_mock.api.get_with_etag.return_value = ({"numberOfTracks": 5}, "etag-m")

    await playlist_manager.move_tracks("1", positions, to_position)

    provider_mock.api.post.assert_called_once_with(
        f"playlists/1/items/{indices}",
        data={"toIndex": to_index},
        as_form=True,
        headers={"If-None-Match": "etag-m"},
    )


@pytest.mark.parametrize(
    ("positions", "to_position"),
    [([], 1), ([2, 2], 1), ([1, 2], 5), ([0, 1], 1), ([5, 6], 1)],
)
async def test_move_playlist_tracks_invalid(
    playlist_manager: TidalPlaylistManager,
    provider_mock: Mock,
    positions: list[int],
    to_position: int,
) -> None:
    """Test move_tracks rejects empty, duplicate, out-of-range or ill-fitting moves."""
    provider_mock.api.get_with_etag.return_value = ({"numberOfTracks": 5}, "etag-m")

    with pytest.raises(InvalidDataError):
        await playlist_manager.move_tracks("1", positions, to_position)

    provider_mock.api.post.assert_not_called()
