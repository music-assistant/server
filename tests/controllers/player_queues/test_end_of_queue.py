"""
Tests for what a queue looks like once it played to its end.

Reaching the end rewinds the queue to its start instead of emptying it, so pressing play
again replays it from the beginning. A queue that holds no items at all has nothing to
resume and must report that instead of silently starting something else.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from music_assistant_models.enums import PlaybackState
from music_assistant_models.errors import QueueEmpty
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData

QUEUE_ID = "q1"


def _controller(*, with_items: bool = True) -> tuple[PlayerQueuesController, PlayerQueue]:
    """Build a bare controller holding a queue that just finished its last item."""
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    queue = PlayerQueue(queue_id=QUEUE_ID, active=True, display_name="Q1", available=True, items=0)
    items = (
        [
            QueueItem(queue_id=QUEUE_ID, queue_item_id="first", name="first", duration=100),
            QueueItem(queue_id=QUEUE_ID, queue_item_id="last", name="last", duration=100),
        ]
        if with_items
        else []
    )
    queue.items = len(items)
    queue.state = PlaybackState.IDLE
    if items:
        queue.current_index = 1
        queue.current_item = items[1]
        queue.elapsed_time = 99.0
        queue.elapsed_time_last_updated = time.time() - 900
        queue.resume_pos = 99
        queue.index_in_buffer = 1
    queue_data = PlayerQueueData(queue=queue)
    queue_data.items = items
    ctrl._queue_data = {QUEUE_ID: queue_data}
    ctrl.signal_update = Mock()  # type: ignore[method-assign]
    ctrl.on_player_update = Mock()  # type: ignore[method-assign]
    ctrl._check_player_permission = Mock()  # type: ignore[method-assign]
    ctrl.play_index = AsyncMock()  # type: ignore[method-assign]
    ctrl._managed_pool = MagicMock()
    ctrl.mass = MagicMock()
    ctrl.mass.create_task = Mock(side_effect=lambda coro, **_kw: coro.close())
    ctrl.mass.players.get_player = Mock(
        return_value=MagicMock(state=MagicMock(playback_state=PlaybackState.IDLE))
    )
    ctrl.logger = MagicMock()
    return ctrl, queue


def test_reset_playback_position_keeps_items() -> None:
    """A rewind clears the playback position but leaves the items in place."""
    ctrl, queue = _controller()

    ctrl.reset_playback_position(QUEUE_ID)

    assert queue.current_index is None
    assert queue.current_item is None
    assert queue.next_item is None
    assert queue.elapsed_time == 0
    assert queue.index_in_buffer is None
    assert queue.resume_pos == 0
    # the items themselves survive so the queue can be replayed
    assert queue.items == 2
    assert [x.queue_item_id for x in ctrl._queue_data[QUEUE_ID].items] == ["first", "last"]


def test_clear_still_empties_the_queue() -> None:
    """An explicit clear keeps emptying the queue (it shares the position reset)."""
    ctrl, queue = _controller()

    ctrl.clear(QUEUE_ID)

    assert queue.items == 0
    assert ctrl._queue_data[QUEUE_ID].items == []
    assert queue.current_index is None
    assert queue.current_item is None


async def test_resume_after_end_of_queue_restarts_from_the_beginning() -> None:
    """Pressing play on a queue that reached its end replays it from the first item."""
    ctrl, _queue = _controller()
    ctrl.reset_playback_position(QUEUE_ID)

    await ctrl.resume(QUEUE_ID)

    ctrl.play_index.assert_awaited_once()  # type: ignore[attr-defined]
    queue_id, item_id, seek_pos = ctrl.play_index.await_args.args[:3]  # type: ignore[attr-defined]
    assert queue_id == QUEUE_ID
    assert item_id == "first"
    assert seek_pos == 0


async def test_resume_on_empty_queue_raises() -> None:
    """A queue with no items has nothing to resume and says so."""
    ctrl, _queue = _controller(with_items=False)

    with pytest.raises(QueueEmpty):
        await ctrl.resume(QUEUE_ID)

    ctrl.play_index.assert_not_awaited()  # type: ignore[attr-defined]
