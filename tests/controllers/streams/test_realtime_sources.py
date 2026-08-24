"""Tests for the is_realtime gate across the buffer, holdback, and stream paths."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import (
    ContentType,
    CrossfadeMode,
    MediaType,
    PlayerFeature,
    StreamType,
    VolumeNormalizationMode,
)
from music_assistant_models.errors import QueueEmpty
from music_assistant_models.media_items import (
    AudioFormat,
    AudioSource,
    ProviderMapping,
    Radio,
    Track,
)
from music_assistant_models.queue_item import QueueItem
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.controllers.streams.audio import StreamsAudio, _RealtimeTailHold
from music_assistant.controllers.streams.audio_buffer import AudioBuffer
from music_assistant.controllers.streams.constants import BufferSize
from music_assistant.controllers.streams.controller import StreamsController
from music_assistant.controllers.streams.smart_fades.fades import StandardCrossFade
from music_assistant.controllers.streams.smart_fades.helpers import SMART_CROSSFADE_DURATION
from music_assistant.models.music_provider import MusicProvider

# Standard test PCM format: 44100Hz, 16-bit, stereo
TEST_PCM_FORMAT = AudioFormat(
    content_type=ContentType.PCM_S16LE,
    sample_rate=44100,
    bit_depth=16,
    channels=2,
)

# One second of silence in the test format
ONE_SECOND_CHUNK = b"\x00" * TEST_PCM_FORMAT.pcm_sample_size


def _make_stream_details(
    media_type: MediaType,
    *,
    is_realtime: bool = False,
    volume_normalization_mode: VolumeNormalizationMode | None = None,
    queue_id: str | None = None,
) -> StreamDetails:
    """Build minimal stream details for AudioBuffer.get_buffer tests."""
    return StreamDetails(
        provider="builtin",
        item_id="item-1",
        audio_format=TEST_PCM_FORMAT,
        media_type=media_type,
        stream_type=StreamType.HTTP,
        path="http://example.com/audio.mp3",
        duration=180,
        can_seek=True,
        allow_seek=True,
        queue_id=queue_id,
        is_realtime=is_realtime,
        volume_normalization_mode=volume_normalization_mode,
    )


async def _make_source(num_chunks: int) -> AsyncGenerator[bytes]:
    """Create an async generator that yields one-second PCM chunks."""
    for _ in range(num_chunks):
        yield ONE_SECOND_CHUNK


def _make_mass_for_get_buffer(
    *, queue: Any | None = None
) -> tuple[MagicMock, list[asyncio.Task[None]], list[float | None]]:
    """Build a minimal mass stub for AudioBuffer.get_buffer tests."""
    received_seek_positions: list[float | None] = []

    def _get_media_stream(*_args: Any, **kwargs: Any) -> AsyncGenerator[bytes]:
        received_seek_positions.append(kwargs.get("seek_position"))
        return _make_source(1)

    mass = MagicMock()
    mass.config.get_raw_core_config_value.return_value = BufferSize.BALANCED.value
    mass.player_queues.get.return_value = queue
    mass.streams = SimpleNamespace(
        audio_analysis=SimpleNamespace(start_analysis=AsyncMock(return_value=None)),
        audio=SimpleNamespace(get_media_stream=_get_media_stream),
    )
    scheduled_tasks: list[asyncio.Task[None]] = []

    def _create_task(coro: Any) -> asyncio.Task[None]:
        task: asyncio.Task[None] = asyncio.ensure_future(coro)
        scheduled_tasks.append(task)
        return task

    mass.create_task.side_effect = _create_task
    return mass, scheduled_tasks, received_seek_positions


def _streamdetails_for_crossfade(
    audio_buffer: AudioBuffer | None, *, is_realtime: bool = False
) -> StreamDetails:
    """Build incoming track details with an optional prepared buffer."""
    streamdetails = StreamDetails(
        provider="test--1",
        item_id="track-1",
        audio_format=AudioFormat(content_type=ContentType.FLAC),
        media_type=MediaType.TRACK,
        stream_type=StreamType.HTTP,
        path="http://test.invalid/track.flac",
        duration=180,
        is_realtime=is_realtime,
    )
    streamdetails.buffer = audio_buffer
    return streamdetails


async def _empty_mix(*_args: object, **_kwargs: object) -> AsyncGenerator[bytes]:
    """Stand in for the mixer, producing no audio."""
    no_audio: tuple[bytes, ...] = ()
    for chunk in no_audio:
        yield chunk


def _buffer(duration_available: float, ready: bool) -> AudioBuffer:
    """Build a valid buffer with the requested resident duration."""
    audio_buffer = MagicMock(spec=AudioBuffer)
    audio_buffer.has_error = False
    audio_buffer.is_valid.return_value = True
    audio_buffer.duration_available = duration_available
    audio_buffer.ready = MagicMock()
    audio_buffer.ready.is_set.return_value = ready
    return audio_buffer


def _stream_details_provider(streamdetails: StreamDetails) -> StreamsAudio:
    """Build a StreamsAudio whose single provider hands back the given streamdetails."""
    provider = MagicMock()
    provider.instance_id = "test--1"
    provider.domain = "test"
    provider.available = True
    provider.is_streaming_provider = True
    provider.get_stream_details = AsyncMock(return_value=streamdetails)
    mass = MagicMock()
    mass.get_provider.side_effect = lambda instance, **_kwargs: (
        provider if instance == "test--1" else None
    )
    mass.providers = []
    mass.player_queues.queue_data_or_none.return_value = None
    mass.streams.get_config_value.return_value = -17
    return StreamsAudio(mass)


def _queue_item_with_mapping(media_item_cls: type) -> QueueItem:
    """Build a queue item whose media item carries one matching provider mapping."""
    mapping = ProviderMapping(item_id="item-1", provider_domain="test", provider_instance="test--1")
    media_item = media_item_cls(
        item_id="item-1", provider="test--1", name="Item", provider_mappings={mapping}
    )
    return QueueItem(
        queue_id="q1", queue_item_id="qi1", name="Item", duration=None, media_item=media_item
    )


# -- AudioBuffer.get_buffer: ready threshold ladder --


@pytest.mark.parametrize(
    (
        "is_realtime",
        "crossfade_enabled",
        "normalization_mode",
        "media_type",
        "expected_threshold",
    ),
    [
        pytest.param(True, False, None, MediaType.RADIO, 1, id="realtime_base"),
        pytest.param(True, False, None, MediaType.AUDIO_SOURCE, 1, id="realtime_audio_source"),
        # the queue's crossfade setting buys nothing for a realtime source: MA's own
        # crossfade is force-disabled for one, so a second of audio here would only
        # be a second of extra startup delay
        pytest.param(True, True, None, MediaType.TRACK, 1, id="realtime_crossfade"),
        pytest.param(
            True,
            False,
            VolumeNormalizationMode.DYNAMIC,
            MediaType.TRACK,
            2,
            id="realtime_dynamic_normalization",
        ),
        pytest.param(False, True, None, MediaType.TRACK, 8, id="non_realtime_crossfade"),
        pytest.param(
            False,
            False,
            VolumeNormalizationMode.DYNAMIC,
            MediaType.RADIO,
            3,
            id="non_realtime_dynamic_radio",
        ),
        pytest.param(
            False,
            False,
            VolumeNormalizationMode.DYNAMIC,
            MediaType.TRACK,
            5,
            id="non_realtime_dynamic_track",
        ),
        pytest.param(False, False, None, MediaType.TRACK, 2, id="non_realtime_default"),
    ],
)
async def test_ready_threshold_ladder(
    is_realtime: bool,
    crossfade_enabled: bool,
    normalization_mode: VolumeNormalizationMode | None,
    media_type: MediaType,
    expected_threshold: int,
) -> None:
    """The buffered-ready threshold follows the realtime ladder, leaving the old one intact."""
    # a realtime source is only ever raised above the floor by dynamic normalization,
    # which genuinely needs its lookahead
    queue = SimpleNamespace(crossfade_enabled=crossfade_enabled)
    mass, scheduled_tasks, _seek_positions = _make_mass_for_get_buffer(queue=queue)
    streamdetails = _make_stream_details(
        media_type,
        is_realtime=is_realtime,
        volume_normalization_mode=normalization_mode,
        queue_id="queue-1",
    )

    buffer = await AudioBuffer.get_buffer(mass, streamdetails, reason="test")

    assert buffer._ready_threshold == expected_threshold
    await asyncio.gather(*scheduled_tasks)
    await buffer.clear()


# -- AudioBuffer.get_buffer: seek handling --


@pytest.mark.parametrize(
    ("is_realtime", "seek_seconds", "expected_source_seek"),
    [
        pytest.param(True, 30, 30, id="realtime_short_seek_reaches_source"),
        pytest.param(False, 30, 0, id="non_realtime_short_seek_buffers_from_start"),
        pytest.param(False, 90, 90, id="non_realtime_long_seek_reaches_source"),
    ],
)
async def test_get_buffer_seek_position_reaches_the_source(
    is_realtime: bool, seek_seconds: int, expected_source_seek: int
) -> None:
    """A realtime source always seeks at the source; a non-realtime one only for a large seek."""
    mass, scheduled_tasks, received_seek_positions = _make_mass_for_get_buffer()
    streamdetails = _make_stream_details(MediaType.TRACK, is_realtime=is_realtime)

    buffer = await AudioBuffer.get_buffer(
        mass, streamdetails, seek_position_ms=seek_seconds * 1000, reason="test"
    )

    assert received_seek_positions == [expected_source_seek]
    assert buffer._discarded_chunks == expected_source_seek
    await asyncio.gather(*scheduled_tasks)
    await buffer.clear()


# -- AudioBuffer.eof --


async def test_eof_reflects_producer_completion() -> None:
    """The eof flag turns True only once the producer has delivered everything."""
    buf = AudioBuffer(TEST_PCM_FORMAT)
    assert not buf.eof
    await buf._put(ONE_SECOND_CHUNK)
    assert not buf.eof
    await buf._set_eof()
    assert buf.eof


# -- StreamsAudio._crossfade_holdback_allowed --


def test_holdback_rejected_when_crossfade_buffer_size_is_zero() -> None:
    """A zero-size crossfade buffer has nothing to hold back."""
    audio = StreamsAudio(MagicMock())
    streamdetails = SimpleNamespace(
        is_realtime=False, buffer=SimpleNamespace(eof=True, has_error=False, max_size_seconds=300)
    )

    assert audio._crossfade_holdback_allowed(cast("Any", streamdetails), 0) is False


def test_holdback_never_arms_a_fixed_window_for_a_realtime_source() -> None:
    """A realtime source's holdback is surplus-grown (_RealtimeTailHold), never fixed."""
    audio = StreamsAudio(MagicMock())
    for eof in (False, True):
        streamdetails = SimpleNamespace(
            is_realtime=True,
            buffer=SimpleNamespace(eof=eof, has_error=False, max_size_seconds=300),
        )
        assert audio._crossfade_holdback_allowed(cast("Any", streamdetails), 10) is False


