"""Tests for adding a queue's items to a playlist, new or existing."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import InvalidDataError, QueueEmpty

from music_assistant.controllers.player_queues import PlayerQueuesController

QUEUE_ID = "kitchen"
PLAYLIST_ID = "42"


class _Playlists:
    """Stand-in for the playlists controller, recording what the queue hands it."""

    def __init__(self) -> None:
        self.task = MagicMock(name="task")
        self.create_playlist = AsyncMock(return_value=SimpleNamespace(item_id=PLAYLIST_ID))
        self.add_playlist_tracks = AsyncMock(return_value=self.task)


def _item(queue_item_id: str, media_type: MediaType = MediaType.TRACK) -> SimpleNamespace:
    """Build a stand-in queue item carrying just what the playlist export reads."""
    uri = f"library://{media_type.value}/{queue_item_id}"
    return SimpleNamespace(queue_item_id=queue_item_id, media_type=media_type, uri=uri)


def _controller(*items: SimpleNamespace) -> tuple[PlayerQueuesController, _Playlists]:
    """Build a bare queue controller holding one queue with the given items."""
    playlists = _Playlists()
    controller = PlayerQueuesController.__new__(PlayerQueuesController)
    controller.mass = SimpleNamespace(music=SimpleNamespace(playlists=playlists))  # type: ignore[assignment]
    controller._queue_data = {QUEUE_ID: SimpleNamespace(items=list(items))}  # type: ignore[dict-item]
    controller._check_player_permission = MagicMock()  # type: ignore[method-assign]
    controller.get = MagicMock(return_value=SimpleNamespace(queue_id=QUEUE_ID))  # type: ignore[method-assign]
    return controller, playlists


async def test_the_whole_queue_is_added_to_an_existing_playlist() -> None:
    """Every playlist-compatible item goes to the playlist, in queue order."""
    controller, playlists = _controller(_item("a"), _item("b", MediaType.RADIO), _item("c"))

    task = await controller.add_to_playlist(QUEUE_ID, PLAYLIST_ID)

    assert task is playlists.task
    playlists.add_playlist_tracks.assert_awaited_once_with(
        PLAYLIST_ID, ["library://track/a", "library://radio/b", "library://track/c"]
    )
    playlists.create_playlist.assert_not_awaited()


async def test_saving_as_a_new_playlist_creates_it_first() -> None:
    """Saving creates the named playlist and then fills it with the queue."""
    controller, playlists = _controller(_item("a"))

    await controller.save_as_playlist(QUEUE_ID, "Tonight")

    playlists.create_playlist.assert_awaited_once_with("Tonight")
    playlists.add_playlist_tracks.assert_awaited_once_with(PLAYLIST_ID, ["library://track/a"])


async def test_an_empty_queue_is_refused() -> None:
    """Nothing to add is an error rather than an empty background task."""
    controller, _ = _controller()

    with pytest.raises(QueueEmpty):
        await controller.add_to_playlist(QUEUE_ID, PLAYLIST_ID)


async def test_a_queue_without_playlist_compatible_items_is_refused() -> None:
    """Items a playlist cannot hold are skipped, and none left is an error."""
    controller, _ = _controller(_item("ad", MediaType.FLOW_STREAM))

    with pytest.raises(InvalidDataError):
        await controller.add_to_playlist(QUEUE_ID, PLAYLIST_ID)
