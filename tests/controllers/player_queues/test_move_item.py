"""Tests for moving items around a playing queue."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import MagicMock, Mock

import pytest
from music_assistant_models.enums import MediaType, PlaybackState
from music_assistant_models.media_items import ItemMapping, ProviderMapping, Track
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem
from music_assistant_models.unique_list import UniqueList

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData

TRACKS = ["t0", "t1", "t2", "t3", "t4"]


def _track(item_id: str) -> Track:
    """Build a playable Track on the 'test' provider."""
    return Track(
        item_id=item_id,
        provider="test",
        name=f"Track {item_id}",
        duration=60,
        artists=UniqueList(
            [ItemMapping(item_id="a", provider="test", name="A", media_type=MediaType.ARTIST)]
        ),
        provider_mappings={
            ProviderMapping(item_id=item_id, provider_domain="test", provider_instance="test")
        },
    )


def _controller(
    *,
    state: PlaybackState = PlaybackState.PLAYING,
    current_index: int = 0,
    index_in_buffer: int | None = 0,
) -> Any:
    """Build a bare controller holding queue "q1" loaded with TRACKS at the given position."""
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    ctrl.logger = Mock()
    ctrl.mass = MagicMock()
    ctrl.signal_update = Mock()  # type: ignore[method-assign]
    ctrl.get_next_item = Mock(return_value=None)  # type: ignore[method-assign]
    ctrl._enqueue_next_item = Mock()  # type: ignore[method-assign]
    queue = PlayerQueue(queue_id="q1", active=True, display_name="Q1", available=True, items=0)
    ctrl._queue_data = {"q1": PlayerQueueData(queue=queue)}
    items = [QueueItem.from_media_item("q1", _track(item_id)) for item_id in TRACKS]
    ctrl._queue_data["q1"].items = items
    queue.items = len(items)
    queue.state = state
    queue.current_index = current_index
    queue.current_item = items[current_index]
    queue.index_in_buffer = index_in_buffer
    return ctrl


def _item_id_at(ctrl: Any, index: int) -> str:
    """Return the queue_item_id of the item sitting at the given index."""
    return cast("str", ctrl._queue_data["q1"].items[index].queue_item_id)


def _order(ctrl: Any) -> list[str]:
    """Return the provider item ids of the queue's items, in play order."""
    return [
        item.media_item.item_id
        for item in ctrl._queue_data["q1"].items
        if item.media_item is not None
    ]


async def test_play_next_lands_behind_the_buffered_track() -> None:
    """With a track buffered ahead, "play next" queues behind it rather than into its slot."""
    ctrl = _controller(current_index=0, index_in_buffer=1)
    buffered_item_id = _item_id_at(ctrl, 1)

    ctrl.move_item("q1", _item_id_at(ctrl, 4), pos_shift=0)

    assert _order(ctrl) == ["t0", "t1", "t4", "t2", "t3"]
    assert _item_id_at(ctrl, 1) == buffered_item_id


async def test_play_next_lands_behind_the_playing_track_when_nothing_is_buffered_ahead() -> None:
    """Without a track buffered ahead, "play next" is the slot right after the playing one."""
    ctrl = _controller(current_index=0, index_in_buffer=0)

    ctrl.move_item("q1", _item_id_at(ctrl, 4), pos_shift=0)

    assert _order(ctrl) == ["t0", "t4", "t1", "t2", "t3"]


async def test_play_next_on_the_item_already_next_keeps_the_order() -> None:
    """Asking for the item that is already first in line is a no-op."""
    ctrl = _controller(current_index=0, index_in_buffer=1)

    ctrl.move_item("q1", _item_id_at(ctrl, 2), pos_shift=0)

    assert _order(ctrl) == TRACKS


async def test_play_next_on_a_paused_queue_respects_the_buffered_track() -> None:
    """A paused player still holds the track it was handed, so the move goes behind it."""
    ctrl = _controller(state=PlaybackState.PAUSED, current_index=1, index_in_buffer=2)

    ctrl.move_item("q1", _item_id_at(ctrl, 4), pos_shift=0)

    assert _order(ctrl) == ["t0", "t1", "t2", "t4", "t3"]


async def test_play_next_on_an_idle_queue_puts_the_item_first() -> None:
    """On a queue that is not playing, the moved item takes the position that plays next."""
    ctrl = _controller(state=PlaybackState.IDLE, current_index=0, index_in_buffer=None)

    ctrl.move_item("q1", _item_id_at(ctrl, 3), pos_shift=0)

    assert _order(ctrl) == ["t3", "t0", "t1", "t2", "t4"]


async def test_play_next_clears_a_track_buffered_two_ahead() -> None:
    """A buffered index further than one ahead still decides where the move lands."""
    ctrl = _controller(current_index=0, index_in_buffer=2)

    ctrl.move_item("q1", _item_id_at(ctrl, 4), pos_shift=0)

    assert _order(ctrl) == ["t0", "t1", "t2", "t4", "t3"]


async def test_moving_is_refused_while_repeat_wraps_the_queue() -> None:
    """A queue whose buffered track wrapped back to the front refuses moves."""
    ctrl = _controller(current_index=4, index_in_buffer=0)

    with pytest.raises(IndexError):
        ctrl.move_item("q1", _item_id_at(ctrl, 2), pos_shift=0)

    assert _order(ctrl) == TRACKS
    assert _item_id_at(ctrl, 4) == ctrl._queue_data["q1"].queue.current_item.queue_item_id


async def test_moving_a_buffered_item_is_refused() -> None:
    """An item at or before the buffered one cannot be moved."""
    ctrl = _controller(current_index=0, index_in_buffer=1)

    with pytest.raises(IndexError):
        ctrl.move_item("q1", _item_id_at(ctrl, 1), pos_shift=1)


async def test_relative_move_is_unaffected_by_the_buffered_index() -> None:
    """A relative move still shifts the item by the requested number of positions."""
    ctrl = _controller(current_index=0, index_in_buffer=1)

    ctrl.move_item("q1", _item_id_at(ctrl, 2), pos_shift=1)

    assert _order(ctrl) == ["t0", "t1", "t3", "t2", "t4"]
