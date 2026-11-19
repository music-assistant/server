"""Tests for the queue position a player's reported stream URL sets."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock

from music_assistant_models.enums import PlaybackState
from music_assistant_models.player import PlayerMedia
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData
from music_assistant.models.player import Player

BASE_URL = "http://ma.local:8097"


def _controller() -> tuple[PlayerQueuesController, PlayerQueue]:
    """Create a controller whose queue q1 just moved to its second item in session 'new'."""
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    ctrl.mass = MagicMock()
    ctrl.mass.streams.base_url = BASE_URL
    items = [
        QueueItem(queue_id="q1", queue_item_id=f"item{i}", name=f"x{i}", duration=60)
        for i in range(3)
    ]
    queue = PlayerQueue(
        queue_id="q1",
        active=True,
        display_name="q1",
        available=True,
        items=len(items),
        state=PlaybackState.PLAYING,
        current_index=1,
        current_item=items[1],
    )
    ctrl._queue_data = {"q1": PlayerQueueData(queue=queue, items=items, session_id="new")}
    return ctrl, queue


def _player(uri: str) -> Player:
    """Build a player whose rendering protocol reports the given stream URL."""
    protocol = SimpleNamespace(current_media=PlayerMedia(uri=uri))
    player = SimpleNamespace(resolve_output_protocol=lambda: protocol, corrected_elapsed_time=21.0)
    return cast("Player", player)


def test_stream_url_of_a_superseded_session_keeps_the_queue_position() -> None:
    """A player still reporting the stream it played before Next does not pull the queue back."""
    ctrl, queue = _controller()

    updated = ctrl._update_current_index_from_player(
        queue, _player(f"{BASE_URL}/single/old/q1/item0/q1.flac")
    )

    assert updated is False
    assert queue.current_index == 1


def test_stream_url_of_the_current_session_sets_the_queue_position() -> None:
    """The stream the player reports in the current session sets the current item."""
    ctrl, queue = _controller()

    updated = ctrl._update_current_index_from_player(
        queue, _player(f"{BASE_URL}/single/new/q1/item2/q1.flac")
    )

    assert updated is True
    assert queue.current_index == 2
    assert queue.elapsed_time == 21.0
