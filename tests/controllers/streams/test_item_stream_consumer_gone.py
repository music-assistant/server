"""Tests for a single-item stream whose consumer disconnected before its audio was opened."""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.enums import ContentType, MediaType
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.controllers.streams.audio import StreamsAudio

PCM_FORMAT = AudioFormat(
    content_type=ContentType.PCM_S16LE,
    codec_type=ContentType.PCM_S16LE,
    sample_rate=44100,
    bit_depth=16,
    channels=2,
)


def _audio() -> StreamsAudio:
    """Build a StreamsAudio whose item preparation reports that the consumer left."""
    audio = StreamsAudio(MagicMock())
    mass = cast("MagicMock", audio.mass)
    mass.streams.audio_analysis.get_audio_analysis = AsyncMock(return_value=None)
    mass.player_queues.queue_data_or_none.return_value = None
    audio.get_audio_buffer = AsyncMock(return_value=None)  # type: ignore[method-assign]
    audio._notify_provider_streamed = MagicMock()  # type: ignore[method-assign]
    return audio


def _queue_item() -> MagicMock:
    """Build a track queue item with plain stream details."""
    streamdetails = StreamDetails(
        provider="test--1",
        item_id="track-1",
        audio_format=PCM_FORMAT,
        media_type=MediaType.TRACK,
        duration=180,
    )
    queue_item = MagicMock()
    queue_item.queue_id = "queue-1"
    queue_item.queue_item_id = "item-1"
    queue_item.name = "Track"
    queue_item.media_type = MediaType.TRACK
    queue_item.streamdetails = streamdetails
    queue_item.available = True
    queue_item.extra_attributes = {}
    return queue_item


async def test_item_stream_ends_quietly_when_its_consumer_left() -> None:
    """No audio, no error flag and no playback report for a consumer that is gone."""
    audio = _audio()
    queue_item = _queue_item()
    consumer_connected = MagicMock(return_value=False)

    chunks = [
        chunk
        async for chunk in audio.get_queue_item_stream(
            queue_item, PCM_FORMAT, consumer_connected=consumer_connected
        )
    ]

    assert chunks == []
    prepare_call = cast("AsyncMock", audio.get_audio_buffer).await_args
    assert prepare_call is not None
    assert prepare_call.kwargs["consumer_connected"] is consumer_connected
    assert queue_item.available is True
    assert queue_item.streamdetails.stream_error is False
    cast("MagicMock", audio._notify_provider_streamed).assert_not_called()


async def test_crossfade_item_stream_skips_the_boundary_when_its_consumer_left() -> None:
    """A stream without a consumer neither loads the next item nor claims a fade into it."""
    audio = _audio()
    mass = cast("MagicMock", audio.mass)
    queue = MagicMock(queue_id="queue-1", display_name="Queue", index_in_buffer=0)
    mass.player_queues.get.return_value = queue
    mass.player_queues.load_next_queue_item = AsyncMock()
    queue_item = _queue_item()

    chunks = [
        chunk
        async for chunk in audio.get_queue_item_stream_with_smartfade(
            MagicMock(), queue_item, PCM_FORMAT, consumer_connected=lambda: False
        )
    ]

    assert chunks == []
    mass.player_queues.load_next_queue_item.assert_not_awaited()
    assert queue.index_in_buffer == 0
    assert audio._crossfade_pending == {}
    # the empty stream is not written back as the item's length
    assert queue_item.streamdetails.duration == 180
