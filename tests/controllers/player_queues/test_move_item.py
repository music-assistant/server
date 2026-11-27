"""Tests for moving items around a playing queue."""

from __future__ import annotations

import time
from typing import Any, cast
from unittest.mock import MagicMock, Mock

import pytest
from music_assistant_models.enums import MediaType, PlaybackState
from music_assistant_models.media_items import ItemMapping, ProviderMapping, Track
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem
from music_assistant_models.unique_list import UniqueList

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.constants import NEXT_ITEM_REPLACE_MARGIN
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
    ctrl.mass.players.get_player.return_value.fake_paused = False
    ctrl.signal_update = Mock()  # type: ignore[method-assign]
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
    queue.elapsed_time_last_updated = time.time()
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


@pytest.mark.parametrize(
    ("flow_mode", "faded_in", "expected_order"),
    [
        (False, False, ["t0", "t4", "t1", "t2", "t3"]),
        (True, False, ["t0", "t1", "t4", "t2", "t3"]),
        (False, True, ["t0", "t1", "t4", "t2", "t3"]),
    ],
    ids=["replaceable", "flow mode", "faded into"],
)
async def test_play_next_takes_the_slot_of_a_buffered_track_the_player_can_replace(
    flow_mode: bool, faded_in: bool, expected_order: list[str]
) -> None:
    """A player that can drop the track it buffered gets the new one, unless it was faded into."""
    ctrl = _controller(current_index=0, index_in_buffer=0)
    ctrl._preload_next_item = Mock()
    ctrl.mass.players.replaces_enqueued_media.return_value = True
    ctrl._queue_data["q1"].queue.flow_mode = flow_mode
    ctrl.track_loaded_in_buffer("q1", _item_id_at(ctrl, 1), faded_in=faded_in)

    ctrl.move_item("q1", _item_id_at(ctrl, 4), pos_shift=0)

    assert _order(ctrl) == expected_order


async def test_play_next_lands_behind_a_track_a_crossfade_is_prepared_into() -> None:
    """Once the playing track's stream prepares a crossfade into the next one, it stays next."""
    ctrl = _controller(current_index=0, index_in_buffer=0)
    ctrl._preload_next_item = Mock()
    ctrl.mass.players.replaces_enqueued_media.return_value = True
    ctrl.track_loaded_in_buffer("q1", _item_id_at(ctrl, 0))
    ctrl.fading_into("q1", _item_id_at(ctrl, 1))

    ctrl.move_item("q1", _item_id_at(ctrl, 4), pos_shift=0)

    assert _order(ctrl) == ["t0", "t1", "t4", "t2", "t3"]


@pytest.mark.parametrize("elapsed", [56.0, 20.0], ids=["within the margin", "before it"])
async def test_play_next_lands_behind_a_buffered_track_that_starts_too_soon_to_replace(
    elapsed: float,
) -> None:
    """An edit in the last seconds of the playing track would reach the player too late."""
    ctrl = _controller(current_index=0, index_in_buffer=0)
    ctrl._preload_next_item = Mock()
    ctrl.mass.players.replaces_enqueued_media.return_value = True
    queue = ctrl._queue_data["q1"].queue
    queue.elapsed_time = elapsed
    queue.playback_speed = 2.0
    ctrl.track_loaded_in_buffer("q1", _item_id_at(ctrl, 1))
    assert ctrl.mass.call_later.called is (elapsed == 20.0)
    if ctrl.mass.call_later.called:
        delay, lock_next_item, queue_id = ctrl.mass.call_later.call_args.args
        assert delay == pytest.approx((60 - elapsed) / 2 - NEXT_ITEM_REPLACE_MARGIN, abs=0.5)
        ctrl.signal_update.reset_mock()
        lock_next_item(queue_id)
        ctrl.signal_update.assert_called_once_with("q1")

    ctrl.move_item("q1", _item_id_at(ctrl, 4), pos_shift=0)

    assert _order(ctrl) == ["t0", "t1", "t4", "t2", "t3"]


