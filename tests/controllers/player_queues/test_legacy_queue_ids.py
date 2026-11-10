"""Tests for finding a queue by a legacy id of its player."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from music_assistant_models.player_queue import PlayerQueue

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData

QUEUE_ID = "kitchen"
LEGACY_ID = "old_kitchen"


def _controller() -> tuple[PlayerQueuesController, PlayerQueueData]:
    """Return a bare queue controller holding the queue of a player with a legacy id."""
    legacy_ids = {LEGACY_ID: QUEUE_ID}
    controller = PlayerQueuesController.__new__(PlayerQueuesController)
    controller.mass = SimpleNamespace(  # type: ignore[assignment]
        players=SimpleNamespace(
            resolve_player_id=lambda player_id: legacy_ids.get(player_id, player_id)
        )
    )
    queue_data = PlayerQueueData(
        queue=PlayerQueue(
            queue_id=QUEUE_ID, active=False, display_name="Kitchen", available=True, items=0
        )
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


def test_unknown_queue_is_not_found() -> None:
    """An id that no queue answers to finds nothing."""
    controller, _ = _controller()

    assert controller.get("garage") is None
    assert controller.queue_data_or_none("garage") is None
    with pytest.raises(KeyError, match="garage"):
        controller.queue_data("garage")
