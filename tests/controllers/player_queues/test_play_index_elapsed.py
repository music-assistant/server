"""Tests that ``play_index`` resets the queue's elapsed clock with the item switch."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, Mock

from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData

QUEUE_ID = "q1"
STALE_ELAPSED = 43_217.7


def _controller_with_stale_queue() -> tuple[
    PlayerQueuesController, PlayerQueue, list[tuple[str | None, float]]
]:
    """Build a bare controller mid-way through item "old", recording signal_update states."""
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    queue = PlayerQueue(queue_id=QUEUE_ID, active=True, display_name="Q1", available=True, items=2)
    items = [
        QueueItem(queue_id=QUEUE_ID, queue_item_id="old", name="old", duration=50_000),
        QueueItem(queue_id=QUEUE_ID, queue_item_id="new", name="new", duration=279),
    ]
    queue.current_index = 0
    queue.current_item = items[0]
    queue.elapsed_time = STALE_ELAPSED
    queue.elapsed_time_last_updated = time.time() - 900
    queue_data = PlayerQueueData(queue=queue)
    queue_data.items = items
    ctrl._queue_data = {QUEUE_ID: queue_data}

    signals: list[tuple[str | None, float]] = []

    def _record_signal(_queue_id: str, items_changed: bool = False) -> None:  # noqa: ARG001
        item = queue.current_item
        signals.append((item.queue_item_id if item else None, queue.elapsed_time))

    ctrl.signal_update = Mock(side_effect=_record_signal)  # type: ignore[method-assign]
    ctrl._check_player_permission = Mock()  # type: ignore[method-assign]
    ctrl._set_transitioning = Mock()  # type: ignore[method-assign]
    ctrl._load_item = AsyncMock()  # type: ignore[method-assign]
    ctrl._get_next_index = Mock(return_value=None)  # type: ignore[method-assign]
    ctrl.player_media_from_queue_item = AsyncMock()  # type: ignore[method-assign]
    ctrl.mass = MagicMock()
    ctrl.mass.players.play_media = AsyncMock()
    ctrl.logger = MagicMock()  # the retry-fallback path logs a warning
    return ctrl, queue, signals


async def test_play_index_resets_elapsed_with_item_switch() -> None:
    """Starting a new item zeroes elapsed_time and freshens its timestamp."""
    ctrl, queue, signals = _controller_with_stale_queue()
    before = time.time()

    await ctrl.play_index(QUEUE_ID, 1)

    assert queue.current_item is not None
    assert queue.current_item.queue_item_id == "new"
    assert queue.elapsed_time == 0
    assert queue.elapsed_time_last_updated >= before
    # no signaled state may pair the new item with the previous item's elapsed
    assert all(elapsed == 0 for item_id, elapsed in signals if item_id == "new"), signals


async def test_play_index_sets_elapsed_to_seek_position() -> None:
    """A seeked start (resume, audiobook resume point) anchors elapsed at the seek."""
    ctrl, queue, signals = _controller_with_stale_queue()

    await ctrl.play_index(QUEUE_ID, 1, seek_position=30)

    assert queue.elapsed_time == 30
    assert all(elapsed == 30 for item_id, elapsed in signals if item_id == "new"), signals


async def test_play_index_retry_fallback_discards_failed_items_seek_position() -> None:
    """Falling back to another item after a load failure zeroes elapsed_time, not seek_position."""
    ctrl, queue, signals = _controller_with_stale_queue()
    fallback_item = QueueItem(
        queue_id=QUEUE_ID, queue_item_id="fallback", name="fallback", duration=200
    )
    ctrl._queue_data[QUEUE_ID].items.append(fallback_item)
    ctrl._load_item = AsyncMock(side_effect=[MediaNotFoundError("unplayable"), None])  # type: ignore[method-assign]
    ctrl._get_next_index = Mock(return_value=2)  # type: ignore[method-assign]

    await ctrl.play_index(QUEUE_ID, 1, seek_position=45)

    assert queue.current_item is not None
    assert queue.current_item.queue_item_id == "fallback"
    assert queue.elapsed_time == 0
    assert all(elapsed == 0 for item_id, elapsed in signals if item_id == "fallback"), signals
