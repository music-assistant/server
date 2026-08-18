"""Tests that starting an item releases the source slots its own queue no longer needs."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.queue_item import QueueItem
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData
from music_assistant.controllers.streams.audio_buffer import AudioBuffer
from music_assistant.models.music_provider import MusicProvider

QUEUE_ID = "q1"
LIMITED = "limited--1"
UNLIMITED = "local--1"


def _item(queue_id: str, item_id: str, provider: str | None, buffering: bool = True) -> QueueItem:
    """
    Build a queue item, optionally with a buffer attached to its stream details.

    :param queue_id: The queue the item belongs to.
    :param item_id: The queue item id.
    :param provider: Provider instance owning the source, or None for an item without details.
    :param buffering: Whether the attached buffer is still filling from its source.
    """
    queue_item = QueueItem(queue_id=queue_id, queue_item_id=item_id, name=item_id, duration=180)
    if provider is None:
        return queue_item
    audio_buffer = MagicMock(spec=AudioBuffer)
    audio_buffer.is_buffering = buffering
    audio_buffer.clear = AsyncMock()
    queue_item.streamdetails = StreamDetails(
        provider=provider,
        item_id=item_id,
        audio_format=AudioFormat(content_type=ContentType.MP3),
        media_type=MediaType.TRACK,
        stream_type=StreamType.HTTP,
        path=f"http://test.invalid/{item_id}.mp3",
    )
    queue_item.streamdetails.buffer = audio_buffer
    return queue_item


def _controller(*queues: tuple[str, list[QueueItem]]) -> PlayerQueuesController:
    """Build a bare controller holding the given queues and their items."""
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    ctrl.logger = MagicMock()
    ctrl._queue_data = {
        queue_id: PlayerQueueData(queue=MagicMock(), items=items, session_id=queue_id)
        for queue_id, items in queues
    }
    limited = MagicMock(spec=MusicProvider)
    limited.name = "Limited"
    limited.max_concurrent_streams = 2
    unlimited = MagicMock(spec=MusicProvider)
    unlimited.name = "Local"
    unlimited.max_concurrent_streams = None
    providers = {LIMITED: limited, UNLIMITED: unlimited}
    ctrl.mass = MagicMock()
    ctrl.mass.get_provider.side_effect = lambda instance, **_kwargs: providers.get(instance)
    return ctrl


async def test_starting_an_item_aborts_the_other_filling_sources_of_its_queue() -> None:
    """A still-filling source of another item in the same queue hands its slot over."""
    target = _item(QUEUE_ID, "target", LIMITED)
    filling = _item(QUEUE_ID, "filling", LIMITED)
    ctrl = _controller((QUEUE_ID, [filling, target]))

    await ctrl._abort_superseded_source_buffers(target)

    assert filling.streamdetails is not None
    assert filling.streamdetails.buffer is None
    assert target.streamdetails is not None
    assert target.streamdetails.buffer is not None


async def test_completed_and_unlimited_sources_are_left_alone() -> None:
    """Only a source that still holds a capped provider slot is worth aborting."""
    target = _item(QUEUE_ID, "target", LIMITED)
    completed = _item(QUEUE_ID, "completed", LIMITED, buffering=False)
    unlimited = _item(QUEUE_ID, "unlimited", UNLIMITED)
    ctrl = _controller((QUEUE_ID, [completed, unlimited, target]))

    await ctrl._abort_superseded_source_buffers(target)

    for item in (completed, unlimited):
        assert item.streamdetails is not None
        assert item.streamdetails.buffer is not None
        assert item.streamdetails.buffer.clear.await_count == 0


async def test_other_queues_keep_their_sources() -> None:
    """Playback on one queue never takes a source away from another queue."""
    target = _item(QUEUE_ID, "target", LIMITED)
    other = _item("q2", "other", LIMITED)
    ctrl = _controller((QUEUE_ID, [target]), ("q2", [other]))

    await ctrl._abort_superseded_source_buffers(target)

    assert other.streamdetails is not None
    assert other.streamdetails.buffer is not None
    assert other.streamdetails.buffer.clear.await_count == 0
