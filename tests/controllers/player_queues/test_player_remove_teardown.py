"""
Tests for what removing a player tears down of its queue.

An unregister does not stop the queue first, so its removal has to do what a stop would:
otherwise the prepared next track keeps its provider stream slot until the buffer's
inactivity timeout, and the preload and enqueue-next work runs on for a queue that is gone.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.queue_item import QueueItem
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData
from music_assistant.controllers.streams.audio_buffer import AudioBuffer

QUEUE_ID = "q1"


def _item(item_id: str) -> QueueItem:
    """Build a queue item with a prepared buffer attached."""
    queue_item = QueueItem(queue_id=QUEUE_ID, queue_item_id=item_id, name=item_id, duration=180)
    audio_buffer = MagicMock(spec=AudioBuffer)
    audio_buffer.clear = AsyncMock()
    queue_item.streamdetails = StreamDetails(
        provider="local--1",
        item_id=item_id,
        audio_format=AudioFormat(content_type=ContentType.MP3),
        media_type=MediaType.TRACK,
        stream_type=StreamType.HTTP,
        path=f"http://test.invalid/{item_id}.mp3",
        queue_id=QUEUE_ID,
    )
    queue_item.streamdetails.queue_session_id = "sess-1"
    queue_item.streamdetails.buffer = audio_buffer
    return queue_item


def _controller(items: list[QueueItem]) -> tuple[PlayerQueuesController, MagicMock]:
    """Build a bare controller holding one playing queue, and its mass mock."""
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    ctrl.logger = MagicMock()
    ctrl._queue_data = {
        QUEUE_ID: PlayerQueueData(queue=MagicMock(), items=items, session_id="sess-1")
    }
    ctrl._managed_pool = MagicMock()
    ctrl._set_transitioning = MagicMock()  # type: ignore[method-assign]
    mass = MagicMock()

    def _close_coro(target: Any, *_args: Any, **_kwargs: Any) -> None:
        # nothing runs the scheduled coroutines here
        if hasattr(target, "close"):
            target.close()

    mass.create_task = MagicMock(side_effect=_close_coro)
    ctrl.mass = mass
    return ctrl, mass


def test_removal_cancels_the_pending_next_track_work() -> None:
    """The preload, enqueue-next and prepare work of a removed queue is cancelled."""
    ctrl, mass = _controller([_item("current"), _item("next")])

    ctrl.on_player_remove(QUEUE_ID, permanent=False)

    cancelled_tasks = {call.args[0] for call in mass.cancel_task.call_args_list}
    cancelled_timers = {call.args[0] for call in mass.cancel_timer.call_args_list}
    assert {
        f"preload_next_item_{QUEUE_ID}",
        f"enqueue_next_item_{QUEUE_ID}",
        f"prepare_next_audio_buffer_{QUEUE_ID}",
    } <= cancelled_tasks
    assert f"enqueue_next_item_{QUEUE_ID}" in cancelled_timers


def test_removal_releases_the_queue_audio() -> None:
    """Every buffer and the crossfade handover of a removed queue are released."""
    items = [_item("current"), _item("next")]
    buffers = [item.streamdetails.buffer for item in items if item.streamdetails]
    ctrl, mass = _controller(items)

    ctrl.on_player_remove(QUEUE_ID, permanent=False)

    assert QUEUE_ID not in ctrl._queue_data
    for item, buffer in zip(items, buffers, strict=True):
        assert item.streamdetails is not None
        assert item.streamdetails.buffer is None
        assert buffer is not None
        buffer.clear.assert_called_once()
    mass.streams.audio.clear_crossfade_handover.assert_called_once_with(QUEUE_ID)


def test_removal_without_release_keeps_the_playing_audio() -> None:
    """A removal that keeps its audio leaves the buffers alone, but still cancels the next-track work."""
    items = [_item("current"), _item("next")]
    buffers = [item.streamdetails.buffer for item in items if item.streamdetails]
    ctrl, mass = _controller(items)

    ctrl.on_player_remove(QUEUE_ID, permanent=False, release_audio=False)

    assert QUEUE_ID not in ctrl._queue_data
    cancelled_tasks = {call.args[0] for call in mass.cancel_task.call_args_list}
    assert f"preload_next_item_{QUEUE_ID}" in cancelled_tasks
    for item, buffer in zip(items, buffers, strict=True):
        assert item.streamdetails is not None
        assert item.streamdetails.buffer is buffer
        assert buffer is not None
        buffer.clear.assert_not_called()
    mass.streams.audio.clear_crossfade_handover.assert_not_called()
