"""
Tests that resolved stream details record the playback session that claimed them.

A queue's audio teardown uses this stamp to tell the audio of the session it is
tearing down from the audio of a session that started after it.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.queue_item import QueueItem
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.controllers.player_queues.state import PlayerQueueData
from music_assistant.controllers.streams.audio import StreamsAudio

QUEUE_ID = "queue-1"
INSTANCE = "service--1"


def _queue_item() -> QueueItem:
    """Build a queue item carrying unexpired stream details, so no provider is consulted."""
    queue_item = QueueItem(
        queue_id=QUEUE_ID, queue_item_id="queue-item-1", name="Track", duration=30
    )
    queue_item.streamdetails = StreamDetails(
        provider=INSTANCE,
        item_id="item-1",
        audio_format=AudioFormat(content_type=ContentType.MP3),
        media_type=MediaType.TRACK,
        stream_type=StreamType.HTTP,
        path="http://test.invalid/item.mp3",
        duration=30,
    )
    return queue_item


def _audio(session_id: str | None) -> StreamsAudio:
    """Build a streams-audio controller whose queue is in the given playback session."""
    mass = MagicMock()
    mass.player_queues.queue_data_or_none.return_value = (
        PlayerQueueData(queue=MagicMock(), session_id=session_id)
        if session_id is not None
        else None
    )
    mass.streams.get_config_value.return_value = -17
    return StreamsAudio(mass)


async def test_stream_details_record_the_session_that_resolved_them() -> None:
    """The queue's current session is stamped on the details it plays."""
    queue_item = _queue_item()

    result = await _audio("sess-1").get_stream_details(queue_item)

    assert result.queue_session_id == "sess-1"


async def test_reused_stream_details_move_to_the_session_that_claimed_them() -> None:
    """Details left behind by an earlier session belong to whoever picks them up next."""
    queue_item = _queue_item()
    queue_item.streamdetails.queue_session_id = "sess-1"  # type: ignore[union-attr]

    result = await _audio("sess-2").get_stream_details(queue_item)

    assert result.queue_session_id == "sess-2"


async def test_an_unregistered_queue_leaves_the_session_unset() -> None:
    """Nothing owns the audio of a queue the controller no longer holds a record for."""
    queue_item = _queue_item()
    queue_item.streamdetails.queue_session_id = "sess-1"  # type: ignore[union-attr]

    result = await _audio(None).get_stream_details(queue_item)

    assert result.queue_session_id is None
