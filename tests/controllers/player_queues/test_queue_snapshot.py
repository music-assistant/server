"""Tests for taking a snapshot of a queue's content and restoring it later."""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock

from music_assistant_models.enums import (
    ContentType,
    MediaType,
    PlaybackState,
    RepeatMode,
    StreamType,
)
from music_assistant_models.media_items import (
    AudioFormat,
    ItemMapping,
    Playlist,
    ProviderMapping,
    Track,
)
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem
from music_assistant_models.streamdetails import StreamDetails
from music_assistant_models.unique_list import UniqueList

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData
from music_assistant.controllers.streams.audio_buffer import AudioBuffer

QUEUE_ID = "q1"


def _track(item_id: str) -> Track:
    """Build a playable Track on the 'test' provider."""
    return Track(
        item_id=item_id,
        provider="test",
        name=f"Track {item_id}",
        duration=180,
        artists=UniqueList(
            [ItemMapping(item_id="a", provider="test", name="A", media_type=MediaType.ARTIST)]
        ),
        provider_mappings={
            ProviderMapping(item_id=item_id, provider_domain="test", provider_instance="test")
        },
    )


def _dynamic_playlist() -> Playlist:
    """Build a playlist that feeds the queue with its own tracks (a dynamic source)."""
    playlist = Playlist(
        item_id="radio",
        provider="test",
        name="Radio",
        provider_mappings={
            ProviderMapping(item_id="radio", provider_domain="test", provider_instance="test")
        },
    )
    playlist.is_dynamic = True
    return playlist


def _attach_buffer(queue_item: QueueItem) -> None:
    """Give a queue item stream details holding an audio buffer, as playback does."""
    buffer = MagicMock(spec=AudioBuffer)
    buffer.clear = AsyncMock()
    queue_item.streamdetails = StreamDetails(
        provider="test",
        item_id=queue_item.queue_item_id,
        audio_format=AudioFormat(content_type=ContentType.MP3),
        media_type=MediaType.TRACK,
        stream_type=StreamType.HTTP,
        path="http://test.invalid/track.mp3",
        queue_id=QUEUE_ID,
    )
    queue_item.streamdetails.buffer = buffer


def _controller() -> Any:
    """
    Build a bare controller holding a dynamic, shuffled queue positioned on its second item.

    The queue has three items, a resume position, autoplay seeds, credited albums and an owner.
    """
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    ctrl.logger = Mock()
    ctrl.mass = MagicMock()

    def _create_task(target: Any, *args: Any, **_kwargs: Any) -> asyncio.Task[Any]:
        coro = target if asyncio.iscoroutine(target) else target(*args)
        return asyncio.get_running_loop().create_task(coro)

    ctrl.mass.create_task.side_effect = _create_task
    ctrl._smart_shuffle = MagicMock()
    ctrl._smart_shuffle.is_enabled.return_value = False
    ctrl._managed_pool = MagicMock()
    ctrl._check_player_permission = Mock()  # type: ignore[method-assign]
    playlist = _dynamic_playlist()
    queue = PlayerQueue(
        queue_id=QUEUE_ID,
        active=True,
        display_name="Q1",
        available=True,
        items=0,
        shuffle_enabled=True,
        is_dynamic=True,
        sources=[ItemMapping.from_item(playlist)],
    )
    items = [QueueItem.from_media_item(QUEUE_ID, _track(f"t{index}")) for index in range(3)]
    ctrl._queue_data = {
        QUEUE_ID: PlayerQueueData(
            queue=queue,
            items=items,
            source_items=[playlist],
            enqueued_media_items=[playlist],
            credited_albums={"test://album/1"},
            userid="user-1",
        )
    }
    queue.items = len(items)
    queue.state = PlaybackState.IDLE
    queue.current_index = 1
    queue.current_item = items[1]
    queue.next_item = items[2]
    queue.resume_pos = 42
    return ctrl


def _take_over_like_a_quiz(ctrl: Any) -> None:
    """Replace the queue's content and change a setting, like a Music Quiz game does."""
    queue_data = ctrl._queue_data[QUEUE_ID]
    queue = queue_data.queue
    ctrl.clear(QUEUE_ID, skip_stop=True)
    quiz_track = _track("quiz")
    quiz_item = QueueItem.from_media_item(QUEUE_ID, quiz_track)
    _attach_buffer(quiz_item)
    queue_data.enqueued_media_items = [quiz_track]
    queue_data.credited_albums = set()
    queue_data.userid = None
    ctrl.update_items(QUEUE_ID, [quiz_item])
    queue.current_index = 0
    queue.current_item = quiz_item
    queue.elapsed_time = 25
    queue.resume_pos = 0
    queue.repeat_mode = RepeatMode.ALL


