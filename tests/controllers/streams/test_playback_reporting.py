"""Tests for the playback start and end reports a queue item stream sends to its provider."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Coroutine
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import ContentType, MediaType, ProviderType
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamDetails

import music_assistant.controllers.streams.audio as audio_mod
from music_assistant.controllers.streams.audio import StreamsAudio

PCM_FORMAT = AudioFormat(
    content_type=ContentType.PCM_S16LE,
    codec_type=ContentType.PCM_S16LE,
    sample_rate=44100,
    bit_depth=16,
    channels=2,
)
# one second of audio per chunk
CHUNK = b"\x00" * PCM_FORMAT.pcm_sample_size


@pytest.fixture
def audio(monkeypatch: pytest.MonkeyPatch) -> tuple[StreamsAudio, MagicMock]:
    """Build a StreamsAudio serving three seconds of audio from a fake buffer."""

    class _FakeBuffer:
        has_error = False
        pcm_format = PCM_FORMAT

        @classmethod
        async def get_buffer(cls, **_kwargs: Any) -> _FakeBuffer:
            return cls()

        async def get_stream(self, **_kwargs: Any) -> AsyncGenerator[bytes]:
            for _ in range(3):
                yield CHUNK

    monkeypatch.setattr(audio_mod, "AudioBuffer", _FakeBuffer)

    controller = StreamsAudio(MagicMock())
    mass = cast("MagicMock", controller.mass)
    mass.create_task.side_effect = lambda coro, *_a, **_kw: asyncio.ensure_future(
        cast("Coroutine[Any, Any, Any]", coro)
    )
    mass.streams.audio_analysis.get_audio_analysis = AsyncMock(return_value=None)
    mass.config.get_player_config = AsyncMock(return_value=MagicMock())
    mass.player_queues.queue_data_or_none.return_value = None
    provider = MagicMock()
    provider.type = ProviderType.MUSIC
    provider.on_stream_started = AsyncMock()
    provider.on_streamed = AsyncMock()
    mass.get_provider.return_value = provider
    return controller, provider


def _make_queue_item() -> MagicMock:
    streamdetails = StreamDetails(
        provider="test_provider",
        item_id="track_a",
        audio_format=PCM_FORMAT,
        media_type=MediaType.TRACK,
        duration=3,
    )
    queue_item = MagicMock()
    queue_item.media_type = MediaType.TRACK
    queue_item.streamdetails = streamdetails
    queue_item.name = "Track A"
    return queue_item


async def _stream(controller: StreamsAudio, queue_item: MagicMock, take: int | None = None) -> None:
    """Consume the queue item stream, or only its first chunks when ``take`` is given."""
    stream = controller.get_queue_item_stream(queue_item, PCM_FORMAT)
    received = 0
    async for _ in stream:
        received += 1
        if take is not None and received >= take:
            await stream.aclose()
            break
    # the reports are scheduled as tasks
    await asyncio.sleep(0)


async def test_played_item_reports_one_start_and_one_end(
    audio: tuple[StreamsAudio, MagicMock],
) -> None:
    """A stream reports the start with its first chunk and the end once it ran out."""
    controller, provider = audio
    queue_item = _make_queue_item()

    await _stream(controller, queue_item)

    provider.on_stream_started.assert_awaited_once_with(queue_item.streamdetails)
    provider.on_streamed.assert_awaited_once_with(queue_item.streamdetails)
    assert queue_item.streamdetails.seconds_streamed == 3


async def test_later_streams_of_the_same_playback_report_no_start(
    audio: tuple[StreamsAudio, MagicMock],
) -> None:
    """A seek or reconnect reuses the streamdetails and starts no second playback."""
    controller, provider = audio
    queue_item = _make_queue_item()

    await _stream(controller, queue_item)
    await _stream(controller, queue_item)

    provider.on_stream_started.assert_awaited_once()
    assert provider.on_streamed.await_count == 2


async def test_short_stream_that_reported_the_start_also_reports_the_end(
    audio: tuple[StreamsAudio, MagicMock],
) -> None:
    """A playback stopped right after it started still gets its end report."""
    controller, provider = audio
    queue_item = _make_queue_item()

    await _stream(controller, queue_item, take=1)

    provider.on_stream_started.assert_awaited_once()
    provider.on_streamed.assert_awaited_once()
    assert queue_item.streamdetails.seconds_streamed == 1


async def test_short_stream_without_a_start_reports_nothing(
    audio: tuple[StreamsAudio, MagicMock],
) -> None:
    """A short follow-up stream (e.g. a crossfade body cut off early) stays unreported."""
    controller, provider = audio
    queue_item = _make_queue_item()
    queue_item.streamdetails.seconds_streamed = 3

    await _stream(controller, queue_item, take=1)

    provider.on_stream_started.assert_not_awaited()
    provider.on_streamed.assert_not_awaited()


async def test_plugin_provider_items_are_not_reported(
    audio: tuple[StreamsAudio, MagicMock],
) -> None:
    """The playback callbacks exist on music providers only."""
    controller, provider = audio
    provider.type = ProviderType.PLUGIN
    queue_item = _make_queue_item()

    await _stream(controller, queue_item)

    provider.on_stream_started.assert_not_awaited()
    provider.on_streamed.assert_not_awaited()
