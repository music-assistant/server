"""Tests for keeping the player's upcoming track in step with the queue."""

from __future__ import annotations

from typing import Any
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
    index_in_buffer: int | None = 1,
    state: PlaybackState = PlaybackState.PLAYING,
    enqueued_offset: int = 1,
) -> Any:
    """
    Build a controller whose player has been handed the track at `enqueued_offset`.

    :param current_index: The index the player is playing.
    :param index_in_buffer: The index the streams engine has read ahead to.
    :param state: The queue's playback state.
    :param enqueued_offset: Offset from current_index of the track the player already holds.
    """
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    ctrl.logger = Mock()
    ctrl.mass = MagicMock()
    ctrl.signal_update = Mock()  # type: ignore[method-assign]
    ctrl._enqueue_next_item = Mock()  # type: ignore[method-assign]
    ctrl._smart_shuffle = Mock()
    ctrl._smart_shuffle.is_enabled = Mock(return_value=False)
    ctrl.mass.streams.is_smart_fades_active = Mock(return_value=False)
    queue = PlayerQueue(queue_id="q1", active=True, display_name="Q1", available=True, items=0)
    queue_data = PlayerQueueData(queue=queue)
    ctrl._queue_data = {"q1": queue_data}
    items = [QueueItem.from_media_item("q1", _track(item_id)) for item_id in TRACKS]
    queue_data.items = items
    queue_data.next_item_id_enqueued = items[current_index + enqueued_offset].queue_item_id
    queue.items = len(items)
    queue.state = state
    queue.current_index = current_index
    queue.current_item = items[current_index]
    queue.index_in_buffer = index_in_buffer
    queue.repeat_mode = RepeatMode.OFF
    return ctrl


def _enqueued(ctrl: Any) -> str | None:
    """Return the provider item id handed to the player, if any."""
    if not ctrl._enqueue_next_item.called:
        return None
    item = ctrl._enqueue_next_item.call_args.args[1]
    return str(item.media_item.item_id)


async def test_a_changed_upcoming_track_reaches_the_player_while_a_track_is_buffered() -> None:
    """A track buffered ahead does not stop the player being told the upcoming track changed."""
    ctrl = _controller(current_index=0, index_in_buffer=1)
    items = ctrl._queue_data["q1"].items
    reordered = [items[0], items[4], items[1], items[2], items[3]]

    ctrl.update_items("q1", reordered)

    assert _enqueued(ctrl) == "t4"


async def test_an_unchanged_upcoming_track_is_not_handed_over_again() -> None:
    """A queue change that leaves the upcoming track alone sends nothing to the player."""
    ctrl = _controller(current_index=0, index_in_buffer=1)
    items = ctrl._queue_data["q1"].items
    reordered = [items[0], items[1], items[4], items[2], items[3]]

    ctrl.update_items("q1", reordered)

    assert _enqueued(ctrl) is None


async def test_repeat_one_hands_the_playing_track_back_to_the_player() -> None:
    """Switching to repeat-one makes the playing track the upcoming one on the player too."""
    ctrl = _controller(current_index=0, index_in_buffer=1)

    await ctrl.set_repeat("q1", RepeatMode.ONE)

    assert _enqueued(ctrl) == "t0"


async def test_crossfade_hands_the_same_track_over_again() -> None:
    """Crossfade changes how the upcoming track is streamed, so it is handed over again."""
    ctrl = _controller(current_index=0, index_in_buffer=1)

    ctrl.set_crossfade("q1", crossfade_enabled=True)

    assert _enqueued(ctrl) == "t1"


async def test_nothing_is_handed_over_while_the_queue_holds_no_position() -> None:
    """A queue mid-replace has no committed position, so no upcoming track is picked for it."""
    ctrl = _controller(current_index=0, index_in_buffer=None)
    items = ctrl._queue_data["q1"].items

    ctrl.update_items("q1", [items[0], items[4], items[1], items[2], items[3]])

    assert _enqueued(ctrl) is None


async def test_nothing_is_handed_over_while_the_queue_is_not_playing() -> None:
    """A paused or idle queue is not handed an upcoming track."""
    ctrl = _controller(current_index=0, index_in_buffer=1, state=PlaybackState.PAUSED)
    items = ctrl._queue_data["q1"].items

    ctrl.update_items("q1", [items[0], items[4], items[1], items[2], items[3]])

    assert _enqueued(ctrl) is None