async def test_restore_puts_back_the_content_a_takeover_replaced() -> None:
    """Items, position, shuffle, seeds, sources and owner come back; later settings stay."""
    ctrl = _controller()
    queue_data = ctrl._queue_data[QUEUE_ID]
    queue = queue_data.queue
    original_ids = [item.queue_item_id for item in queue_data.items]
    _attach_buffer(queue_data.items[1])

    snapshot = ctrl.snapshot(QUEUE_ID)
    _take_over_like_a_quiz(ctrl)
    assert (queue.shuffle_enabled, queue.is_dynamic) == (False, False)
    await ctrl.restore(QUEUE_ID, snapshot)

    assert [item.queue_item_id for item in queue_data.items] == original_ids
    assert {item.queue_id for item in queue_data.items} == {QUEUE_ID}
    assert queue.items == 3
    assert queue.current_index == 1
    assert queue.current_item is queue_data.items[1]
    assert queue.next_item is queue_data.items[2]
    assert queue.index_in_buffer is None
    assert queue.resume_pos == 42
    assert queue.elapsed_time == 42
    assert queue.ended is False
    assert queue.shuffle_enabled is True
    assert queue.is_dynamic is True
    assert queue.smart_shuffle_active is True
    assert [source.uri for source in queue.sources] == ["test://playlist/radio"]
    assert [item.uri for item in queue_data.source_items] == ["test://playlist/radio"]
    assert [item.uri for item in queue_data.enqueued_media_items] == ["test://playlist/radio"]
    assert queue_data.credited_albums == {"test://album/1"}
    assert queue_data.userid == "user-1"
    # a setting changed after the snapshot is not part of the content
    assert queue.repeat_mode == RepeatMode.ALL


async def test_snapshot_is_detached_from_the_live_queue() -> None:
    """The snapshot holds its own items, without the audio buffers of the live ones."""
    ctrl = _controller()
    live_items = ctrl._queue_data[QUEUE_ID].items
    _attach_buffer(live_items[1])

    snapshot = ctrl.snapshot(QUEUE_ID)
    await ctrl.restore(QUEUE_ID, snapshot)

    assert snapshot.queue_id == QUEUE_ID
    for saved, live in zip(snapshot.data.items, live_items, strict=True):
        assert saved is not live
        assert saved.streamdetails is None
    restored = ctrl._queue_data[QUEUE_ID].items
    assert all(item.streamdetails is None for item in restored)
    # restoring hands the queue copies, so the snapshot stays detached
    assert not {id(item) for item in restored} & {id(item) for item in snapshot.data.items}


async def test_snapshot_of_a_playing_queue_keeps_the_live_position() -> None:
    """While playing, the live playback clock is the position, not the stored resume point."""
    ctrl = _controller()
    queue = ctrl._queue_data[QUEUE_ID].queue
    queue.state = PlaybackState.PLAYING
    queue.elapsed_time = 30
    queue.elapsed_time_last_updated = time.time() - 5
    queue.resume_pos = 7

    snapshot = ctrl.snapshot(QUEUE_ID)

    assert 35 <= snapshot.data.queue.resume_pos <= 36


async def test_restore_stops_a_playing_queue_first() -> None:
    """Playback on the queue is stopped before its content is swapped, and not started again."""
    ctrl = _controller()
    queue_data = ctrl._queue_data[QUEUE_ID]
    snapshot = ctrl.snapshot(QUEUE_ID)
    _take_over_like_a_quiz(ctrl)
    queue_data.queue.state = PlaybackState.PLAYING
    ctrl._handle_stop = AsyncMock()
    ctrl.play_index = AsyncMock()
    ctrl.resume = AsyncMock()

    await ctrl.restore(QUEUE_ID, snapshot)

    ctrl._handle_stop.assert_awaited_once_with(QUEUE_ID)
    ctrl.play_index.assert_not_awaited()
    ctrl.resume.assert_not_awaited()
    assert queue_data.queue.current_item is queue_data.items[1]


async def test_restore_on_a_queue_that_is_gone_does_nothing() -> None:
    """A queue that no longer exists is left alone."""
    ctrl = _controller()
    snapshot = ctrl.snapshot(QUEUE_ID)
    ctrl.mass.signal_event.reset_mock()

    await ctrl.restore("gone", snapshot)

    assert "gone" not in ctrl._queue_data
    ctrl.mass.signal_event.assert_not_called()
