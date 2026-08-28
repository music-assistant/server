"""
Tests for which buffers a queue's audio cleanup is allowed to clear.

A stop tears down the audio of the session it was issued for. When playback restarts
before that teardown gets to run - only possible once the playback lock gives up on a
wedged holder - the replacement session's producers must survive it, while the stopped
session's producers still have to be killed.
"""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.queue_item import QueueItem
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData
from music_assistant.controllers.streams.audio_buffer import AudioBuffer

QUEUE_ID = "q1"


def _item(item_id: str, session_id: str | None) -> QueueItem:
    """
    Build a queue item with a buffer attached, owned by the given playback session.

    :param item_id: The queue item id.
    :param session_id: Session to stamp on the stream details, or None to leave unstamped.
    """
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
    queue_item.streamdetails.queue_session_id = session_id
    queue_item.streamdetails.buffer = audio_buffer
    return queue_item


def _controller(items: list[QueueItem]) -> PlayerQueuesController:
    """Build a bare controller holding one queue with the given items."""
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    ctrl.logger = MagicMock()
    ctrl._queue_data = {
        QUEUE_ID: PlayerQueueData(queue=MagicMock(), items=items, session_id="sess-2")
    }
    ctrl.mass = MagicMock()
    return ctrl


async def test_a_stop_leaves_a_newer_sessions_buffers_alone() -> None:
    """Playback that restarted before the teardown ran keeps the audio it prepared."""
    stopped = _item("stopped", "sess-1")
    replacement = _item("replacement", "sess-2")
    ctrl = _controller([stopped, replacement])

    await ctrl._cleanup_queue_audio_data(QUEUE_ID, "sess-1")

    assert stopped.streamdetails is not None
    assert stopped.streamdetails.buffer is None
    assert replacement.streamdetails is not None
    assert replacement.streamdetails.buffer is not None
    replacement.streamdetails.buffer.clear.assert_not_awaited()


async def test_a_stop_still_kills_every_buffer_of_its_own_session() -> None:
    """The stopped session's producers are what a stop exists to release."""
    playing = _item("playing", "sess-1")
    preloaded = _item("preloaded", "sess-1")
    ctrl = _controller([playing, preloaded])

    await ctrl._cleanup_queue_audio_data(QUEUE_ID, "sess-1")

    for item in (playing, preloaded):
        assert item.streamdetails is not None
        assert item.streamdetails.buffer is None


async def test_a_buffer_without_a_session_is_cleared_by_a_stop() -> None:
    """
    Audio that cannot be proven to belong to a later session is torn down.

    Leaving it would keep a producer alive - and its provider's stream slot with it -
    which is exactly what a stop has to prevent.
    """
    unstamped = _item("unstamped", None)
    ctrl = _controller([unstamped])

    await ctrl._cleanup_queue_audio_data(QUEUE_ID, "sess-1")

    assert unstamped.streamdetails is not None
    assert unstamped.streamdetails.buffer is None


async def test_without_a_session_every_buffer_is_cleared() -> None:
    """A clear/replace drops the items themselves, so all their audio goes with them."""
    items = [_item("a", "sess-1"), _item("b", "sess-2"), _item("c", None)]
    ctrl = _controller(items)

    await ctrl._cleanup_queue_audio_data(QUEUE_ID)

    for item in items:
        assert item.streamdetails is not None
        assert item.streamdetails.buffer is None


async def test_a_buffer_attached_while_it_is_released_is_kept() -> None:
    """
    Releasing a buffer suspends, and what a later session attaches then must survive.

    Cancelling the producer waits on the producer task, so the replacement session gets to
    run and attach its own buffer to the same stream details while that is in flight.
    """
    stopped = _item("stopped", "sess-1")
    ctrl = _controller([stopped])
    assert stopped.streamdetails is not None
    replacement_buffer = MagicMock(spec=AudioBuffer)

    async def _attach_a_replacement() -> None:
        # stands in for the new session claiming this item while the old buffer is released
        stopped.streamdetails.buffer = replacement_buffer  # type: ignore[union-attr]

    stopped.streamdetails.buffer.clear = AsyncMock(side_effect=_attach_a_replacement)

    await ctrl._cleanup_queue_audio_data(QUEUE_ID, "sess-1")

    assert stopped.streamdetails.buffer is replacement_buffer


async def test_pending_crossfade_data_is_always_dropped() -> None:
    """A restarted session starts its first track from scratch, with nothing to fade from."""
    ctrl = _controller([_item("a", "sess-2")])

    await ctrl._cleanup_queue_audio_data(QUEUE_ID, "sess-1")

    clear_crossfade_data = cast("MagicMock", ctrl.mass.streams.audio.clear_crossfade_data)
    clear_crossfade_data.assert_called_once_with(QUEUE_ID)