async def test_realtime_tail_hold_grows_with_the_banked_surplus() -> None:
    """The holdback window covers exactly what the source delivered beyond the clock."""
    pcm_format = TEST_PCM_FORMAT
    frame_size = (pcm_format.bit_depth // 8) * pcm_format.channels
    audio_buffer = SimpleNamespace(eof=False, duration_available=2.0)
    hold = _RealtimeTailHold(pcm_format, cast("Any", audio_buffer))

    # nothing arrived yet: nothing may be held
    assert hold.hold_target(8 * pcm_format.pcm_sample_size, frame_size) == 0

    # 22s of content arrived; pretend ~4s of wall time passed since the first byte
    hold.note_bytes(22 * pcm_format.pcm_sample_size)
    hold._started = asyncio.get_event_loop().time() - 4.0
    target = hold.hold_target(8 * pcm_format.pcm_sample_size, frame_size)
    # surplus = 22 (content) + 2 (resident) - 4 (elapsed) = 20s; half of it may be
    # held (the rest keeps growing the player's lead) => capped at the window
    assert target == 8 * pcm_format.pcm_sample_size
    # a larger window is bounded by half the surplus, frame-aligned
    larger = hold.hold_target(45 * pcm_format.pcm_sample_size, frame_size)
    assert larger % frame_size == 0
    assert int(9.5 * pcm_format.pcm_sample_size) < larger <= 10 * pcm_format.pcm_sample_size

    # once the source is done, the rest is resident: full window regardless
    audio_buffer.eof = True
    assert (
        hold.hold_target(45 * pcm_format.pcm_sample_size, frame_size)
        == 45 * pcm_format.pcm_sample_size
    )


def test_holdback_rejected_without_a_buffer() -> None:
    """A source with no buffer yet has nothing to hold back."""
    audio = StreamsAudio(MagicMock())
    streamdetails = SimpleNamespace(is_realtime=False, buffer=None)

    assert audio._crossfade_holdback_allowed(cast("Any", streamdetails), 10) is False


def test_holdback_rejected_before_source_reaches_eof() -> None:
    """A still-filling buffer keeps limiting playback, so its tail is not held back yet."""
    audio = StreamsAudio(MagicMock())
    streamdetails = SimpleNamespace(
        is_realtime=False, buffer=SimpleNamespace(eof=False, has_error=False, max_size_seconds=300)
    )

    assert audio._crossfade_holdback_allowed(cast("Any", streamdetails), 10) is False


def test_holdback_allowed_once_source_reaches_eof() -> None:
    """A fully delivered, non-realtime source may hold back its tail for a crossfade."""
    audio = StreamsAudio(MagicMock())
    streamdetails = SimpleNamespace(
        is_realtime=False, buffer=SimpleNamespace(eof=True, has_error=False, max_size_seconds=300)
    )

    assert audio._crossfade_holdback_allowed(cast("Any", streamdetails), 10) is True


def test_holdback_rejected_for_a_failed_source() -> None:
    """A source that failed is skipped without a fade, so its tail is played out instead."""
    audio = StreamsAudio(MagicMock())
    streamdetails = SimpleNamespace(
        is_realtime=False,
        buffer=SimpleNamespace(eof=True, has_error=True, max_size_seconds=300),
    )

    assert audio._crossfade_holdback_allowed(cast("Any", streamdetails), 10) is False


def test_holdback_allowed_when_the_buffer_can_never_hold_the_tail() -> None:
    """A buffer smaller than the tail collects it while the source runs, or loses the fade."""
    audio = StreamsAudio(MagicMock())
    streamdetails = SimpleNamespace(
        is_realtime=False, buffer=SimpleNamespace(eof=False, has_error=False, max_size_seconds=15)
    )

    assert audio._crossfade_holdback_allowed(cast("Any", streamdetails), 45) is True
    assert audio._crossfade_holdback_allowed(cast("Any", streamdetails), 10) is False


def test_holdback_capacity_accounts_for_playback_speed() -> None:
    """Buffer capacity is source time, so faster playback leaves fewer seconds to fade with."""
    audio = StreamsAudio(MagicMock())
    streamdetails = SimpleNamespace(
        is_realtime=False, buffer=SimpleNamespace(eof=False, has_error=False, max_size_seconds=60)
    )

    assert audio._crossfade_holdback_allowed(cast("Any", streamdetails), 45) is False
    assert audio._crossfade_holdback_allowed(cast("Any", streamdetails), 45, 2.0) is True


# -- StreamsAudio._select_buffered_crossfade --


def test_realtime_incoming_source_climbs_the_fade_ladder_by_residency() -> None:
    """What is resident picks the rung: smart with the window in hand, standard without."""
    audio = StreamsAudio(MagicMock())

    # barely delivering: the streaming standard mix covers the overlap as it arrives
    mode, duration = audio._select_buffered_crossfade(
        _streamdetails_for_crossfade(_buffer(2, ready=True), is_realtime=True),
        CrossfadeMode.SMART_CROSSFADE,
        standard_crossfade_duration=8,
    )
    assert (mode, duration) == (CrossfadeMode.STANDARD_CROSSFADE, 8)

    # with a held tail that can carry the window, the smart fade applies and is
    # sized by that tail: the incoming side streams in while the blend plays
    mode, duration = audio._select_buffered_crossfade(
        _streamdetails_for_crossfade(_buffer(2, ready=True), is_realtime=True),
        CrossfadeMode.SMART_CROSSFADE,
        standard_crossfade_duration=8,
        fade_out_seconds=20,
    )
    assert (mode, duration) == (CrossfadeMode.SMART_CROSSFADE, 20)

    # ... but not when the outgoing tail cannot carry its half of the window
    mode, duration = audio._select_buffered_crossfade(
        _streamdetails_for_crossfade(_buffer(15, ready=True), is_realtime=True),
        CrossfadeMode.SMART_CROSSFADE,
        standard_crossfade_duration=8,
        fade_out_seconds=6,
    )
    assert (mode, duration) == (CrossfadeMode.STANDARD_CROSSFADE, 8)


def test_realtime_incoming_source_not_yet_delivering_skips_the_fade() -> None:
    """A realtime source whose buffer is not ready yet means the boundary plays clean."""
    audio = StreamsAudio(MagicMock())

    mode, duration = audio._select_buffered_crossfade(
        _streamdetails_for_crossfade(_buffer(0, ready=False), is_realtime=True),
        CrossfadeMode.STANDARD_CROSSFADE,
        standard_crossfade_duration=8,
    )

    assert mode == CrossfadeMode.DISABLED
    assert duration == 0


# -- Path level: get_queue_item_stream_with_smartfade --


async def test_smartfade_realtime_current_item_fades_once_its_source_is_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A realtime item whose source finished delivering holds its tail and fades."""
    pcm_format = AudioFormat(
        content_type=ContentType.PCM_S16LE,
        sample_rate=8000,
        bit_depth=16,
        channels=2,
    )
    # the source is done delivering, which is what arms the realtime holdback
    current_details = SimpleNamespace(
        duration=16,
        seek_position=0,
        seconds_streamed=0,
        uri="test://current",
        buffer=SimpleNamespace(eof=True, cancelled=False, has_error=False, max_size_seconds=300),
        is_realtime=True,
    )
    next_details = SimpleNamespace(
        audio_format=pcm_format,
        buffer=_buffer(SMART_CROSSFADE_DURATION, ready=True),
        duration=16,
        seek_position=0,
        uri="test://next",
        is_realtime=False,
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
    build = AsyncMock(
        return_value=SimpleNamespace(
            timing_info=SimpleNamespace(
                fadein_trimmed_duration=0.0,
                crossfade_duration=8.0,
                pre_crossfade_duration=0.0,
            )
        )
    )
    monkeypatch.setattr(audio.smart_fades_mixer, "build", build)

    async def _concat_mix(
        _smart_fade: object,
        *,
        fade_in_part: AsyncGenerator[bytes],
        fade_out_part: bytes,
        **_kwargs: object,
    ) -> AsyncGenerator[bytes]:
        yield fade_out_part
        async for fade_in_chunk in fade_in_part:
            yield fade_in_chunk

    monkeypatch.setattr(audio.smart_fades_mixer, "mix", _concat_mix)

    async def _item_stream(
        _queue_item: object,
        *_args: object,
        **_kwargs: object,
    ) -> AsyncGenerator[bytes]:
        yield bytes(pcm_format.pcm_sample_size * 8)
        yield bytes(pcm_format.pcm_sample_size * 8)

    monkeypatch.setattr(audio, "get_queue_item_stream", _item_stream)
    stream = audio.get_queue_item_stream_with_smartfade(
        cast("Any", player),
        cast("Any", current_item),
        pcm_format,
        crossfade_mode=CrossfadeMode.STANDARD_CROSSFADE,
        standard_crossfade_duration=8,
    )

    output = b"".join([chunk async for chunk in stream])

    # 8s warmup + 8s of mix output (pre+overlap); the incoming share of the mix
    # is buffered as crossfade data for the next item's own stream
    assert len(output) == pcm_format.pcm_sample_size * 16
    build.assert_awaited_once()
    crossfade_data = audio._crossfade_data.get("queue-1")
    assert crossfade_data is not None
    assert crossfade_data.queue_item_id == "next"


async def test_smartfade_still_filling_buffer_yields_all_audio_without_crossfade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-realtime source that has not yet reached EOF is also never held back for a fade."""
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
        buffer=SimpleNamespace(eof=False, cancelled=False, has_error=False, max_size_seconds=300),
        is_realtime=False,
    )
    next_details = SimpleNamespace(
        audio_format=pcm_format,
        buffer=_buffer(SMART_CROSSFADE_DURATION, ready=True),
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
    build = AsyncMock()
    monkeypatch.setattr(audio.smart_fades_mixer, "build", build)

    async def _current_stream(
        queue_item: object,
        *_args: object,
        **_kwargs: object,
    ) -> AsyncGenerator[bytes]:
        if queue_item is not current_item:
            pytest.fail("The incoming source was opened while the current buffer was still open")
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
    build.assert_not_awaited()
    assert "queue-1" not in audio._crossfade_data


# -- Path level: get_queue_flow_stream --


async def test_flow_realtime_item_yields_all_audio_as_plain_concatenation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A realtime item's flow audio is passed straight through and simply concatenated."""
    pcm_format = AudioFormat(
        content_type=ContentType.PCM_S16LE,
        sample_rate=8000,
        bit_depth=16,
        channels=2,
    )
    # the source is done delivering, so only the realtime flag can deny the holdback
    realtime_details = SimpleNamespace(
        audio_format=pcm_format,
        buffer=SimpleNamespace(eof=True, cancelled=False, has_error=False, max_size_seconds=300),
        fade_in=False,
        stream_error=False,
        uri="test://realtime",
        seek_position=0,
        seconds_streamed=0,
        duration=20,
        is_realtime=True,
    )
    realtime_item = SimpleNamespace(
        queue_id="queue-1",
        queue_item_id="item-1",
        name="Realtime",
        media_type=MediaType.TRACK,
        media_item=None,
        streamdetails=realtime_details,
        duration=20,
        extra_attributes={},
    )
    next_details = SimpleNamespace(
        audio_format=pcm_format,
        buffer=None,
        fade_in=False,
        stream_error=False,
        uri="test://next",
        seek_position=0,
        seconds_streamed=0,
        duration=20,
        is_realtime=False,
    )
    next_item = SimpleNamespace(
        queue_id="queue-1",
        queue_item_id="item-2",
        name="Next",
        media_type=MediaType.TRACK,
        media_item=None,
        streamdetails=next_details,
        duration=20,
        extra_attributes={},
    )
    queue = SimpleNamespace(
        queue_id="queue-1",
        display_name="Queue",
        flow_mode=False,
        overlay_enabled=False,
        overlay_source=None,
    )
    queue_data = SimpleNamespace(session_id="session-1", flow_mode_stream_log=[])
    mass = MagicMock()
    mass.player_queues.queue_data.return_value = queue_data
    mass.player_queues.load_next_queue_item = AsyncMock(side_effect=[next_item, QueueEmpty])
    mass.player_queues.get.return_value = queue
    mass.streams.get_crossfade_mode.return_value = CrossfadeMode.STANDARD_CROSSFADE
    mass.config.get_raw_core_config_value.return_value = 8
    mass.streams.audio_processing.update_item_context = MagicMock()
    mass.player_queues.queue_buffer_completed = MagicMock()
    player = MagicMock()
    player.config.get_value.return_value = "fixed_48000"
    player.get_supported_sample_rates.return_value = []
    mass.players.get_player.return_value = player
    audio = StreamsAudio(cast("Any", mass))
    audio.setup()
    build = AsyncMock()
    monkeypatch.setattr(audio.smart_fades_mixer, "build", build)

    realtime_chunks = [
        bytes(pcm_format.pcm_sample_size * 8),
        bytes(pcm_format.pcm_sample_size * 8),
    ]
    next_chunks = [bytes(pcm_format.pcm_sample_size * 2)]

    async def _item_stream(
        queue_item: SimpleNamespace, *_args: object, **_kwargs: object
    ) -> AsyncGenerator[bytes]:
        chunks = realtime_chunks if queue_item is realtime_item else next_chunks
        for chunk in chunks:
            yield chunk

    monkeypatch.setattr(audio, "get_queue_item_stream", _item_stream)
    select_crossfade = MagicMock(wraps=audio._select_buffered_crossfade)
    monkeypatch.setattr(audio, "_select_buffered_crossfade", select_crossfade)
    stream = audio.get_queue_flow_stream(
        cast("Any", queue), cast("Any", realtime_item), pcm_format, session_id="session-1"
    )

    output = b"".join([chunk async for chunk in stream])

    assert output == b"".join(realtime_chunks) + b"".join(next_chunks)
    build.assert_not_awaited()
    # no tail was held back, so the next item is never asked to fade into anything
    select_crossfade.assert_not_called()
    mass.player_queues.queue_buffer_completed.assert_called_once()


async def test_smartfade_unaligned_chunks_still_crossfade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A source whose chunks are not whole seconds still collects a complete fade tail."""
    pcm_format = AudioFormat(
        content_type=ContentType.PCM_S16LE,
        sample_rate=8000,
        bit_depth=16,
        channels=2,
    )
    current_details = SimpleNamespace(
        duration=30,
        seek_position=0,
        seconds_streamed=0,
        uri="test://current",
        buffer=SimpleNamespace(eof=True, cancelled=False, has_error=False, max_size_seconds=300),
        is_realtime=False,
    )
    next_details = SimpleNamespace(
        audio_format=pcm_format,
        buffer=_buffer(SMART_CROSSFADE_DURATION, ready=True),
        duration=30,
        seek_position=0,
        uri="test://next",
        is_realtime=False,
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
        extra_attributes={},
        available=True,
    )
    queue = SimpleNamespace(queue_id="queue-1", display_name="Queue", index_in_buffer=0)
    player = SimpleNamespace(player_id="player-1", name="Player")
    mass = MagicMock()
    mass.player_queues.get.return_value = queue
    mass.player_queues.load_next_queue_item = AsyncMock(return_value=next_item)
    mass.player_queues.index_by_id.return_value = 1
    audio = StreamsAudio(cast("Any", mass))
    audio.setup()
    audio.select_pcm_format = AsyncMock(return_value=pcm_format)  # type: ignore[method-assign]
    audio.crossfade_allowed = MagicMock(return_value=True)  # type: ignore[method-assign]
    build = AsyncMock(
        return_value=SimpleNamespace(
            timing_info=SimpleNamespace(
                pre_crossfade_duration=2,
                crossfade_duration=6,
                fadein_trimmed_duration=0,
            )
        )
    )
    monkeypatch.setattr(audio.smart_fades_mixer, "build", build)
    monkeypatch.setattr(audio.smart_fades_mixer, "mix", _empty_mix)

    async def _current_stream(
        queue_item: object, *_args: object, **_kwargs: object
    ) -> AsyncGenerator[bytes]:
        if queue_item is not current_item:
            return
        # a whole second, then chunks that never line up with a second boundary
        yield bytes(pcm_format.pcm_sample_size * 8)
        for _ in range(30):
            yield bytes(pcm_format.pcm_sample_size // 3)

    monkeypatch.setattr(audio, "get_queue_item_stream", _current_stream)
    stream = audio.get_queue_item_stream_with_smartfade(
        cast("Any", player),
        cast("Any", current_item),
        pcm_format,
        crossfade_mode=CrossfadeMode.STANDARD_CROSSFADE,
        standard_crossfade_duration=8,
    )

    async for _chunk in stream:
        pass

    build.assert_awaited_once()


async def test_smartfade_short_remainder_still_crossfades(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Less audio left than the configured overlap still fades with what is there."""
    pcm_format = AudioFormat(
        content_type=ContentType.PCM_S16LE,
        sample_rate=8000,
        bit_depth=16,
        channels=2,
    )
    current_details = SimpleNamespace(
        duration=180,
        seek_position=146,
        seconds_streamed=0,
        uri="test://current",
        buffer=SimpleNamespace(eof=True, cancelled=False, has_error=False, max_size_seconds=300),
        is_realtime=False,
    )
    next_details = SimpleNamespace(
        audio_format=pcm_format,
        buffer=_buffer(SMART_CROSSFADE_DURATION, ready=True),
        duration=180,
        seek_position=0,
        uri="test://next",
        is_realtime=False,
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
        extra_attributes={},
        available=True,
    )
    queue = SimpleNamespace(queue_id="queue-1", display_name="Queue", index_in_buffer=0)
    player = SimpleNamespace(player_id="player-1", name="Player")
    mass = MagicMock()
    mass.player_queues.get.return_value = queue
    mass.player_queues.load_next_queue_item = AsyncMock(return_value=next_item)
    mass.player_queues.index_by_id.return_value = 1
    audio = StreamsAudio(cast("Any", mass))
    audio.setup()
    audio.select_pcm_format = AsyncMock(return_value=pcm_format)  # type: ignore[method-assign]
    audio.crossfade_allowed = MagicMock(return_value=True)  # type: ignore[method-assign]
    build = AsyncMock(
        return_value=SimpleNamespace(
            timing_info=SimpleNamespace(
                pre_crossfade_duration=2,
                crossfade_duration=6,
                fadein_trimmed_duration=0,
            )
        )
    )
    monkeypatch.setattr(audio.smart_fades_mixer, "build", build)
    monkeypatch.setattr(audio.smart_fades_mixer, "mix", _empty_mix)

    async def _current_stream(
        queue_item: object, *_args: object, **_kwargs: object
    ) -> AsyncGenerator[bytes]:
        if queue_item is not current_item:
            return
        # a seek near the end leaves 34s, less than the 45s smart overlap
        yield bytes(pcm_format.pcm_sample_size * 8)
        yield bytes(pcm_format.pcm_sample_size * 26)

    monkeypatch.setattr(audio, "get_queue_item_stream", _current_stream)
    stream = audio.get_queue_item_stream_with_smartfade(
        cast("Any", player),
        cast("Any", current_item),
        pcm_format,
        crossfade_mode=CrossfadeMode.SMART_CROSSFADE,
        standard_crossfade_duration=8,
    )

    async for _chunk in stream:
        pass

    build.assert_awaited_once()
    assert build.await_args is not None
    fade_out_seconds = len(build.await_args.kwargs["fade_out_data"]) / pcm_format.pcm_sample_size
    assert fade_out_seconds == pytest.approx(26, abs=1)


async def test_smartfade_stub_remainder_does_not_crossfade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A remainder too short to overlap with is played out instead of faded."""
    pcm_format = AudioFormat(
        content_type=ContentType.PCM_S16LE,
        sample_rate=8000,
        bit_depth=16,
        channels=2,
    )
    current_details = SimpleNamespace(
        duration=180,
        seek_position=176,
        seconds_streamed=0,
        uri="test://current",
        buffer=SimpleNamespace(eof=True, cancelled=False, has_error=False, max_size_seconds=300),
        is_realtime=False,
    )
    next_details = SimpleNamespace(
        audio_format=pcm_format,
        buffer=_buffer(SMART_CROSSFADE_DURATION, ready=True),
        duration=180,
        seek_position=0,
        uri="test://next",
        is_realtime=False,
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
        extra_attributes={},
        available=True,
    )
    queue = SimpleNamespace(queue_id="queue-1", display_name="Queue", index_in_buffer=0)
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
        queue_item: object, *_args: object, **_kwargs: object
    ) -> AsyncGenerator[bytes]:
        if queue_item is not current_item:
            return
        yield bytes(pcm_format.pcm_sample_size * 8)
        yield bytes(pcm_format.pcm_sample_size * 2)

    monkeypatch.setattr(audio, "get_queue_item_stream", _current_stream)
    stream = audio.get_queue_item_stream_with_smartfade(
        cast("Any", player),
        cast("Any", current_item),
        pcm_format,
        crossfade_mode=CrossfadeMode.SMART_CROSSFADE,
        standard_crossfade_duration=8,
    )

    output = b"".join([chunk async for chunk in stream])

    assert len(output) == pcm_format.pcm_sample_size * 10
    build.assert_not_awaited()


@pytest.mark.parametrize("source_crossfade_mode", [CrossfadeMode.DISABLED, CrossfadeMode.SOURCE])
async def test_flow_reports_the_crossfade_a_realtime_item_really_gets(
    monkeypatch: pytest.MonkeyPatch,
    source_crossfade_mode: CrossfadeMode,
) -> None:
    """
    A realtime item is credited with its source's fade, never one of ours.

    Music Assistant has no audio to spare for an overlap on either side of such an
    item, so the only fade it can report is the one the source applies itself.
    """
    pcm_format = AudioFormat(
        content_type=ContentType.PCM_S16LE,
        sample_rate=8000,
        bit_depth=16,
        channels=2,
    )
    realtime_details = SimpleNamespace(
        audio_format=pcm_format,
        buffer=SimpleNamespace(eof=True, cancelled=False, has_error=False, max_size_seconds=300),
        fade_in=False,
        stream_error=False,
        uri="test://realtime",
        seek_position=0,
        seconds_streamed=0,
        duration=20,
        is_realtime=True,
    )
    realtime_item = SimpleNamespace(
        queue_id="queue-1",
        queue_item_id="item-1",
        name="Realtime",
        media_type=MediaType.TRACK,
        media_item=None,
        streamdetails=realtime_details,
        duration=20,
        extra_attributes={},
    )
    queue = SimpleNamespace(
        queue_id="queue-1",
        display_name="Queue",
        flow_mode=False,
        overlay_enabled=False,
        overlay_source=None,
    )
    mass = MagicMock()
    mass.player_queues.queue_data.return_value = SimpleNamespace(
        session_id="session-1", flow_mode_stream_log=[]
    )
    mass.player_queues.load_next_queue_item = AsyncMock(side_effect=QueueEmpty)
    mass.player_queues.get.return_value = queue
    mass.streams.get_crossfade_mode.return_value = CrossfadeMode.SMART_CROSSFADE
    mass.streams.get_source_crossfade_mode.return_value = source_crossfade_mode
    mass.config.get_raw_core_config_value.return_value = 8
    update_item_context = MagicMock()
    mass.streams.audio_processing.update_item_context = update_item_context
    player = MagicMock()
    player.config.get_value.return_value = "fixed_48000"
    player.get_supported_sample_rates.return_value = []
    mass.players.get_player.return_value = player
    audio = StreamsAudio(cast("Any", mass))
    audio.setup()

    async def _item_stream(*_args: object, **_kwargs: object) -> AsyncGenerator[bytes]:
        yield bytes(pcm_format.pcm_sample_size * 4)

    monkeypatch.setattr(audio, "get_queue_item_stream", _item_stream)
    stream = audio.get_queue_flow_stream(
        cast("Any", queue), cast("Any", realtime_item), pcm_format, session_id="session-1"
    )

    async for _chunk in stream:
        pass

    update_item_context.assert_called()
    reported = update_item_context.call_args.kwargs["queue_processing"]
    assert reported.crossfade_mode == source_crossfade_mode


async def test_flow_standard_fade_only_holds_back_its_overlap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A standard transition waits for its overlap, not for the whole requested window."""
    pcm_format = AudioFormat(
        content_type=ContentType.PCM_S16LE,
        sample_rate=8000,
        bit_depth=16,
        channels=2,
    )
    first_details = SimpleNamespace(
        audio_format=pcm_format,
        buffer=SimpleNamespace(eof=True, cancelled=False, has_error=False, max_size_seconds=300),
        fade_in=False,
        stream_error=False,
        uri="test://first",
        seek_position=0,
        seconds_streamed=0,
        duration=300,
        is_realtime=False,
    )
    second_details = SimpleNamespace(
        audio_format=pcm_format,
        buffer=_buffer(SMART_CROSSFADE_DURATION, ready=True),
        fade_in=False,
        stream_error=False,
        uri="test://second",
        seek_position=0,
        seconds_streamed=0,
        duration=300,
        is_realtime=False,
        volume_normalization_mode=None,
    )
    first_item = SimpleNamespace(
        queue_id="queue-1",
        queue_item_id="item-1",
        name="First",
        media_type=MediaType.TRACK,
        media_item=None,
        streamdetails=first_details,
        duration=300,
        extra_attributes={},
    )
    second_item = SimpleNamespace(
        queue_id="queue-1",
        queue_item_id="item-2",
        name="Second",
        media_type=MediaType.TRACK,
        media_item=None,
        streamdetails=second_details,
        duration=300,
        extra_attributes={},
    )
    queue = SimpleNamespace(
        queue_id="queue-1",
        display_name="Queue",
        flow_mode=False,
        overlay_enabled=False,
        overlay_source=None,
    )
    mass = MagicMock()
    mass.player_queues.queue_data.return_value = SimpleNamespace(
        session_id="session-1", flow_mode_stream_log=[]
    )
    mass.player_queues.load_next_queue_item = AsyncMock(side_effect=[second_item, QueueEmpty])
    mass.player_queues.get.return_value = queue
    mass.streams.get_crossfade_mode.return_value = CrossfadeMode.SMART_CROSSFADE
    mass.config.get_raw_core_config_value.return_value = 8
    player = MagicMock()
    player.config.get_value.return_value = "fixed_48000"
    player.get_supported_sample_rates.return_value = []
    mass.players.get_player.return_value = player
    audio = StreamsAudio(cast("Any", mass))
    audio.setup()
    audio.crossfade_allowed = MagicMock(return_value=True)  # type: ignore[method-assign]
    # the incoming analysis is not ready, so the mixer degrades to a standard fade
    standard = StandardCrossFade(logger=MagicMock(), crossfade_duration=8)
    standard.build(
        pcm_format.pcm_sample_size * SMART_CROSSFADE_DURATION,
        pcm_format.pcm_sample_size * SMART_CROSSFADE_DURATION,
        pcm_format,
    )
    monkeypatch.setattr(audio.smart_fades_mixer, "build", AsyncMock(return_value=standard))
    monkeypatch.setattr(audio.smart_fades_mixer, "mix", _empty_mix)

    consumed: dict[str, int] = {"second": 0}

    async def _item_stream(
        queue_item: SimpleNamespace, *_args: object, **_kwargs: object
    ) -> AsyncGenerator[bytes]:
        if queue_item is first_item:
            for _ in range(60):
                yield bytes(pcm_format.pcm_sample_size)
            return
        for _ in range(SMART_CROSSFADE_DURATION + 20):
            consumed["second"] += 1
            yield bytes(pcm_format.pcm_sample_size)

    monkeypatch.setattr(audio, "get_queue_item_stream", _item_stream)
    stream = audio.get_queue_flow_stream(
        cast("Any", queue), cast("Any", first_item), pcm_format, session_id="session-1"
    )

    seconds_before_transition: int | None = None
    async for _chunk in stream:
        if seconds_before_transition is None and consumed["second"]:
            seconds_before_transition = consumed["second"]

    # the overlap is 8s, so the transition must not wait for the full 45s window
    assert seconds_before_transition is not None
    assert seconds_before_transition <= SMART_CROSSFADE_DURATION / 2


# -- StreamsController.serve_queue_item_stream steering --


class _PcmFormatRequested(Exception):
    """Raised to stop the handler once it has decided on crossfading."""


def _single_item_handler(*, is_realtime: bool) -> tuple[Any, MagicMock, dict[str, Any]]:
    """Return a single-item stream handler that stops once the PCM format is picked."""
    streamdetails = _make_stream_details(MediaType.TRACK, is_realtime=is_realtime)
    queue_item = SimpleNamespace(
        queue_id="queue-1",
        queue_item_id="item-1",
        name="Track",
        duration=180,
        streamdetails=streamdetails,
        media_item=None,
        media_type=MediaType.TRACK,
        extra_attributes={},
        image=None,
    )
    queue = SimpleNamespace(
        queue_id="queue-1",
        display_name="Queue",
        current_item=queue_item,
        crossfade_enabled=True,
        overlay_enabled=False,
        overlay_source=None,
    )
    mass = MagicMock()
    mass.player_queues.get.return_value = queue
    mass.player_queues.queue_data.return_value = SimpleNamespace(session_id="session-1")
    mass.player_queues.get_item.return_value = queue_item
    mass.config.get_raw_core_config_value.return_value = 8
    player = MagicMock(player_id="player-1", protocol_parent_id=None)
    player.state.supported_features = {PlayerFeature.GAPLESS_PLAYBACK}
    player.state.name = "Player"
    mass.players.get_player.return_value = player

    seen: dict[str, Any] = {}

    async def _select_pcm_format(**kwargs: Any) -> None:
        seen["crossfade_enabled"] = kwargs["crossfade_enabled"]
        raise _PcmFormatRequested

    audio = MagicMock()
    audio.select_pcm_format = _select_pcm_format
    controller = cast("Any", object.__new__(StreamsController))
    controller.mass = mass
    controller.audio = audio
    controller.logger = MagicMock()
    controller._log_request = MagicMock()
    controller.get_crossfade_mode = MagicMock(return_value=CrossfadeMode.SMART_CROSSFADE)
    request = MagicMock()
    request.method = "GET"
    request.match_info = {
        "queue_id": "queue-1",
        "player_id": "player-1",
        "session_id": "session-1",
        "queue_item_id": "item-1",
    }
    return controller, request, seen


async def test_single_item_handler_keeps_crossfade_for_a_realtime_item() -> None:
    """A realtime item whose source does not fade keeps the queue's crossfade."""
    controller, request, seen = _single_item_handler(is_realtime=True)

    with pytest.raises(_PcmFormatRequested):
        await controller.serve_queue_item_stream(request)

    assert seen["crossfade_enabled"] is True
    controller.get_crossfade_mode.assert_called_once()


async def test_single_item_handler_steps_aside_for_a_source_that_fades() -> None:
    """An item whose own source fades its boundaries is served without an MA fade."""
    controller, request, seen = _single_item_handler(is_realtime=True)
    controller.get_source_crossfade_mode = MagicMock(return_value=CrossfadeMode.SOURCE)

    with pytest.raises(_PcmFormatRequested):
        await controller.serve_queue_item_stream(request)

    assert seen["crossfade_enabled"] is False
    controller.get_crossfade_mode.assert_not_called()


async def test_single_item_handler_keeps_crossfade_for_a_buffered_item() -> None:
    """A buffered item still gets the queue's configured crossfade."""
    controller, request, seen = _single_item_handler(is_realtime=False)

    with pytest.raises(_PcmFormatRequested):
        await controller.serve_queue_item_stream(request)

    assert seen["crossfade_enabled"] is True
    controller.get_crossfade_mode.assert_called_once()


# -- StreamsController.get_source_crossfade_mode --


class _CrossfadingProvider(MusicProvider):
    """A music provider whose running source crossfades this item's boundary."""

    def delivers_crossfaded_audio(self, streamdetails: StreamDetails) -> bool | None:
        """Declare the source fade."""
        return True


class _NonCrossfadingServingProvider(MusicProvider):
    """A music provider serving this item, whose session fades neither of its boundaries."""

    def delivers_crossfaded_audio(self, streamdetails: StreamDetails) -> bool | None:
        """Deny the source fade for this item."""
        return False


class _UndecidedCrossfadingProvider(MusicProvider):
    """A music provider that can crossfade its own playback but serves nothing yet."""

    def delivers_crossfaded_audio(self, streamdetails: StreamDetails) -> bool | None:
        """Leave the answer to the queue's own setting."""
        return None


@pytest.mark.parametrize(
    ("queue_crossfade_mode", "provider", "expected"),
    [
        # with no session running the queue preference is all there is to go on
        (
            CrossfadeMode.DISABLED,
            object.__new__(_UndecidedCrossfadingProvider),
            CrossfadeMode.DISABLED,
        ),
        (
            CrossfadeMode.SMART_CROSSFADE,
            object.__new__(_UndecidedCrossfadingProvider),
            CrossfadeMode.SOURCE,
        ),
        # a running source answers for itself, whatever the setting says now
        (
            CrossfadeMode.DISABLED,
            object.__new__(_CrossfadingProvider),
            CrossfadeMode.SOURCE,
        ),
        # a provider that does not fade its own playback, and a plugin-served item
        (CrossfadeMode.SMART_CROSSFADE, object.__new__(MusicProvider), CrossfadeMode.DISABLED),
        (CrossfadeMode.SMART_CROSSFADE, None, CrossfadeMode.DISABLED),
    ],
)
def test_only_a_declared_source_fade_is_reported_as_such(
    queue_crossfade_mode: CrossfadeMode,
    provider: Any,
    expected: CrossfadeMode,
) -> None:
    """Only a provider that says it crossfades its own playback is credited with one."""
    controller = _source_crossfade_controller(provider, queue_crossfade_mode)

    assert controller.get_source_crossfade_mode(MagicMock(), _realtime_track()) == expected


@pytest.mark.parametrize(
    ("media_type", "is_realtime"),
    [
        # our own mixer owns both of these, so the source's fade is not in play
        (MediaType.TRACK, False),
        (MediaType.AUDIOBOOK, True),
    ],
)
def test_no_source_fade_for_audio_we_mix_ourselves(
    media_type: MediaType, is_realtime: bool
) -> None:
    """A source fade is only reported for audio Music Assistant does not mix."""
    controller = _source_crossfade_controller(
        object.__new__(_CrossfadingProvider), CrossfadeMode.SMART_CROSSFADE
    )
    controller.mass.player_queues.get_next_item.return_value = _follower("same-provider")
    queue_item = SimpleNamespace(
        queue_item_id="item-1",
        media_type=media_type,
        streamdetails=_make_stream_details(media_type, is_realtime=is_realtime),
    )

    assert controller.get_source_crossfade_mode(MagicMock(), queue_item) == CrossfadeMode.DISABLED


@pytest.mark.parametrize(
    ("neighbour_kind", "expected"),
    [
        # a boundary the source owns both sides of, as a provider item and as a
        # library item that carries the source in its mappings instead
        ("same-provider", CrossfadeMode.SOURCE),
        ("library-mapped", CrossfadeMode.SOURCE),
        # nothing next to it, so there is no boundary at all
        ("none", CrossfadeMode.DISABLED),
        # the source plays the current item out at any of these, so the cut is hard
        ("other-provider", CrossfadeMode.DISABLED),
        ("not-a-track", CrossfadeMode.DISABLED),
        ("unresolvable", CrossfadeMode.DISABLED),
    ],
)
@pytest.mark.parametrize("side", ["follower", "predecessor"])
def test_an_undecided_source_needs_a_boundary_it_would_own(
    neighbour_kind: str, side: str, expected: CrossfadeMode
) -> None:
    """
    A source that is not serving this queue yet is credited only where it could fade.

    It cannot answer for the item, so the boundary stands in: either side counts,
    the same way one of our own fades credits both of its sides. Reporting a fade
    anywhere else would describe an overlap nobody rendered - we do not mix a
    realtime item's boundaries either, so it is a hard cut.
    """
    controller = _source_crossfade_controller(
        object.__new__(_UndecidedCrossfadingProvider), CrossfadeMode.SMART_CROSSFADE
    )
    queues = controller.mass.player_queues
    neighbour = _follower(neighbour_kind)
    if side == "follower":
        queues.get_next_item.return_value = neighbour
    else:
        queues.get_next_item.return_value = None
        queues.index_by_id.return_value = 1
        queues.get_item.return_value = neighbour
    queue_item = SimpleNamespace(
        queue_item_id="item-1",
        media_type=MediaType.TRACK,
        streamdetails=_make_stream_details(MediaType.TRACK, is_realtime=True),
    )

    assert controller.get_source_crossfade_mode(MagicMock(), queue_item) == expected


@pytest.mark.parametrize(
    ("provider", "neighbour_kind", "expected"),
    [
        # the source says it fades this item, so its own boundary answer stands even
        # where the queue offers nothing to fade against
        (_CrossfadingProvider, "none", CrossfadeMode.SOURCE),
        # and it says it does not, so the neighbour it happens to own is not credited:
        # a list predecessor that never played is exactly this case
        (_NonCrossfadingServingProvider, "same-provider", CrossfadeMode.DISABLED),
    ],
)
def test_a_serving_source_is_not_second_guessed_by_the_queue(
    provider: type[MusicProvider], neighbour_kind: str, expected: CrossfadeMode
) -> None:
    """
    A source already serving the item answers for its boundaries, and that answer stands.

    It knows what it fed and what it played across; the neighbour check is only a
    necessary condition, so applying it on top would both deny fades that happened
    and credit ones that did not.
    """
    controller = _source_crossfade_controller(
        object.__new__(provider), CrossfadeMode.SMART_CROSSFADE
    )
    controller.mass.player_queues.get_next_item.return_value = _follower(neighbour_kind)

    assert controller.get_source_crossfade_mode(MagicMock(), _realtime_track()) == expected


def test_the_raw_pcm_path_reports_a_source_fade_too() -> None:
    """
    A source fade reaches the processing details on the raw-PCM entry point as well.

    Gapless players are served per item straight from ``get_stream`` rather than over
    the http route, so leaving it out there would report nothing for most players.
    """
    queue_item = SimpleNamespace(
        queue_id="queue-1",
        queue_item_id="item-1",
        name="Track",
        media_type=MediaType.TRACK,
        media_item=None,
        streamdetails=_make_stream_details(MediaType.TRACK, is_realtime=True),
        extra_attributes={},
        image=None,
    )
    queue = SimpleNamespace(
        queue_id="queue-1",
        display_name="Queue",
        crossfade_enabled=True,
        overlay_enabled=False,
        overlay_source=None,
    )
    controller = cast("Any", object.__new__(StreamsController))
    controller.mass = MagicMock()
    controller.mass.player_queues.get.return_value = queue
    controller.mass.player_queues.get_item.return_value = queue_item
    controller.mass.players.get_player.return_value = None
    controller.logger = MagicMock()
    controller.audio = MagicMock()
    controller._update_audio_processing_context = MagicMock()
    controller.get_source_crossfade_mode = MagicMock(return_value=CrossfadeMode.SOURCE)
    media = SimpleNamespace(
        media_type=MediaType.TRACK,
        source_id="queue-1",
        queue_item_id="item-1",
        queue_session_id="session-1",
        uri="",
    )

    controller.get_stream(cast("Any", media), TEST_PCM_FORMAT)

    assert (
        controller._update_audio_processing_context.call_args.kwargs["source_crossfade_mode"]
        == CrossfadeMode.SOURCE
    )


def test_a_music_provider_declares_no_source_fade_by_default() -> None:
    """The declaration is opt-in: nothing downstream verifies it."""
    assert (
        object.__new__(MusicProvider).delivers_crossfaded_audio(
            _make_stream_details(MediaType.TRACK, is_realtime=True)
        )
        is False
    )


def _source_crossfade_controller(provider: Any, queue_crossfade_mode: CrossfadeMode) -> Any:
    """
    Return a controller resolving source fades against one provider and setting.

    The queue follows on with another item of the same source, so only the provider
    and the setting decide; the boundary cases are covered on their own.
    """
    controller = cast("Any", object.__new__(StreamsController))
    controller.mass = MagicMock()
    controller.mass.get_provider.return_value = provider
    controller.mass.player_queues.get_next_item.return_value = _follower("same-provider")
    # first in the queue, so nothing precedes it and only the follower decides
    controller.mass.player_queues.index_by_id.return_value = 0
    controller.get_crossfade_mode = MagicMock(return_value=queue_crossfade_mode)
    return controller


def _follower(kind: str) -> Any:
    """Return the queue item that follows, in each shape the resolver has to handle."""
    if kind == "none":
        return None
    if kind == "not-a-track":
        return SimpleNamespace(media_type=MediaType.AUDIOBOOK, streamdetails=None, media_item=None)
    if kind == "same-provider":
        # already resolved to the provider that will serve it
        return SimpleNamespace(
            media_type=MediaType.TRACK,
            streamdetails=_make_stream_details(MediaType.TRACK, is_realtime=True),
            media_item=None,
        )
    if kind == "other-provider":
        other = _make_stream_details(MediaType.TRACK, is_realtime=True)
        other.provider = "other--1"
        return SimpleNamespace(media_type=MediaType.TRACK, streamdetails=other, media_item=None)
    if kind == "library-mapped":
        return SimpleNamespace(
            media_type=MediaType.TRACK,
            streamdetails=None,
            media_item=SimpleNamespace(
                provider="library",
                provider_mappings=[SimpleNamespace(provider_instance="builtin")],
            ),
        )
    # nothing to resolve it with at all
    return SimpleNamespace(media_type=MediaType.TRACK, streamdetails=None, media_item=None)


def _realtime_track() -> Any:
    """Return a queue item whose track arrives at playback pace."""
    return SimpleNamespace(
        queue_item_id="item-1",
        media_type=MediaType.TRACK,
        streamdetails=_make_stream_details(MediaType.TRACK, is_realtime=True),
    )


def test_the_context_update_carries_a_source_fade_the_audio_layer_never_sees() -> None:
    """
    A crossfade performed by the source is published with the item's context.

    Our own mixer reports the fades it renders, but it renders none here, so this is
    the only place the source's fade can reach the processing details.
    """
    controller = cast("Any", object.__new__(StreamsController))
    controller.mass = MagicMock()
    controller.mass.player_queues.queue_data_or_none.return_value = SimpleNamespace(
        session_id="session-1"
    )
    controller.audio_processing = MagicMock()
    queue_item = SimpleNamespace(
        queue_item_id="item-1",
        streamdetails=_make_stream_details(MediaType.TRACK, is_realtime=True),
        extra_attributes={},
    )

    controller._update_audio_processing_context(
        queue=SimpleNamespace(queue_id="queue-1"),
        queue_item=queue_item,
        pcm_format=TEST_PCM_FORMAT,
        overlay_enabled=False,
        session_id="session-1",
        source_crossfade_mode=CrossfadeMode.SOURCE,
    )

    reported = controller.audio_processing.update_item_context.call_args.kwargs["queue_processing"]
    assert reported.crossfade_mode == CrossfadeMode.SOURCE


# -- StreamsAudio.get_stream_details --


@pytest.mark.parametrize(
    ("media_item_cls", "media_type", "expected_is_realtime"),
    [
        pytest.param(Radio, MediaType.RADIO, True, id="radio"),
        pytest.param(AudioSource, MediaType.AUDIO_SOURCE, True, id="audio_source"),
        pytest.param(Track, MediaType.TRACK, False, id="track"),
    ],
)
async def test_get_stream_details_sets_is_realtime_by_media_type(
    media_item_cls: type, media_type: MediaType, expected_is_realtime: bool
) -> None:
    """RADIO and AUDIO_SOURCE streams are marked realtime; a TRACK's flag is left alone."""
    provider_streamdetails = StreamDetails(
        provider="test--1",
        item_id="item-1",
        audio_format=AudioFormat(content_type=ContentType.MP3),
        media_type=media_type,
        stream_type=StreamType.CUSTOM,
        duration=180 if media_type == MediaType.TRACK else None,
    )
    audio = _stream_details_provider(provider_streamdetails)

    streamdetails = await audio.get_stream_details(
        queue_item=_queue_item_with_mapping(media_item_cls)
    )

    assert streamdetails.is_realtime is expected_is_realtime
