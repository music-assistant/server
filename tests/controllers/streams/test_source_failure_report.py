"""Tests that the stream of a queue item tells its queue when the source of the item gave no audio."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import ContentType, CrossfadeMode, MediaType
from music_assistant_models.errors import AudioError, QueueEmpty
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.controllers.streams.audio import StreamsAudio
from music_assistant.models.music_provider import ProviderStreamLimitError

PCM_FORMAT = AudioFormat(
    content_type=ContentType.PCM_S16LE,
    codec_type=ContentType.PCM_S16LE,
    sample_rate=44100,
    bit_depth=16,
    channels=2,
)
SESSION = "session-1"
NO_STREAM_SLOT = ProviderStreamLimitError(
    cast("Any", SimpleNamespace(name="Provider", instance_id="p--1", max_concurrent_streams=1)),
    5.0,
)


def _audio() -> StreamsAudio:
    """Build a StreamsAudio around a mocked server."""
    audio = StreamsAudio(MagicMock())
    mass = cast("MagicMock", audio.mass)
    mass.streams.audio_analysis.get_audio_analysis = AsyncMock(return_value=None)
    mass.player_queues.queue_data_or_none.return_value = None
    audio._notify_provider_streamed = MagicMock()  # type: ignore[method-assign]
    return audio


def _queue_item() -> MagicMock:
    """Build a track queue item with plain stream details."""
    queue_item = MagicMock()
    queue_item.queue_id = "queue-1"
    queue_item.queue_item_id = "item-1"
    queue_item.name = "Track"
    queue_item.media_type = MediaType.TRACK
    queue_item.streamdetails = StreamDetails(
        provider="test--1",
        item_id="track-1",
        audio_format=PCM_FORMAT,
        media_type=MediaType.TRACK,
        duration=180,
    )
    queue_item.available = True
    queue_item.extra_attributes = {}
    return queue_item


def _buffer(error: AudioError, source_failed: bool) -> MagicMock:
    """Build a buffer that delivers one chunk and then fails with the given error."""

    async def _stream(**_kwargs: Any) -> AsyncGenerator[bytes]:
        yield b"\0" * PCM_FORMAT.pcm_sample_size
        raise error

    audio_buffer = MagicMock()
    audio_buffer.get_stream = _stream
    audio_buffer.has_error = source_failed
    return audio_buffer


async def _drain(audio: StreamsAudio, queue_item: MagicMock, **kwargs: Any) -> None:
    async for _chunk in audio.get_queue_item_stream(
        queue_item, PCM_FORMAT, raise_on_error=False, session_id=SESSION, **kwargs
    ):
        pass


@pytest.mark.parametrize(
    ("error", "reported"),
    [
        (AudioError("Timeout waiting for audio data"), True),
        # a capacity miss is no failure of the source
        (NO_STREAM_SLOT, False),
    ],
)
async def test_a_source_that_gives_no_audio_is_reported(error: AudioError, reported: bool) -> None:
    """A buffer that does not get ready reports the item, a taken stream slot does not."""
    audio = _audio()
    audio.get_audio_buffer = AsyncMock(side_effect=error)  # type: ignore[method-assign]
    queue_item = _queue_item()

    await _drain(audio, queue_item)

    report = cast("MagicMock", audio.mass).player_queues.report_source_failure
    if reported:
        report.assert_called_once_with(queue_item, SESSION)
    else:
        report.assert_not_called()


async def test_a_prepared_buffer_that_is_gone_is_not_reported() -> None:
    """A crossfade buffer that was released is our own doing, not a failure of the source."""
    audio = _audio()
    queue_item = _queue_item()

    await _drain(audio, queue_item, prepared_buffer=MagicMock())

    cast("MagicMock", audio.mass).player_queues.report_source_failure.assert_not_called()


@pytest.mark.parametrize("source_failed", [True, False])
async def test_a_source_that_fails_mid_track_is_reported(source_failed: bool) -> None:
    """A source that stalls after some audio counts; failing to read delivered audio does not."""
    audio = _audio()
    audio_buffer = _buffer(AudioError("Source stalled: no audio for 20s"), source_failed)
    audio.get_audio_buffer = AsyncMock(return_value=audio_buffer)  # type: ignore[method-assign]
    queue_item = _queue_item()

    await _drain(audio, queue_item)

    report = cast("MagicMock", audio.mass).player_queues.report_source_failure
    if source_failed:
        report.assert_called_once_with(queue_item, SESSION)
    else:
        report.assert_not_called()


@pytest.mark.parametrize("stream_failed", [False, True])
async def test_a_flow_item_without_audio_is_reported_once(stream_failed: bool) -> None:
    """A flow item whose source ended without audio is reported; an error was reported already."""
    mass = MagicMock()
    streamdetails = SimpleNamespace(
        buffer=None,
        fade_in=False,
        stream_error=False,
        tail_overlap=None,
        uri="test://item",
        seek_position=0,
        duration=180,
        is_realtime=False,
    )
    queue_item = SimpleNamespace(
        queue_id="queue-1",
        queue_item_id="item-1",
        name="item",
        media_type=MediaType.AUDIOBOOK,
        streamdetails=streamdetails,
        extra_attributes={},
    )
    mass.player_queues.queue_data.return_value = SimpleNamespace(
        session_id=SESSION, flow_mode_stream_log=[]
    )
    mass.player_queues.load_next_queue_item.side_effect = QueueEmpty
    mass.player_queues.get_active_queue.return_value = None
    mass.streams.get_crossfade_mode.return_value = CrossfadeMode.DISABLED
    mass.config.get_raw_core_config_value.return_value = 0
    audio = StreamsAudio(cast("Any", mass))

    async def _item_stream(*_args: object, **_kwargs: object) -> AsyncGenerator[bytes]:
        # an error inside the item stream flags its stream details, and is reported there
        streamdetails.stream_error = stream_failed
        chunks: tuple[bytes, ...] = ()
        for chunk in chunks:
            yield chunk

    audio.get_queue_item_stream = _item_stream  # type: ignore[method-assign]
    queue = SimpleNamespace(
        queue_id="queue-1",
        display_name="Queue",
        flow_mode=False,
        overlay_enabled=False,
        overlay_source=None,
    )

    async for _chunk in audio.get_queue_flow_stream(
        cast("Any", queue), cast("Any", queue_item), PCM_FORMAT, session_id=SESSION
    ):
        pass

    report = mass.player_queues.report_source_failure
    if stream_failed:
        report.assert_not_called()
    else:
        report.assert_called_once_with(queue_item, SESSION)
