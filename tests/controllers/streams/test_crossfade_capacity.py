"""Tests for crossfade degradation when incoming source capacity is unavailable."""

from __future__ import annotations

import struct
from collections.abc import AsyncGenerator
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import ContentType, CrossfadeMode, MediaType, StreamType
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.controllers.streams.audio import (
    MIN_CROSSFADE_DURATION,
    StreamsAudio,
)
from music_assistant.controllers.streams.audio_buffer import AudioBuffer
from music_assistant.controllers.streams.smart_fades.helpers import SMART_CROSSFADE_DURATION


def _audio(pcm_format: AudioFormat, seconds: float) -> bytes:
    """
    Return PCM that reads as audio rather than as an item's trailing silence.

    The holdback measures the silent run a buffer ends with, so a fixture filled
    with zeroes would stand in for a track that has already finished.
    """
    frame = struct.pack("<2h", 9000, -9000)
    size = int(pcm_format.pcm_sample_size * seconds)
    return (frame * (size // len(frame) + 1))[:size]


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


def _buffer(duration_available: float, ready: bool, eof: bool = False) -> AudioBuffer:
    """Build a valid buffer with the requested resident duration."""
    audio_buffer = MagicMock(spec=AudioBuffer)
    audio_buffer.has_error = False
    audio_buffer.is_valid.return_value = True
    audio_buffer.duration_available = duration_available
    audio_buffer.eof = eof
    audio_buffer.ready = MagicMock()
    audio_buffer.ready.is_set.return_value = ready
    return audio_buffer


def _delivered_buffer() -> SimpleNamespace:
    """Build the outgoing track's buffer, with its source done delivering."""
    return SimpleNamespace(eof=True, cancelled=False, has_error=False, max_size_seconds=300)


def test_ready_incoming_buffer_keeps_smart_crossfade() -> None:
    """A tail that carries the full smart window keeps the requested Smart Fade."""
    audio = StreamsAudio(MagicMock())

    mode, duration = audio._select_buffered_crossfade(
        _streamdetails(_buffer(SMART_CROSSFADE_DURATION, ready=True)),
        CrossfadeMode.SMART_CROSSFADE,
        standard_crossfade_duration=8,
        fade_out_seconds=SMART_CROSSFADE_DURATION,
    )

    assert mode == CrossfadeMode.SMART_CROSSFADE
    assert duration == SMART_CROSSFADE_DURATION


def test_a_partly_resident_incoming_buffer_keeps_the_full_window() -> None:
    """The incoming side streams in while the blend plays, so residency does not cap it."""
    audio = StreamsAudio(MagicMock())

    mode, duration = audio._select_buffered_crossfade(
        _streamdetails(_buffer(2, ready=True)),
        CrossfadeMode.SMART_CROSSFADE,
        standard_crossfade_duration=8,
        fade_out_seconds=SMART_CROSSFADE_DURATION,
    )

    assert mode == CrossfadeMode.SMART_CROSSFADE
    assert duration == SMART_CROSSFADE_DURATION


@pytest.mark.parametrize(
    "audio_buffer",
    [
        None,
        _buffer(30, ready=False),
    ],
)
def test_unprepared_incoming_buffer_disables_crossfade(
    audio_buffer: AudioBuffer | None,
) -> None:
    """An incoming source that is not delivering yet falls back to playback without a fade."""
    audio = StreamsAudio(MagicMock())

    mode, duration = audio._select_buffered_crossfade(
        _streamdetails(audio_buffer),
        CrossfadeMode.SMART_CROSSFADE,
        standard_crossfade_duration=8,
        fade_out_seconds=SMART_CROSSFADE_DURATION,
    )

    assert mode == CrossfadeMode.DISABLED
    assert duration == 0


def test_a_tail_below_the_minimum_disables_the_crossfade() -> None:
    """Too short a held tail is played out instead of blended."""
    audio = StreamsAudio(MagicMock())

    mode, duration = audio._select_buffered_crossfade(
        _streamdetails(_buffer(30, ready=True)),
        CrossfadeMode.SMART_CROSSFADE,
        standard_crossfade_duration=8,
        fade_out_seconds=MIN_CROSSFADE_DURATION - 0.5,
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
        buffer=_delivered_buffer(),
        is_realtime=False,
    )
    next_details = SimpleNamespace(
        audio_format=pcm_format,
        buffer=None,
        duration=16,
        seek_position=0,
        uri="test://next",
        is_realtime=False,
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
        yield _audio(pcm_format, 8)
        yield _audio(pcm_format, 8)

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


@pytest.mark.parametrize("playback_speed", [0.5, 2.0])
async def test_crossfade_reads_its_window_past_the_resident_buffer(
    monkeypatch: pytest.MonkeyPatch,
    playback_speed: float,
) -> None:
    """The blend consumes its whole window as it arrives, and hands on the media time used."""
    pcm_format = AudioFormat(
        content_type=ContentType.PCM_S16LE,
        sample_rate=8000,
        bit_depth=16,
        channels=2,
    )
    resident_media_duration = 2.0
    current_details = SimpleNamespace(
        duration=16,
        seek_position=0,
        seconds_streamed=0,
        uri="test://current",
        buffer=_delivered_buffer(),
        is_realtime=False,
    )
    next_details = SimpleNamespace(
        audio_format=pcm_format,
        buffer=_buffer(resident_media_duration, ready=True),
        duration=16,
        seek_position=0,
        uri="test://next",
        volume_normalization_mode=None,
        is_realtime=False,
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
    crossfade_duration = 8
    smart_fade = SimpleNamespace(
        timing_info=SimpleNamespace(
            pre_crossfade_duration=0,
            post_crossfade_duration=0,
            crossfade_duration=crossfade_duration,
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
    incoming_seconds_read = 0

    async def _item_stream(
        queue_item: object,
        *_args: object,
        **_kwargs: object,
    ) -> AsyncGenerator[bytes]:
        nonlocal incoming_seconds_read
        if queue_item is current_item:
            yield _audio(pcm_format, 8)
            yield _audio(pcm_format, 8)
            return
        # the incoming source keeps delivering beyond what was resident at the boundary
        for _ in range(20):
            incoming_seconds_read += 1
            yield _audio(pcm_format, 1)

    monkeypatch.setattr(audio, "get_queue_item_stream", _item_stream)
    stream = audio.get_queue_item_stream_with_smartfade(
        cast("Any", player),
        cast("Any", current_item),
        pcm_format,
        crossfade_mode=CrossfadeMode.STANDARD_CROSSFADE,
        standard_crossfade_duration=crossfade_duration,
    )

    _ = [chunk async for chunk in stream]

    assert incoming_seconds_read > resident_media_duration
    crossfade_data = audio._crossfade_data["queue-1"]
    assert crossfade_data.queue_item_id == "next"
    # the window is stream time, so fast playback reaches the incoming track's
    # half-duration cap sooner: at 2x an 8s overlap would eat this whole track
    expected_window = min(crossfade_duration, next_details.duration / playback_speed / 2)
    # the next track resumes at the media time the blend already played
    assert crossfade_data.fade_in_media_duration == pytest.approx(expected_window * playback_speed)
    assert crossfade_data.fade_in_media_duration <= next_details.duration / 2
