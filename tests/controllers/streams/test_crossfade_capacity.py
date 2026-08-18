"""Tests for crossfade degradation when incoming source capacity is unavailable."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import ContentType, CrossfadeMode, MediaType, StreamType
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.controllers.streams.audio import (
    MIN_CROSSFADE_FALLBACK_DURATION,
    StreamsAudio,
)
from music_assistant.controllers.streams.audio_buffer import AudioBuffer
from music_assistant.controllers.streams.smart_fades.helpers import SMART_CROSSFADE_DURATION


def _streamdetails(audio_buffer: AudioBuffer | None) -> StreamDetails:
    """Build incoming track details with an optional prepared buffer."""
    streamdetails = StreamDetails(
        provider="test--1",
        item_id="track-1",
        audio_format=AudioFormat(content_type=ContentType.FLAC),
        media_type=MediaType.TRACK,
        stream_type=StreamType.HTTP,
        path="http://test.invalid/track.flac",
        duration=180,
    )
    streamdetails.buffer = audio_buffer
    return streamdetails


def _buffer(duration_available: float, ready: bool) -> AudioBuffer:
    """Build a valid buffer with the requested resident duration."""
    audio_buffer = MagicMock(spec=AudioBuffer)
    audio_buffer.has_error = False
    audio_buffer.is_valid.return_value = True
    audio_buffer.duration_available = duration_available
    audio_buffer.ready = MagicMock()
    audio_buffer.ready.is_set.return_value = ready
    return audio_buffer


def test_ready_incoming_buffer_keeps_smart_crossfade() -> None:
    """A fully resident incoming buffer keeps the requested Smart Fade."""
    audio = StreamsAudio(MagicMock())

    mode, duration = audio._select_buffered_crossfade(
        _streamdetails(_buffer(SMART_CROSSFADE_DURATION, ready=True)),
        CrossfadeMode.SMART_CROSSFADE,
        standard_crossfade_duration=8,
    )

    assert mode == CrossfadeMode.SMART_CROSSFADE
    assert duration == SMART_CROSSFADE_DURATION


def test_partial_incoming_buffer_degrades_to_short_standard_crossfade() -> None:
    """Five resident seconds are crossfaded without waiting for more source PCM."""
    audio = StreamsAudio(MagicMock())
    available_seconds = MIN_CROSSFADE_FALLBACK_DURATION + 2

    mode, duration = audio._select_buffered_crossfade(
        _streamdetails(_buffer(available_seconds, ready=False)),
        CrossfadeMode.SMART_CROSSFADE,
        standard_crossfade_duration=8,
    )

    assert mode == CrossfadeMode.STANDARD_CROSSFADE
    assert duration == available_seconds


def test_crossfade_resident_duration_accounts_for_playback_speed() -> None:
    """Fast playback cannot claim more post-filter overlap than resident PCM can produce."""
    audio = StreamsAudio(MagicMock())

    mode, duration = audio._select_buffered_crossfade(
        _streamdetails(_buffer(8, ready=False)),
        CrossfadeMode.SMART_CROSSFADE,
        standard_crossfade_duration=8,
        playback_speed=2.0,
    )

    assert mode == CrossfadeMode.DISABLED
    assert duration == 0


@pytest.mark.parametrize(
    "audio_buffer",
    [
        None,
        _buffer(MIN_CROSSFADE_FALLBACK_DURATION - 0.5, ready=False),
    ],
)
def test_unprepared_incoming_buffer_disables_crossfade(
    audio_buffer: AudioBuffer | None,
) -> None:
    """An unusable incoming buffer falls back to gapless/no-crossfade playback."""
    audio = StreamsAudio(MagicMock())

    mode, duration = audio._select_buffered_crossfade(
        _streamdetails(audio_buffer),
        CrossfadeMode.SMART_CROSSFADE,
        standard_crossfade_duration=8,
    )

    assert mode == CrossfadeMode.DISABLED
    assert duration == 0


async def test_unprepared_next_track_flushes_outgoing_tail_without_opening_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing incoming PCM emits the complete outgoing track without a blocking fade fetch."""
    pcm_format = AudioFormat(
        content_type=ContentType.PCM_S16LE,
        sample_rate=8000,
        bit_depth=16,
        channels=2,
    )
    current_details = SimpleNamespace(
        duration=16,
        seek_position=0,
        seconds_streamed=0,
        uri="test://current",
    )
    next_details = SimpleNamespace(
        audio_format=pcm_format,
        buffer=None,
        duration=16,
        seek_position=0,
        uri="test://next",
    )
    current_item = SimpleNamespace(
        queue_id="queue-1",
        queue_item_id="current",
        name="Current",
        streamdetails=current_details,
        extra_attributes={},
    )
    next_item = SimpleNamespace(
        queue_id="queue-1",
        queue_item_id="next",
        name="Next",
        streamdetails=next_details,
        extra_attributes={},
        available=True,
    )
    queue = SimpleNamespace(
        queue_id="queue-1",
        display_name="Queue",
        index_in_buffer=0,
    )
    player = SimpleNamespace(player_id="player-1", name="Player")
    mass = MagicMock()
    mass.player_queues.get.return_value = queue
    mass.player_queues.load_next_queue_item = AsyncMock(return_value=next_item)
    mass.player_queues.index_by_id.return_value = 1
    audio = StreamsAudio(cast("Any", mass))
    audio.setup()
    audio.select_pcm_format = AsyncMock(return_value=pcm_format)  # type: ignore[method-assign]
    audio.crossfade_allowed = MagicMock(return_value=True)  # type: ignore[method-assign]
    build = AsyncMock()
    monkeypatch.setattr(audio.smart_fades_mixer, "build", build)

    async def _current_stream(
        queue_item: object,
        *_args: object,
        **_kwargs: object,
    ) -> AsyncGenerator[bytes]:
        if queue_item is not current_item:
            pytest.fail("The incoming source was opened during crossfade fallback")
        yield bytes(pcm_format.pcm_sample_size * 8)
        yield bytes(pcm_format.pcm_sample_size * 8)

    monkeypatch.setattr(audio, "get_queue_item_stream", _current_stream)
    stream = audio.get_queue_item_stream_with_smartfade(
        cast("Any", player),
        cast("Any", current_item),
        pcm_format,
        crossfade_mode=CrossfadeMode.STANDARD_CROSSFADE,
        standard_crossfade_duration=8,
    )

    output = b"".join([chunk async for chunk in stream])

    assert len(output) == pcm_format.pcm_sample_size * 16
    assert next_item.available
    build.assert_not_awaited()


