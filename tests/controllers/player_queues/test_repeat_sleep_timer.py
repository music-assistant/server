"""Tests that the queue does not repeat an item a sleep timer stops playback at."""

from __future__ import annotations

from unittest.mock import MagicMock, Mock

import pytest
from music_assistant_models.enums import PlaybackState, RepeatMode
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData


def _controller(
    repeat_mode: RepeatMode, item_ids: list[str], timer_ends_item: bool
) -> PlayerQueuesController:
    """Build a bare controller holding queue "q1" with the given items."""
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
    return ctrl


@pytest.mark.parametrize(
    ("repeat_mode", "item_ids", "cur_index", "timer_ends_item", "is_skip", "expected"),
    [
        (RepeatMode.ONE, ["item_0", "item_1", "item_2"], 1, False, False, 1),
        # the queue moves on instead, and the armed stop ends playback
        (RepeatMode.ONE, ["item_0", "item_1", "item_2"], 1, True, False, 2),
        (RepeatMode.ONE, ["item_0", "item_1", "item_2"], 2, True, False, None),
        (RepeatMode.ALL, ["item_0"], 0, False, False, 0),
        (RepeatMode.ALL, ["item_0"], 0, True, False, None),
        # a user skip still starts the item over
        (RepeatMode.ALL, ["item_0"], 0, True, True, 0),
    ],
)
def test_next_index_does_not_repeat_the_item_a_sleep_timer_ends(
    repeat_mode: RepeatMode,
    item_ids: list[str],
    cur_index: int,
    timer_ends_item: bool,
    is_skip: bool,
    expected: int | None,
) -> None:
    """A repeat would start the item over before the sleep timer could stop it."""
    ctrl = _controller(repeat_mode, item_ids, timer_ends_item)

    assert ctrl._get_next_index("q1", cur_index, is_skip=is_skip) == expected


@pytest.mark.parametrize(("timer_ends_item", "handed_over"), [(True, "item_2"), (False, None)])
def test_player_holding_the_repeat_gets_the_item_that_follows_now(
    timer_ends_item: bool, handed_over: str | None
) -> None:
    """A repeat the player already holds is replaced once a sleep timer ends that item."""
    ctrl = _controller(RepeatMode.ONE, ["item_0", "item_1", "item_2"], timer_ends_item)
    queue_data = ctrl._queue_data["q1"]
    queue_data.queue.state = PlaybackState.PLAYING
    queue_data.queue.current_index = 1
    queue_data.queue.index_in_buffer = 1
    queue_data.next_item_id_enqueued = "item_1"
    ctrl._enqueue_next_item = Mock()  # type: ignore[method-assign]

    ctrl.update_next_item_on_player("q1")

    enqueued = [call.args[1].queue_item_id for call in ctrl._enqueue_next_item.call_args_list]
    assert enqueued == ([handed_over] if handed_over else [])
