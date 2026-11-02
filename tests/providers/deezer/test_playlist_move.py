"""Test that a track can be moved within a Deezer playlist."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from music_assistant_models.errors import InvalidDataError

from music_assistant.providers.deezer.media import DeezerMediaManager
from music_assistant.providers.deezer.provider import DeezerProvider


@pytest.fixture
def gql(provider: DeezerProvider) -> AsyncMock:
    """Stub the GraphQL client and serve a playlist whose second track was removed."""
    client = AsyncMock()
    client.move_track_in_playlist.return_value = SimpleNamespace(move_status=True)
    provider.gql_client = client
    provider.media_manager = DeezerMediaManager(provider)
    provider.browse_manager.get_playlist_tracks = AsyncMock(  # type: ignore[method-assign]
        return_value=[
            SimpleNamespace(item_id=str(position), position=position) for position in (1, 3, 4, 5)
        ]
    )
    return client


@pytest.fixture
def invalidate(provider: DeezerProvider) -> AsyncMock:
    """Record the playlist cache invalidation."""
    invalidate = AsyncMock()
    provider.browse_manager.invalidate_playlist_cache = invalidate  # type: ignore[method-assign]
    return invalidate


@pytest.mark.parametrize(
    ("from_position", "to_position", "track_id", "after_track_id"),
    [(1, 4, "1", "4"), (5, 3, "5", "1"), (4, 1, "4", None)],
)
async def test_track_is_moved_behind_its_new_predecessor(
    provider: DeezerProvider,
    gql: AsyncMock,
    invalidate: AsyncMock,
    from_position: int,
    to_position: int,
    track_id: str,
    after_track_id: str | None,
) -> None:
    """Down, up and to the top, with positions as the playlist numbers them."""
    await provider.move_playlist_track("123", from_position, to_position)

    gql.move_track_in_playlist.assert_awaited_once_with(
        playlist_id="123", track_id=track_id, after_track_id=after_track_id
    )
    invalidate.assert_awaited_once_with("123")


async def test_position_without_track_is_refused(provider: DeezerProvider, gql: AsyncMock) -> None:
    """A position no track holds raises before anything is sent."""
    with pytest.raises(InvalidDataError):
        await provider.move_playlist_track("123", 2, 1)

    gql.move_track_in_playlist.assert_not_called()


async def test_move_deezer_did_not_do_raises(
    provider: DeezerProvider, gql: AsyncMock, invalidate: AsyncMock
) -> None:
    """Deezer reports a failed move only through moveStatus, without an error."""
    gql.move_track_in_playlist.return_value = SimpleNamespace(move_status=False)

    with pytest.raises(InvalidDataError):
        await provider.move_playlist_track("123", 1, 3)

    invalidate.assert_not_called()
