"""Tests for the playlists of the Audiobookshelf provider."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, Mock, patch

import pytest
from aioaudiobookshelf.schema.media_progress import MediaProgress
from aioaudiobookshelf.schema.playlist import PlaylistItem, PlaylistItemExpandedBook

from .abs_items import expanded_book

if TYPE_CHECKING:
    from music_assistant_models.media_items import Audiobook

    from music_assistant.providers.audiobookshelf import Audiobookshelf


async def test_playlist_items_take_progress_from_the_user(provider: Audiobookshelf) -> None:
    """The progresses of all playlist items come from the one user the server returns."""
    provider._client.get_playlist = AsyncMock(  # type: ignore[method-assign]
        return_value=Mock(
            items=[
                PlaylistItemExpandedBook(library_item_id=x, library_item=expanded_book(x))
                for x in ("book1", "book2")
            ]
        )
    )
    progress = MediaProgress(
        id_="progress1",
        library_item_id="book2",
        duration=60,
        current_time=30,
        is_finished=False,
        hide_from_continue_listening=False,
        last_update=1000,
        started_at=100,
    )
    provider._client.get_my_user = AsyncMock(return_value=Mock(media_progress=[progress]))  # type: ignore[method-assign]

    with patch.object(provider, "get_setup_value", return_value="http://abs.local"):
        items = await provider.get_playlist_tracks("playlist1")

    assert [(x.item_id, x.position, cast("Audiobook", x).resume_position_ms) for x in items] == [
        ("book1", 1, None),
        ("book2", 2, 30000),
    ]


async def test_playlist_of_a_library_not_synced_yet(provider: Audiobookshelf) -> None:
    """A socket update for a library the playlist sync hasn't seen yet adds the playlist."""
    provider.playlist_last = 0
    provider.playlist_lock = asyncio.Lock()
    provider.abs_username = "user"
    provider.mass.music.get_library_item_by_prov_id = AsyncMock(return_value=None)  # type: ignore[method-assign]
    provider.mass.music.playlists.add_item_to_library = AsyncMock()  # type: ignore[method-assign, misc]
    provider.mass.cache.set = AsyncMock()  # type: ignore[method-assign]
    abs_playlist = Mock(id_="playlist1", library_id="lib1")
    abs_playlist.name = "Playlist"

    await provider._socket_abs_playlist_changed(abs_playlist)

    assert provider.libraries.playlists_audiobooks == {"lib1": {"playlist1"}}


def _stub_playlist(provider: Audiobookshelf) -> Mock:
    provider.playlist_last = 0
    provider.playlist_lock = asyncio.Lock()
    abs_playlist = Mock(
        items=[
            PlaylistItem(library_item_id="podcast_1", episode_id=x)
            for x in ("episode_1", "episode_2", "episode_3", "episode_4")
        ]
    )
    abs_playlist.name = "Playlist"
    provider._client = client = Mock()
    client.get_playlist = AsyncMock(return_value=abs_playlist)
    client.update_playlist = AsyncMock()
    client.remove_item_from_playlist_batch = AsyncMock()
    return client


@pytest.mark.parametrize(
    ("positions", "to_position", "order"),
    [
        ([1], 4, ["episode_2", "episode_3", "episode_4", "episode_1"]),
        ([2, 4], 1, ["episode_2", "episode_4", "episode_1", "episode_3"]),
        ([1, 2], None, ["episode_3", "episode_4", "episode_1", "episode_2"]),
    ],
)
async def test_move_playlist_items(
    provider: Audiobookshelf, positions: list[int], to_position: int | None, order: list[str]
) -> None:
    """The moved items are written back to abs as the full list in their new order."""
    client = _stub_playlist(provider)

    if len(positions) == 1 and to_position is not None:
        await provider.move_playlist_track("playlist1", positions[0], to_position)
    else:
        await provider.move_playlist_tracks("playlist1", positions, to_position)

    parameters = client.update_playlist.call_args.kwargs["parameters"]
    assert parameters.name == "Playlist"
    assert [x.episode_id for x in parameters.items] == order


async def test_remove_playlist_items_by_position(provider: Audiobookshelf) -> None:
    """Positions start at 1, like the ones the playlist listing hands out."""
    client = _stub_playlist(provider)

    await provider.remove_playlist_tracks("playlist1", (1, 3))

    items = client.remove_item_from_playlist_batch.call_args.kwargs["items"]
    assert [x.episode_id for x in items] == ["episode_1", "episode_3"]