@pytest.mark.parametrize(
    ("playback_speed", "resident_media_duration"),
    [(0.5, 2.5), (2.0, 10.0)],
)
async def test_partial_crossfade_resumes_at_consumed_media_time(
    monkeypatch: pytest.MonkeyPatch,
    playback_speed: float,
    resident_media_duration: float,
) -> None:
    """Crossfade output bytes resume the raw source at the matching media-time position."""
    pcm_format = AudioFormat(
        content_type=ContentType.PCM_S16LE,
        sample_rate=8000,
        bit_depth=16,
        channels=2,
    )
    current_details = SimpleNamespace(
        duration=16,
        seek_position=0,
        seconds_streamed=0,
        uri="test://current",
    )
    next_details = SimpleNamespace(
        audio_format=pcm_format,
        buffer=_buffer(resident_media_duration, ready=False),
        duration=16,
        seek_position=0,
        uri="test://next",
        volume_normalization_mode=None,
    )
    current_item = SimpleNamespace(
        queue_id="queue-1",
        queue_item_id="current",
        name="Current",
        streamdetails=current_details,
        extra_attributes={},
    )
    next_item = SimpleNamespace(
        queue_id="queue-1",
        queue_item_id="next",
        name="Next",
        streamdetails=next_details,
        extra_attributes={"playback_speed": playback_speed},
        available=True,
    )
    queue = SimpleNamespace(
        queue_id="queue-1",
        display_name="Queue",
        index_in_buffer=0,
    )
    player = SimpleNamespace(player_id="player-1", name="Player")
    mass = MagicMock()
    mass.player_queues.get.return_value = queue
    mass.player_queues.load_next_queue_item = AsyncMock(return_value=next_item)
    mass.player_queues.index_by_id.return_value = 1
    audio = StreamsAudio(cast("Any", mass))
    audio.setup()
    audio.select_pcm_format = AsyncMock(return_value=pcm_format)  # type: ignore[method-assign]
    audio.crossfade_allowed = MagicMock(return_value=True)  # type: ignore[method-assign]
    smart_fade = SimpleNamespace(
        timing_info=SimpleNamespace(
            pre_crossfade_duration=3,
            crossfade_duration=5,
            fadein_trimmed_duration=0,
        )
    )
    monkeypatch.setattr(
        audio.smart_fades_mixer,
        "build",
        AsyncMock(return_value=smart_fade),
    )

    async def _mix(
        _smart_fade: object,
        *,
        fade_in_part: AsyncGenerator[bytes],
        **_kwargs: object,
    ) -> AsyncGenerator[bytes]:
        async for chunk in fade_in_part:
            yield chunk

    monkeypatch.setattr(audio.smart_fades_mixer, "mix", _mix)
    requested_beyond_resident = False
    seek_positions: list[float | None] = []

    async def _item_stream(
        queue_item: object,
        *_args: object,
        **kwargs: object,
    ) -> AsyncGenerator[bytes]:
        nonlocal requested_beyond_resident
        if queue_item is current_item:
            yield bytes(pcm_format.pcm_sample_size * 8)
            yield bytes(pcm_format.pcm_sample_size * 8)
            return
        seek_positions.append(cast("float | None", kwargs.get("seek_position")))
        yield bytes(pcm_format.pcm_sample_size * MIN_CROSSFADE_FALLBACK_DURATION)
        requested_beyond_resident = True
        pytest.fail("Crossfade requested audio beyond the resident buffer")

    monkeypatch.setattr(audio, "get_queue_item_stream", _item_stream)
    stream = audio.get_queue_item_stream_with_smartfade(
        cast("Any", player),
        cast("Any", current_item),
        pcm_format,
        crossfade_mode=CrossfadeMode.STANDARD_CROSSFADE,
        standard_crossfade_duration=8,
    )

    _ = [chunk async for chunk in stream]

    assert not requested_beyond_resident
    crossfade_data = audio._crossfade_data["queue-1"]
    assert crossfade_data.fade_in_media_duration == resident_media_duration
    assert crossfade_data.elapsed_time_offset == 5 * playback_speed

    next_stream = audio.get_queue_item_stream_with_smartfade(
        cast("Any", player),
        cast("Any", next_item),
        pcm_format,
        crossfade_mode=CrossfadeMode.STANDARD_CROSSFADE,
        standard_crossfade_duration=8,
    )
    await anext(next_stream)
    await next_stream.aclose()

    assert seek_positions == [None, resident_media_duration]
    assert next_details.seek_position == 5 * playback_speed
