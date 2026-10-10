"""Tests for the direct-PCM stream helper on the streams controller."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.player import PlayerMedia
from music_assistant_models.queue_item import QueueItem
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.controllers.streams.controller import StreamsController

PCM_FORMAT = AudioFormat(
    content_type=ContentType.PCM_S16LE,
    sample_rate=44100,
    bit_depth=16,
    channels=2,
)

QUEUE_ID = "player-1"
QUEUE_ITEM_ID = "item-1"


def _queue_item(seek_position: int) -> QueueItem:
    """Build an audiobook queue item whose streamdetails carry the given seek position."""
    return QueueItem(
        queue_id=QUEUE_ID,
        queue_item_id=QUEUE_ITEM_ID,
        name="Some Audiobook",
        duration=7200,
        streamdetails=StreamDetails(
            provider="builtin",
            item_id="book-1",
            audio_format=PCM_FORMAT,
            media_type=MediaType.AUDIOBOOK,
            stream_type=StreamType.HTTP,
            path="http://example.com/book.mp3",
            duration=7200,
            can_seek=True,
            allow_seek=True,
            queue_id=QUEUE_ID,
            seek_position=seek_position,
        ),
    )


def _controller(queue_item: QueueItem) -> tuple[StreamsController, dict[str, Any]]:
    """
    Build a streams controller that records the kwargs of the single item stream call.

    The queue itself is absent, which keeps the request on the non-flow (single item)
    branch: crossfade and audio overlay both need a queue to force flow mode.

    :param queue_item: The item the controller resolves the stream request to.
    """
    mass = MagicMock()
    mass.config.get_raw_core_config_value.return_value = "GLOBAL"
    mass.player_queues.get.return_value = None
    mass.player_queues.get_item.return_value = queue_item
    controller = StreamsController(mass)
    call_kwargs: dict[str, Any] = {}

    def _record(**kwargs: Any) -> object:
        call_kwargs.update(kwargs)
        return object()

    controller.audio = MagicMock()
    controller.audio.get_queue_item_stream.side_effect = _record
    return controller, call_kwargs


@pytest.mark.parametrize("seek_position", [1800, 0])
def test_single_item_stream_forwards_the_seek_position(seek_position: int) -> None:
    """A resumed (or seeked) item is streamed from its seek position, not from the start."""
    queue_item = _queue_item(seek_position)
    controller, call_kwargs = _controller(queue_item)

    controller.get_stream(
        PlayerMedia(
            uri="library://audiobook/1",
            media_type=MediaType.AUDIOBOOK,
            source_id=QUEUE_ID,
            queue_item_id=QUEUE_ITEM_ID,
        ),
        PCM_FORMAT,
    )

    assert call_kwargs["seek_position"] == seek_position


async def _pcm_chunks(chunks: tuple[bytes, ...]) -> AsyncGenerator[bytes]:
    """Yield the given PCM chunks one at a time."""
    for chunk in chunks:
        yield chunk


def _pcm_stream_controller(
    chunks: tuple[bytes, ...] = (b"chunk-1", b"chunk-2"),
) -> StreamsController:
    """Build a controller whose single item stream yields the given PCM chunks."""
    controller, _ = _controller(_queue_item(0))
    audio = cast("Any", controller.audio)
    audio.get_queue_item_stream.side_effect = lambda **_kwargs: _pcm_chunks(chunks)
    return controller


def _media() -> PlayerMedia:
    """Build the player media that resolves to the audiobook queue item."""
    return PlayerMedia(
        uri="library://audiobook/1",
        media_type=MediaType.AUDIOBOOK,
        source_id=QUEUE_ID,
        queue_item_id=QUEUE_ITEM_ID,
    )


@pytest.mark.asyncio
async def test_direct_pcm_stream_counts_as_an_active_output_stream() -> None:
    """A player consuming raw PCM registers as playing, so analysis yields CPU to it."""
    controller = _pcm_stream_controller()

    stream = controller.get_stream(
        PlayerMedia(
            uri="library://audiobook/1",
            media_type=MediaType.AUDIOBOOK,
            source_id=QUEUE_ID,
            queue_item_id=QUEUE_ITEM_ID,
        ),
        PCM_FORMAT,
    )
    assert controller.output_stream_active() is False

    chunks = []
    async for chunk in stream:
        chunks.append(chunk)
        assert controller.output_stream_active() is True

    assert chunks == [b"chunk-1", b"chunk-2"]
    assert controller.output_stream_active() is False


@pytest.mark.asyncio
async def test_abandoned_direct_pcm_stream_releases_the_gauge() -> None:
    """A consumer that stops mid-stream releases its count, so analysis regains its budget."""
    controller = _pcm_stream_controller()

    stream = controller.get_stream(
        PlayerMedia(
            uri="library://audiobook/1",
            media_type=MediaType.AUDIOBOOK,
            source_id=QUEUE_ID,
            queue_item_id=QUEUE_ITEM_ID,
        ),
        PCM_FORMAT,
    )
    assert await anext(stream) == b"chunk-1"
    assert controller.output_stream_active() is True

    await stream.aclose()
    assert controller.output_stream_active() is False


@pytest.mark.asyncio
async def test_direct_pcm_stream_marks_the_item_served_on_its_first_chunk() -> None:
    """The item counts as served once its first chunk reached the consumer, and only once."""
    controller = _pcm_stream_controller()
    mark_item_served = cast("MagicMock", controller.mass.player_queues.mark_item_served)

    stream = controller.get_stream(_media(), PCM_FORMAT)
    mark_item_served.assert_not_called()

    assert await anext(stream) == b"chunk-1"
    mark_item_served.assert_called_once_with(QUEUE_ID, QUEUE_ITEM_ID)

    assert await anext(stream) == b"chunk-2"
    await stream.aclose()
    mark_item_served.assert_called_once_with(QUEUE_ID, QUEUE_ITEM_ID)


@pytest.mark.asyncio
async def test_direct_pcm_stream_without_audio_marks_nothing_served() -> None:
    """A source that produces no audio leaves the item unserved."""
    controller = _pcm_stream_controller(chunks=())

    stream = controller.get_stream(_media(), PCM_FORMAT)

    assert [chunk async for chunk in stream] == []
    cast("MagicMock", controller.mass.player_queues.mark_item_served).assert_not_called()


@pytest.mark.asyncio
async def test_unconsumed_direct_pcm_stream_marks_nothing_served() -> None:
    """A stream the player never pulled from leaves the item unserved."""
    controller = _pcm_stream_controller()

    stream = controller.get_stream(_media(), PCM_FORMAT)
    await stream.aclose()

    cast("MagicMock", controller.mass.player_queues.mark_item_served).assert_not_called()
