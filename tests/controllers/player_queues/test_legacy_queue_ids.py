"""Tests for finding a queue by a legacy id of its player."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from music_assistant_models.enums import PlaybackState
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData

QUEUE_ID = "kitchen"
LEGACY_ID = "old_kitchen"
ITEM_ID = "item_1"


def _controller() -> tuple[PlayerQueuesController, PlayerQueueData]:
    """Return a bare queue controller holding the queue of a player with a legacy id."""
    legacy_ids = {LEGACY_ID: QUEUE_ID}
    # the player is paused on its own queue, which has no stream session anymore
    player = SimpleNamespace(playback_state=PlaybackState.PAUSED, active_source=QUEUE_ID)
    controller = PlayerQueuesController.__new__(PlayerQueuesController)
    controller.mass = SimpleNamespace(  # type: ignore[assignment]
        players=SimpleNamespace(
            resolve_player_id=lambda player_id: legacy_ids.get(player_id, player_id),
            get_player=lambda player_id: player if player_id == QUEUE_ID else None,
        )
    )
    queue_data = PlayerQueueData(
        queue=PlayerQueue(
            queue_id=QUEUE_ID, active=False, display_name="Kitchen", available=True, items=1
        ),
        items=[QueueItem(queue_id=QUEUE_ID, queue_item_id=ITEM_ID, name="Song", duration=180)],
    )
    controller._queue_data = {QUEUE_ID: queue_data}
    return controller, queue_data


@pytest.mark.parametrize("queue_id", [QUEUE_ID, LEGACY_ID])
def test_queue_is_found_by_its_id_and_by_a_legacy_id(queue_id: str) -> None:
    """Every lookup of a queue takes the id of the queue or a legacy id of its player."""
    controller, queue_data = _controller()

    assert controller.get(queue_id) is queue_data.queue
    assert controller.queue_data(queue_id) is queue_data
    assert controller.queue_data_or_none(queue_id) is queue_data
    assert controller.items(queue_id) == queue_data.items
    assert controller.get_item(queue_id, ITEM_ID) is queue_data.items[0]
    assert controller.get_item(queue_id, 0) is queue_data.items[0]
    assert controller.index_by_id(queue_id, ITEM_ID) == 0
    assert controller.has_lost_paused_stream(queue_id)


def test_unknown_queue_is_not_found() -> None:
    """An id that no queue answers to finds nothing."""
    controller, _ = _controller()

    assert controller.get("garage") is None
    assert controller.queue_data_or_none("garage") is None
    assert controller.items("garage") == []
    assert controller.get_item("garage", ITEM_ID) is None
    assert controller.index_by_id("garage", ITEM_ID) is None
    assert not controller.has_lost_paused_stream("garage")
    with pytest.raises(KeyError, match="garage"):
        controller.queue_data("garage")
