"""Tests that the queue does not repeat an item a sleep timer stops playback at."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import RepeatMode
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData


@pytest.mark.parametrize(
    ("repeat_mode", "item_ids", "cur_index", "timer_ends_item", "expected"),
    [
        (RepeatMode.ONE, ["item_0", "item_1", "item_2"], 1, False, 1),
        # the queue moves on instead, and the armed stop ends playback
        (RepeatMode.ONE, ["item_0", "item_1", "item_2"], 1, True, 2),
        (RepeatMode.ONE, ["item_0", "item_1", "item_2"], 2, True, None),
        (RepeatMode.ALL, ["item_0"], 0, False, 0),
        (RepeatMode.ALL, ["item_0"], 0, True, None),
    ],
)
def test_next_index_does_not_repeat_the_item_a_sleep_timer_ends(
    repeat_mode: RepeatMode,
    item_ids: list[str],
    cur_index: int,
    timer_ends_item: bool,
    expected: int | None,
) -> None:
    """A repeat would start the item over before the sleep timer could stop it."""
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    ctrl.mass = MagicMock()
    ctrl.mass.players.sleep_timer_ends_item.return_value = timer_ends_item
    queue = PlayerQueue(
        queue_id="q1", active=True, display_name="Q1", available=True, items=len(item_ids)
    )
    queue.repeat_mode = repeat_mode
    queue_data = PlayerQueueData(queue=queue)
    queue_data.items = [
        QueueItem(queue_id="q1", queue_item_id=item_id, name=item_id, duration=60)
        for item_id in item_ids
    ]
    ctrl._queue_data = {"q1": queue_data}

    assert ctrl._get_next_index("q1", cur_index) == expected
