"""
Tests for a queue whose current item is no longer among its items.

The playback tracker can move the queue's position into a region of the queue that a concurrent
load replaces right after, which leaves the current item pointing at a track the queue no longer
holds. Pressing play then failed on that missing track every time, even though the queue was full.
The position follows the items instead, and resuming never trusts a current item that is gone.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, Mock

from music_assistant_models.enums import PlaybackState
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData

QUEUE_ID = "q1"
CURRENT_INDEX = 10


def _item(name: str) -> QueueItem:
    """Build a queue item named after its id."""
    return QueueItem(queue_id=QUEUE_ID, queue_item_id=name, name=name, duration=100)


def _controller() -> tuple[PlayerQueuesController, PlayerQueue]:
    """Build a bare idle controller holding a 12 item queue parked part-way into its item 10."""
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    queue = PlayerQueue(queue_id=QUEUE_ID, active=True, display_name="Q1", available=True, items=0)
    items = [_item(f"old{index}") for index in range(12)]
    queue.items = len(items)
    queue.state = PlaybackState.IDLE
    queue.current_index = CURRENT_INDEX
    queue.index_in_buffer = CURRENT_INDEX
    queue.current_item = items[CURRENT_INDEX]
    queue.next_item = items[CURRENT_INDEX + 1]
    queue.elapsed_time = 42.0
    queue.resume_pos = 42
    queue_data = PlayerQueueData(queue=queue)
    queue_data.items = items
    ctrl._queue_data = {QUEUE_ID: queue_data}
    ctrl.signal_update = Mock()  # type: ignore[method-assign]
    ctrl._check_player_permission = Mock()  # type: ignore[method-assign]
    ctrl.play_index = AsyncMock()  # type: ignore[method-assign]
    ctrl.mass = MagicMock()
    ctrl.mass.players.get_player = Mock(
        return_value=MagicMock(state=MagicMock(playback_state=PlaybackState.IDLE))
    )
    ctrl.logger = MagicMock()
    return ctrl, queue


def _replace_tail(ctrl: PlayerQueuesController, new_count: int = 12) -> list[QueueItem]:
    """Replace everything after the first three items with new ones, as a concurrent load does."""
    kept = ctrl._queue_data[QUEUE_ID].items[:3]
    items = [*kept, *(_item(f"new{index}") for index in range(3, 3 + new_count))]
    ctrl.update_items(QUEUE_ID, items)
    return items


def _played(ctrl: PlayerQueuesController) -> tuple[str, str, int]:
    """Return the queue, item id and seek position the resume started playback with."""
    ctrl.play_index.assert_awaited_once()  # type: ignore[attr-defined]
    queue_id, item_id, seek_pos = ctrl.play_index.await_args.args[:3]  # type: ignore[attr-defined]
    return queue_id, item_id, seek_pos


async def test_a_replaced_current_item_is_swapped_for_the_item_now_at_its_index() -> None:
    """The queue moves onto the track that now sits at its index, and play starts there."""
    ctrl, queue = _controller()

    items = _replace_tail(ctrl)

    assert queue.current_index == CURRENT_INDEX
    assert queue.current_item is items[CURRENT_INDEX]
    assert queue.next_item is items[CURRENT_INDEX + 1]
    # a different track starts from its beginning, not from where the replaced one was
    assert queue.elapsed_time == 0
    assert queue.resume_pos == 0

    await ctrl.resume(QUEUE_ID)

    assert _played(ctrl) == (QUEUE_ID, items[CURRENT_INDEX].queue_item_id, 0)


def test_the_index_is_clamped_when_the_replacement_is_shorter() -> None:
    """A replacement with fewer items parks the queue on its new last item."""
    ctrl, queue = _controller()

    items = _replace_tail(ctrl, new_count=2)

    assert queue.current_index == len(items) - 1
    assert queue.current_item is items[-1]
    assert queue.next_item is None


def test_a_change_that_keeps_the_current_item_leaves_the_position_alone() -> None:
    """Only a vanished current item moves the queue; items added around it change nothing."""
    ctrl, queue = _controller()
    items = ctrl._queue_data[QUEUE_ID].items

    ctrl.update_items(QUEUE_ID, [*items, _item("added")])

    assert queue.current_index == CURRENT_INDEX
    assert queue.current_item is items[CURRENT_INDEX]
    assert queue.elapsed_time == 42.0
    assert queue.resume_pos == 42


def test_a_replace_in_flight_leaves_the_position_to_its_caller() -> None:
    """A replace drops the buffered index while it swaps the items and sets the position itself."""
    ctrl, queue = _controller()
    queue.state = PlaybackState.PLAYING
    queue.index_in_buffer = None
    old_current = queue.current_item

    _replace_tail(ctrl)

    assert queue.current_index == CURRENT_INDEX
    assert queue.current_item is old_current
    assert queue.elapsed_time == 42.0


async def test_resume_falls_back_to_the_index_when_the_current_item_is_gone() -> None:
    """A current item that is not in the queue is not trusted: play starts at the index, from 0."""
    ctrl, queue = _controller()
    queue.current_item = _item("gone")

    await ctrl.resume(QUEUE_ID)

    assert _played(ctrl) == (QUEUE_ID, "old10", 0)


async def test_resume_starts_over_when_the_position_lies_beyond_the_items() -> None:
    """A stale item at an index past the end of the queue starts the queue from its first item."""
    ctrl, queue = _controller()
    queue.index_in_buffer = None
    queue.current_index = 20
    queue.current_item = _item("gone")

    await ctrl.resume(QUEUE_ID)

    assert _played(ctrl) == (QUEUE_ID, "old0", 0)
