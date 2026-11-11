"""Tests for what a new queue load drops of the previous one's fetch record."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock

from music_assistant_models.enums import MediaType, PlaybackState
from music_assistant_models.media_items import ItemMapping, ProviderMapping, Track
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem
from music_assistant_models.unique_list import UniqueList

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData

TRACKS = ["t0", "t1", "t2", "t3", "t4"]


def _track(item_id: str) -> Track:
    """Build a playable Track on the 'test' provider."""
    return Track(
        item_id=item_id,
        provider="test",
        name=f"Track {item_id}",
        duration=60,
        artists=UniqueList(
            [ItemMapping(item_id="a", provider="test", name="A", media_type=MediaType.ARTIST)]
        ),
        provider_mappings={
            ProviderMapping(item_id=item_id, provider_domain="test", provider_instance="test")
        },
    )


def _controller(current_index: int = 0) -> Any:
    """Build a bare controller holding queue "q1" loaded with TRACKS at the given position."""
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    ctrl.logger = Mock()
    ctrl.mass = MagicMock()
    ctrl.mass.players.get_player.return_value.offline_by_power_control = False
    ctrl.signal_update = Mock()  # type: ignore[method-assign]
    ctrl._enqueue_next_item = Mock()  # type: ignore[method-assign]
    queue = PlayerQueue(queue_id="q1", active=True, display_name="Q1", available=True, items=0)
    ctrl._queue_data = {"q1": PlayerQueueData(queue=queue)}
    items = [QueueItem.from_media_item("q1", _track(item_id)) for item_id in TRACKS]
    ctrl._queue_data["q1"].items = items
    queue.items = len(items)
    queue.state = PlaybackState.PLAYING
    queue.current_index = current_index
    queue.current_item = items[current_index]
    queue.index_in_buffer = current_index
    return ctrl


async def test_a_new_load_drops_what_the_old_one_fetched() -> None:
    """The item the player last fetched does not count across a restart."""
    ctrl = _controller(current_index=1)
    data = ctrl._queue_data["q1"]
    data.last_served_item_id = data.items[2].queue_item_id
    ctrl._preload_next_item = Mock()
    ctrl._cleanup_stale_queue_buffers = Mock()
    ctrl._load_item = AsyncMock()
    ctrl._check_player_permission = Mock()
    ctrl._set_transitioning = Mock()
    ctrl.player_media_from_queue_item = AsyncMock()
    ctrl.mark_ended = Mock()
    ctrl.mass.streams.is_smart_fades_active.return_value = False
    ctrl.mass.players.play_media = AsyncMock()

    await ctrl.play_index("q1", 0)

    assert data.last_served_item_id is None
