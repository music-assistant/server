"""
Tests for the enqueue-option handling in the player-queues controller.

These exercise ``PlayerQueuesController._enqueue_with_option`` against a bare controller
instance. The real ``load`` and ``update_items`` run so the insert position is verified
end-to-end; only the side-effecting ``play_index`` and ``signal_update`` are stubbed out.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

from music_assistant_models.enums import PlaybackState, QueueOption
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem

from music_assistant.controllers.player_queues import PlayerQueuesController


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
    ctrl._queues = {"q1": queue}
    ctrl._queue_items = {"q1": []}
    items = _items("q1", ["a", "b", "c"])

    await ctrl._enqueue_with_option("q1", items, QueueOption.PLAY, radio_mode=False)

    # the items are loaded from the very start and playback begins at the first one
    assert ctrl._queue_items["q1"][0] is items[0]
    assert len(ctrl._queue_items["q1"]) == 3
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
    ctrl._queues = {"q1": queue}
    ctrl._queue_items = {"q1": list(existing)}
    new_items = _items("q1", ["n0", "n1"])

    await ctrl._enqueue_with_option("q1", new_items, QueueOption.PLAY, radio_mode=False)

    # inserted right after the current/buffered index (2) and playback jumps there
    assert ctrl._queue_items["q1"][3] is new_items[0]
    play_index.assert_awaited_once_with("q1", 3)
