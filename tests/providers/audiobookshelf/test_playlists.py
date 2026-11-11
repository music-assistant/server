"""Tests for the playlists of the Audiobookshelf provider."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, Mock, patch

from aioaudiobookshelf.schema.media_progress import MediaProgress
from aioaudiobookshelf.schema.playlist import PlaylistItemExpandedBook

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

    assert [(x.item_id, cast("Audiobook", x).resume_position_ms) for x in items] == [
        ("book1", None),
        ("book2", 30000),
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
