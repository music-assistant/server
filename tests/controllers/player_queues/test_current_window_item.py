"""Tests for the queue's playhead window check used to refuse stale stream requests."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import MagicMock, Mock

from music_assistant_models.enums import MediaType, PlaybackState, RepeatMode
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
    current_index: int = 0,
    index_in_buffer: int | None = 0,
    repeat_mode: RepeatMode = RepeatMode.OFF,
) -> Any:
    """Build a bare controller holding queue "q1" loaded with TRACKS at the given position."""
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    ctrl.logger = Mock()
    ctrl.mass = MagicMock()
    ctrl.signal_update = Mock()  # type: ignore[method-assign]
    ctrl._enqueue_next_item = Mock()  # type: ignore[method-assign]
    queue = PlayerQueue(queue_id="q1", active=True, display_name="Q1", available=True, items=0)
    ctrl._queue_data = {"q1": PlayerQueueData(queue=queue)}
    items = [QueueItem.from_media_item("q1", _track(item_id)) for item_id in TRACKS]
    ctrl._queue_data["q1"].items = items
    queue.items = len(items)
    queue.state = PlaybackState.PLAYING
    queue.repeat_mode = repeat_mode
    queue.current_index = current_index
    queue.current_item = items[current_index]
    queue.index_in_buffer = index_in_buffer
    return ctrl


def _item_id_at(ctrl: Any, index: int) -> str:
    """Return the queue_item_id of the item sitting at the given index."""
    return cast("str", ctrl._queue_data["q1"].items[index].queue_item_id)


def test_current_and_previous_track_are_window_items() -> None:
    """The playing track and the one right before it may (re)load; older ones may not."""
    ctrl = _controller(current_index=2, index_in_buffer=2)

    assert ctrl.is_current_window_item("q1", _item_id_at(ctrl, 2))
    assert ctrl.is_current_window_item("q1", _item_id_at(ctrl, 1))
    assert not ctrl.is_current_window_item("q1", _item_id_at(ctrl, 0))


def test_expected_next_track_is_a_window_item() -> None:
    """The track that will really play next passes; the one after it does not."""
    ctrl = _controller(current_index=1, index_in_buffer=1)

    assert ctrl.is_current_window_item("q1", _item_id_at(ctrl, 2))
    assert not ctrl.is_current_window_item("q1", _item_id_at(ctrl, 3))


def test_crossfade_preload_does_not_widen_the_window() -> None:
    """
    Refuse the item after the buffered track.

    The crossfade preload moves index_in_buffer to the next track while the current one
    still plays; the buffered track passes, but the item after it must not (after a
    play-next move that is exactly the stale previously-next track).
    """
    ctrl = _controller(current_index=1, index_in_buffer=2)

    assert ctrl.is_current_window_item("q1", _item_id_at(ctrl, 2))
    assert not ctrl.is_current_window_item("q1", _item_id_at(ctrl, 3))
    assert not ctrl.is_current_window_item("q1", _item_id_at(ctrl, 4))


def test_missing_centers_are_tolerated() -> None:
    """A cleared buffer index (a replace in progress) or unknown playhead cannot crash."""
    ctrl = _controller(current_index=1, index_in_buffer=None)
    assert ctrl.is_current_window_item("q1", _item_id_at(ctrl, 1))
    assert ctrl.is_current_window_item("q1", _item_id_at(ctrl, 2))

    ctrl = _controller(current_index=1, index_in_buffer=1)
    ctrl._queue_data["q1"].queue.current_index = None
    assert ctrl.is_current_window_item("q1", _item_id_at(ctrl, 1))
    assert not ctrl.is_current_window_item("q1", _item_id_at(ctrl, 2))


def test_repeat_all_wraps_the_expected_next_track() -> None:
    """With repeat all, the first track counts as the next one at the end of the queue."""
    ctrl = _controller(current_index=4, index_in_buffer=4, repeat_mode=RepeatMode.ALL)

    assert ctrl.is_current_window_item("q1", _item_id_at(ctrl, 0))


def test_repeat_one_keeps_the_window_on_the_current_track() -> None:
    """With repeat one, the queue will replay the current track, not advance past it."""
    ctrl = _controller(current_index=1, index_in_buffer=1, repeat_mode=RepeatMode.ONE)

    assert ctrl.is_current_window_item("q1", _item_id_at(ctrl, 1))
    assert not ctrl.is_current_window_item("q1", _item_id_at(ctrl, 2))


def test_unavailable_next_track_is_skipped_over() -> None:
    """An unavailable track is never up next; the first playable one after it is."""
    ctrl = _controller(current_index=1, index_in_buffer=1)
    ctrl._queue_data["q1"].items[2].available = False

    assert not ctrl.is_current_window_item("q1", _item_id_at(ctrl, 2))
    assert ctrl.is_current_window_item("q1", _item_id_at(ctrl, 3))


def test_unknown_item_and_unknown_queue_are_not_window_items() -> None:
    """Anything the queue does not hold is refused."""
    ctrl = _controller()

    assert not ctrl.is_current_window_item("q1", "no_such_item")
    assert not ctrl.is_current_window_item("no_such_queue", _item_id_at(ctrl, 0))


def test_moving_a_track_to_play_next_dethrones_the_previously_next_track() -> None:
    """
    Refuse the stale next track after a move, allow the new one.

    The reported bug: a player cached track B as next, the user moves track D to play
    next, and the player (whose refresh signal got lost) still asks for B at the
    transition. B must be refused so the player re-reads the queue and picks up D.
    """
    ctrl = _controller(current_index=0, index_in_buffer=0)
    stale_next = _item_id_at(ctrl, 1)
    moved_up = _item_id_at(ctrl, 3)

    ctrl.move_item("q1", moved_up, pos_shift=0)

    assert not ctrl.is_current_window_item("q1", stale_next)
    assert ctrl.is_current_window_item("q1", moved_up)
