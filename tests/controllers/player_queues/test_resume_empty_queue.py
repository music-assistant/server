"""
Tests for resuming a queue that holds no items.

An empty queue has nothing to resume and must report that, rather than reaching into the
playlog for whatever was played most recently elsewhere in the system.
"""

from __future__ import annotations

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
    """Build a bare controller owning a single idle queue."""
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    queue = PlayerQueue(queue_id=QUEUE_ID, active=True, display_name="Q1", available=True, items=0)
    items = (
        [QueueItem(queue_id=QUEUE_ID, queue_item_id="first", name="first", duration=100)]
        if with_items
        else []
    )
    queue.items = len(items)
    queue.state = PlaybackState.IDLE
    queue_data = PlayerQueueData(queue=queue)
    queue_data.items = items
    ctrl._queue_data = {QUEUE_ID: queue_data}
    ctrl.signal_update = Mock()  # type: ignore[method-assign]
    ctrl.on_player_update = Mock()  # type: ignore[method-assign]
    ctrl._check_player_permission = Mock()  # type: ignore[method-assign]
    ctrl.play_index = AsyncMock()  # type: ignore[method-assign]
    ctrl.mass = MagicMock()
    ctrl.mass.players.get_player = Mock(
        return_value=MagicMock(state=MagicMock(playback_state=PlaybackState.IDLE))
    )
    ctrl.logger = MagicMock()
    return ctrl, queue


async def test_resume_on_empty_queue_raises() -> None:
    """A queue with no items has nothing to resume and says so."""
    ctrl, _queue = _controller(with_items=False)

    with pytest.raises(QueueEmpty):
        await ctrl.resume(QUEUE_ID)

    ctrl.play_index.assert_not_awaited()  # type: ignore[attr-defined]


async def test_resume_with_items_but_no_position_starts_at_the_first_item() -> None:
    """A loaded queue that never started plays from its first item."""
    ctrl, _queue = _controller()

    await ctrl.resume(QUEUE_ID)

    ctrl.play_index.assert_awaited_once()  # type: ignore[attr-defined]
    queue_id, item_id = ctrl.play_index.await_args.args[:2]  # type: ignore[attr-defined]
    assert queue_id == QUEUE_ID
    assert item_id == "first"
