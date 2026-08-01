"""Tests that PlayerMedia separates the media length from the served stream length."""

from __future__ import annotations

from unittest.mock import MagicMock

from music_assistant_models.media_items import AudioFormat
from music_assistant_models.queue_item import QueueItem
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData

QUEUE_ID = "q1"
FULL_DURATION = 300


def _controller_with_item(
    seek_position: int = 0, streamdetails_duration: int | None = FULL_DURATION
) -> tuple[PlayerQueuesController, QueueItem]:
    """Build a bare controller holding a single track, optionally seeked into."""
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    queue_item = QueueItem(
        queue_id=QUEUE_ID,
        queue_item_id="item1",
        name="Track 1",
        duration=FULL_DURATION,
    )
    queue_item.streamdetails = StreamDetails(
        provider="test",
        item_id="item1",
        audio_format=AudioFormat(),
        duration=streamdetails_duration,
        seek_position=seek_position,
    )
    queue_data = PlayerQueueData(queue=MagicMock())
    queue_data.session_id = "session1"
    ctrl._queue_data = {QUEUE_ID: queue_data}
    ctrl.mass = MagicMock()
    return ctrl, queue_item


async def test_unseeked_item_has_no_separate_stream_duration() -> None:
    """Without a seek the stream matches the media item, so stream_duration stays unset."""
    ctrl, queue_item = _controller_with_item()

    media = await ctrl.player_media_from_queue_item(queue_item)

    assert media.duration == FULL_DURATION
    assert media.stream_duration is None


async def test_seeked_item_keeps_full_duration_and_reports_stream_duration() -> None:
    """A seek shortens only the stream; duration stays the full media length."""
    ctrl, queue_item = _controller_with_item(seek_position=120)

    media = await ctrl.player_media_from_queue_item(queue_item)

    assert media.duration == FULL_DURATION
    assert media.stream_duration == FULL_DURATION - 120


async def test_item_without_streamdetails_duration_falls_back_to_item_duration() -> None:
    """The queue item's own duration is used when streamdetails carry none."""
    ctrl, queue_item = _controller_with_item(seek_position=60, streamdetails_duration=None)

    media = await ctrl.player_media_from_queue_item(queue_item)

    assert media.duration == FULL_DURATION
    assert media.stream_duration == FULL_DURATION - 60