async def test_clients_get_the_locked_index_as_the_playing_track_moves_on() -> None:
    """The published index follows the playing track, not only the buffered one."""
    ctrl = _controller(current_index=0, index_in_buffer=0)
    ctrl._preload_next_item = Mock()
    ctrl.mass.players.replaces_enqueued_media.return_value = True
    ctrl.track_loaded_in_buffer("q1", _item_id_at(ctrl, 1))
    queue = ctrl._queue_data["q1"].queue
    PlayerQueuesController.signal_update(ctrl, "q1")
    assert queue.last_locked_index == 0

    queue.current_index = 1
    PlayerQueuesController.signal_update(ctrl, "q1")

    assert queue.last_locked_index == 1


async def test_the_buffered_track_stays_when_nothing_follows_it() -> None:
    """A player can only replace the buffered track with another one, so the last one stays."""
    ctrl = _controller(current_index=3, index_in_buffer=3)
    ctrl._preload_next_item = Mock()
    ctrl.mass.players.replaces_enqueued_media.return_value = True
    ctrl.track_loaded_in_buffer("q1", _item_id_at(ctrl, 4))

    ctrl.delete_item("q1", _item_id_at(ctrl, 4))

    assert _order(ctrl) == TRACKS


async def test_the_buffered_track_stays_when_only_unavailable_tracks_follow_it() -> None:
    """The queue skips unavailable tracks, so none of them can take the buffered one's place."""
    ctrl = _controller(current_index=2, index_in_buffer=2)
    ctrl._preload_next_item = Mock()
    ctrl.mass.players.replaces_enqueued_media.return_value = True
    ctrl._queue_data["q1"].items[4].available = False
    ctrl.track_loaded_in_buffer("q1", _item_id_at(ctrl, 3))

    ctrl.delete_item("q1", _item_id_at(ctrl, 3))

    assert _order(ctrl) == TRACKS


async def test_clients_get_a_buffered_last_track_as_locked_until_a_track_follows_it() -> None:
    """Once a track is added behind the buffered last one, clients may remove the buffered one."""
    ctrl = _controller(current_index=3, index_in_buffer=3)
    ctrl._preload_next_item = Mock()
    ctrl.mass.players.replaces_enqueued_media.return_value = True
    ctrl.track_loaded_in_buffer("q1", _item_id_at(ctrl, 4))
    queue = ctrl._queue_data["q1"].queue
    PlayerQueuesController.signal_update(ctrl, "q1")
    assert queue.last_locked_index == 4

    added = QueueItem.from_media_item("q1", _track("t5"))
    ctrl.update_items("q1", [*ctrl._queue_data["q1"].items, added])
    PlayerQueuesController.signal_update(ctrl, "q1")
    assert queue.last_locked_index == 3

    ctrl.delete_item("q1", _item_id_at(ctrl, 4))

    assert _order(ctrl) == ["t0", "t1", "t2", "t3", "t5"]


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
    """A queue whose buffered track wrapped back to the front refuses moves ahead of it."""
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


async def test_moving_to_the_end_is_refused_while_repeat_wraps_the_queue() -> None:
    """Moving to the end shifts the playing track too, so it is refused on a wrapped queue."""
    ctrl = _controller(current_index=4, index_in_buffer=0)

    with pytest.raises(IndexError):
        ctrl.move_item_end("q1", _item_id_at(ctrl, 2))

    assert _order(ctrl) == TRACKS


async def test_deleting_is_ignored_while_repeat_wraps_the_queue() -> None:
    """Deleting ahead of the playing track shifts it, so it is ignored on a wrapped queue."""
    ctrl = _controller(current_index=4, index_in_buffer=0)

    ctrl.delete_item("q1", 2)

    assert _order(ctrl) == TRACKS


async def test_a_relative_move_cannot_land_on_a_track_the_player_owns() -> None:
    """A move towards the front stops at the boundary instead of landing inside it."""
    ctrl = _controller(current_index=0, index_in_buffer=2)

    ctrl.move_item("q1", _item_id_at(ctrl, 4), pos_shift=-3)

    assert _order(ctrl) == TRACKS


async def test_relative_move_is_unaffected_by_the_buffered_index() -> None:
    """A relative move still shifts the item by the requested number of positions."""
    ctrl = _controller(current_index=0, index_in_buffer=1)

    ctrl.move_item("q1", _item_id_at(ctrl, 2), pos_shift=1)

    assert _order(ctrl) == ["t0", "t1", "t3", "t2", "t4"]
