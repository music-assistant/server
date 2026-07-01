"""
Tests for the enqueue-option handling in the player-queues controller.

These exercise ``QueueLoaderMixin._enqueue_with_option`` against a bare controller
instance, mirroring ``test_user_initiated_plays``. The real ``load`` and
``update_items`` run so the insert position is verified end-to-end; only the
side-effecting ``play_index`` and ``signal_update`` are stubbed out.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

from music_assistant_models.enums import PlaybackState, QueueOption
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData


def _controller() -> PlayerQueuesController:
    """Create a bare controller instance with the noisy ``signal_update`` stubbed out."""
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    ctrl.signal_update = Mock()  # type: ignore[method-assign]
    return ctrl


def _items(queue_id: str, names: list[str]) -> list[QueueItem]:
    """Build a list of simple queue items with the given names."""
    return [
        QueueItem(queue_id=queue_id, queue_item_id=name, name=name, duration=60) for name in names
    ]


async def test_play_on_idle_queue_starts_at_first_item() -> None:
    """PLAY with multiple items onto an idle/empty queue plays the first item, not the second."""
    ctrl = _controller()
    play_index = AsyncMock()
    ctrl.play_index = play_index  # type: ignore[method-assign]
    queue = PlayerQueue(queue_id="q1", active=True, display_name="Q1", available=True, items=0)
    ctrl._queue_data = {"q1": PlayerQueueData(queue=queue)}
    items = _items("q1", ["a", "b", "c"])

    await ctrl._enqueue_with_option("q1", items, QueueOption.PLAY, managed_pool=False)

    # the items are loaded from the very start and playback begins at the first one
    assert ctrl._queue_data["q1"].items[0] is items[0]
    assert len(ctrl._queue_data["q1"].items) == 3
    play_index.assert_awaited_once_with("q1", 0)


async def test_play_on_active_queue_inserts_after_current() -> None:
    """PLAY on an active queue keeps inserting right after the current index and jumps to it."""
    ctrl = _controller()
    play_index = AsyncMock()
    ctrl.play_index = play_index  # type: ignore[method-assign]
    ctrl.get_next_item = Mock(return_value=None)  # type: ignore[method-assign]
    existing = _items("q1", ["e0", "e1", "e2"])
    queue = PlayerQueue(
        queue_id="q1",
        active=True,
        display_name="Q1",
        available=True,
        items=len(existing),
        state=PlaybackState.PLAYING,
        current_index=2,
        index_in_buffer=2,
    )
    ctrl._queue_data = {"q1": PlayerQueueData(queue=queue, items=list(existing))}
    new_items = _items("q1", ["n0", "n1"])

    await ctrl._enqueue_with_option("q1", new_items, QueueOption.PLAY, managed_pool=False)

    # inserted right after the current/buffered index (2) and playback jumps there
    assert ctrl._queue_data["q1"].items[3] is new_items[0]
    play_index.assert_awaited_once_with("q1", 3)


async def test_next_shuffle_pins_first_item_and_shuffles_rest() -> None:
    """NEXT with shuffle on pins the first new item after the buffered index, shuffles the rest."""
    ctrl = _controller()
    ctrl.play_index = AsyncMock()  # type: ignore[method-assign]
    ctrl.get_next_item = Mock(return_value=None)  # type: ignore[method-assign]
    # keep the shuffle deterministic-enough: pure random of the "rest", first item stays pinned
    ctrl._smart_shuffle = Mock()
    ctrl._smart_shuffle.is_enabled = Mock(return_value=False)
    existing = _items("q1", ["e0", "e1", "e2"])
    queue = PlayerQueue(
        queue_id="q1",
        active=True,
        display_name="Q1",
        available=True,
        items=len(existing),
        state=PlaybackState.PLAYING,
        current_index=0,
        index_in_buffer=0,
        shuffle_enabled=True,
    )
    ctrl._queue_data = {"q1": PlayerQueueData(queue=queue, items=list(existing))}
    new_items = _items("q1", ["n0", "n1"])

    await ctrl._enqueue_with_option("q1", new_items, QueueOption.NEXT, managed_pool=False)

    items = ctrl._queue_data["q1"].items
    # the first new item is pinned right after the buffered index (0) so it plays next
    assert items[0] is existing[0]
    assert items[1] is new_items[0]
    # the rest of the batch and the existing unplayed tail are shuffled together behind it
    assert {item.queue_item_id for item in items[2:]} == {"n1", "e1", "e2"}
    ctrl.play_index.assert_not_awaited()


async def test_next_shuffle_single_item_keeps_tail_order() -> None:
    """A single-item NEXT inserts at the buffered index+1 without re-arranging the tail."""
    ctrl = _controller()
    ctrl.play_index = AsyncMock()  # type: ignore[method-assign]
    ctrl.get_next_item = Mock(return_value=None)  # type: ignore[method-assign]
    existing = _items("q1", ["e0", "e1", "e2"])
    queue = PlayerQueue(
        queue_id="q1",
        active=True,
        display_name="Q1",
        available=True,
        items=len(existing),
        state=PlaybackState.PLAYING,
        current_index=0,
        index_in_buffer=0,
        shuffle_enabled=True,
    )
    ctrl._queue_data = {"q1": PlayerQueueData(queue=queue, items=list(existing))}
    new_items = _items("q1", ["n0"])

    await ctrl._enqueue_with_option("q1", new_items, QueueOption.NEXT, managed_pool=False)

    # inserted right after the buffered index; the existing tail keeps its order (no reshuffle)
    assert [item.queue_item_id for item in ctrl._queue_data["q1"].items] == ["e0", "n0", "e1", "e2"]
    ctrl.play_index.assert_not_awaited()


async def test_next_on_empty_queue_sets_current_index_without_playing() -> None:
    """NEXT onto an empty queue stages the items and points the current index at the first one."""
    ctrl = _controller()
    ctrl.play_index = AsyncMock()  # type: ignore[method-assign]
    queue = PlayerQueue(queue_id="q1", active=True, display_name="Q1", available=True, items=0)
    ctrl._queue_data = {"q1": PlayerQueueData(queue=queue)}
    items = _items("q1", ["a", "b", "c"])

    await ctrl._enqueue_with_option("q1", items, QueueOption.NEXT, managed_pool=False)

    assert [item.queue_item_id for item in ctrl._queue_data["q1"].items] == ["a", "b", "c"]
    assert queue.current_index == 0
    assert queue.current_item is items[0]
    ctrl.play_index.assert_not_awaited()


async def test_replace_next_on_empty_queue_sets_current_index_without_playing() -> None:
    """REPLACE_NEXT onto an empty queue stages the items and sets the current index."""
    ctrl = _controller()
    ctrl.play_index = AsyncMock()  # type: ignore[method-assign]
    queue = PlayerQueue(queue_id="q1", active=True, display_name="Q1", available=True, items=0)
    ctrl._queue_data = {"q1": PlayerQueueData(queue=queue)}
    items = _items("q1", ["a", "b"])

    await ctrl._enqueue_with_option("q1", items, QueueOption.REPLACE_NEXT, managed_pool=False)

    assert [item.queue_item_id for item in ctrl._queue_data["q1"].items] == ["a", "b"]
    assert queue.current_index == 0
    assert queue.current_item is items[0]
    ctrl.play_index.assert_not_awaited()


async def test_next_on_idle_queue_with_content_keeps_current_index() -> None:
    """NEXT on an idle queue that has content leaves the current index and inserts after it."""
    ctrl = _controller()
    ctrl.play_index = AsyncMock()  # type: ignore[method-assign]
    existing = _items("q1", ["e0", "e1", "e2"])
    queue = PlayerQueue(
        queue_id="q1",
        active=True,
        display_name="Q1",
        available=True,
        items=len(existing),
        state=PlaybackState.IDLE,
        current_index=1,
    )
    ctrl._queue_data = {"q1": PlayerQueueData(queue=queue, items=list(existing))}
    new_items = _items("q1", ["n0"])

    await ctrl._enqueue_with_option("q1", new_items, QueueOption.NEXT, managed_pool=False)

    items = ctrl._queue_data["q1"].items
    # current index is untouched; the new item is inserted right after it
    assert queue.current_index == 1
    assert [item.queue_item_id for item in items] == ["e0", "e1", "n0", "e2"]
    ctrl.play_index.assert_not_awaited()
