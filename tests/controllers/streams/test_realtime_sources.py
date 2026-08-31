"""Tests for the is_realtime gate across the buffer, holdback, and stream paths."""

from __future__ import annotations

import asyncio
import struct
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

from music_assistant.controllers.streams.audio import (
    MAX_SILENT_TAIL_HOLDBACK_SECONDS,
    MIN_CROSSFADE_DURATION,
    WARMUP_DURATION,
    CrossfadeData,
    StreamsAudio,
    _TailHold,
)
from music_assistant.controllers.streams.audio_buffer import AudioBuffer
from music_assistant.controllers.streams.constants import BufferSize
from music_assistant.controllers.streams.controller import StreamsController
from music_assistant.controllers.streams.smart_fades.fades import StandardCrossFade
from music_assistant.controllers.streams.smart_fades.helpers import SMART_CROSSFADE_DURATION
from music_assistant.helpers.audio import trailing_silence_bytes

# Standard test PCM format: 44100Hz, 16-bit, stereo
TEST_PCM_FORMAT = AudioFormat(
    content_type=ContentType.PCM_S16LE,
    sample_rate=44100,
    bit_depth=16,
    channels=2,
)

# One second of silence in the test format
ONE_SECOND_CHUNK = b"\x00" * TEST_PCM_FORMAT.pcm_sample_size


def _audio(pcm_format: AudioFormat, seconds: float) -> bytes:
    """
    Return PCM that reads as audio rather than as an item's trailing silence.

    The holdback measures the silent run a buffer ends with, so a fixture filled
    with zeroes would stand in for a track that has already finished.
    """
    frame = struct.pack("<2h", 9000, -9000)
    size = int(pcm_format.pcm_sample_size * seconds)
    return (frame * (size // len(frame) + 1))[:size]


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
        # the queue's crossfade setting buys nothing for a realtime source: its fade
        # streams in as it arrives, so a second of audio here would only be a second
        # of extra startup delay
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


# -- _TailHold --


async def test_tail_hold_grows_with_the_banked_surplus() -> None:
    """The holdback takes half of what arrived beyond the wall clock plus a reserve."""
    pcm_format = TEST_PCM_FORMAT
    frame_size = (pcm_format.bit_depth // 8) * pcm_format.channels
    audio_buffer = SimpleNamespace(eof=False, has_error=False, duration_available=2.0)
    queue_item = SimpleNamespace(
        streamdetails=SimpleNamespace(buffer=audio_buffer, duration=None, seek_position=0)
    )
    hold = _TailHold(pcm_format, cast("Any", queue_item))

    # nothing arrived yet: nothing may be held
    assert hold.hold_target(8 * pcm_format.pcm_sample_size, frame_size) == 0

    # Arrived well ahead of the wall clock, so half the spare exceeds a small window
    # and fills it outright. Sized off the reserve, so tuning that needs no
    # arithmetic here.
    reserve = _TailHold._LEAD_RESERVE_S
    arrived = reserve + 40.0
    hold.note_bytes(int(arrived * pcm_format.pcm_sample_size))
    hold._started = asyncio.get_event_loop().time() - 4.0
    assert hold.hold_target(8 * pcm_format.pcm_sample_size, frame_size) == (
        8 * pcm_format.pcm_sample_size
    )
    # against a window it cannot fill, half the spare is what is held. Read the clock
    # the way hold_target does, so the two agree on how much time has passed.
    elapsed = asyncio.get_event_loop().time() - hold._started
    larger = hold.hold_target(45 * pcm_format.pcm_sample_size, frame_size)
    assert larger % frame_size == 0
    expected = (arrived - elapsed - reserve) / 2
    assert abs(larger / pcm_format.pcm_sample_size - expected) < 0.5

    # barely above realtime: within the reserve nothing may be held at all
    fresh = _TailHold(pcm_format, cast("Any", queue_item))
    fresh.note_bytes(int((reserve - 1.0) * pcm_format.pcm_sample_size))
    fresh._started = asyncio.get_event_loop().time() - 4.0
    assert fresh.hold_target(8 * pcm_format.pcm_sample_size, frame_size) == 0

    # once the source is done, the rest is resident: full window regardless
    audio_buffer.eof = True
    assert (
        hold.hold_target(45 * pcm_format.pcm_sample_size, frame_size)
        == 45 * pcm_format.pcm_sample_size
    )


async def test_tail_hold_sees_a_buffer_attached_after_it_was_created() -> None:
    """Opening the stream is what creates the buffer, so its EOF must still be seen."""
    pcm_format = TEST_PCM_FORMAT
    frame_size = (pcm_format.bit_depth // 8) * pcm_format.channels
    # the tracker is built before the stream is opened, so there is no buffer yet
    streamdetails = SimpleNamespace(buffer=None, duration=None, seek_position=0)
    hold = _TailHold(pcm_format, cast("Any", SimpleNamespace(streamdetails=streamdetails)))
    hold.note_bytes(pcm_format.pcm_sample_size)
    hold._started = asyncio.get_event_loop().time()

    # a source that finished delivering releases the full window
    streamdetails.buffer = SimpleNamespace(eof=True, has_error=False)

    assert (
        hold.hold_target(45 * pcm_format.pcm_sample_size, frame_size)
        == 45 * pcm_format.pcm_sample_size
    )


async def test_tail_hold_counts_a_long_mix_as_listening_time() -> None:
    """Bytes noted across a long overlap must not read as a suspension and bank a surplus."""
    pcm_format = TEST_PCM_FORMAT
    frame_size = (pcm_format.bit_depth // 8) * pcm_format.channels
    queue_item = SimpleNamespace(
        streamdetails=SimpleNamespace(
            buffer=SimpleNamespace(eof=False, has_error=False), duration=None, seek_position=0
        )
    )
    hold = _TailHold(pcm_format, cast("Any", queue_item))

    # 20s of audio arrives over 20s of wall clock: the source is keeping pace, so
    # there is no surplus to hold back
    hold.note_bytes(pcm_format.pcm_sample_size)
    now = asyncio.get_event_loop().time()
    hold._started = now - 20.0
    hold._last_noted = now
    hold._received_bytes = 20 * pcm_format.pcm_sample_size

    assert hold.hold_target(45 * pcm_format.pcm_sample_size, frame_size) == 0


async def test_tail_hold_follows_a_capacity_reselection() -> None:
    """A reselection hands the item different details; the tracker must follow them."""
    pcm_format = TEST_PCM_FORMAT
    frame_size = (pcm_format.bit_depth // 8) * pcm_format.channels
    queue_item = SimpleNamespace(
        streamdetails=SimpleNamespace(buffer=None, duration=None, seek_position=0)
    )
    hold = _TailHold(pcm_format, cast("Any", queue_item))
    hold.note_bytes(pcm_format.pcm_sample_size)
    hold._started = asyncio.get_event_loop().time()

    # the source was reselected: the item carries a different streamdetails now
    queue_item.streamdetails = SimpleNamespace(
        buffer=SimpleNamespace(eof=True, has_error=False), duration=None, seek_position=0
    )

    assert (
        hold.hold_target(45 * pcm_format.pcm_sample_size, frame_size)
        == 45 * pcm_format.pcm_sample_size
    )


async def test_tail_hold_releases_everything_for_a_failed_source() -> None:
    """A failed source is skipped without a fade, so its remaining audio is played out."""
    pcm_format = TEST_PCM_FORMAT
    frame_size = (pcm_format.bit_depth // 8) * pcm_format.channels
    audio_buffer = SimpleNamespace(eof=True, has_error=True, duration_available=30.0)
    hold = _TailHold(
        pcm_format,
        cast(
            "Any",
            SimpleNamespace(
                streamdetails=SimpleNamespace(buffer=audio_buffer, duration=None, seek_position=0)
            ),
        ),
    )
    hold.note_bytes(27 * pcm_format.pcm_sample_size)
    hold._started = asyncio.get_event_loop().time() - 4.0

    assert hold.hold_target(8 * pcm_format.pcm_sample_size, frame_size) == 0


async def test_tail_hold_forgives_a_suspended_source() -> None:
    """A pause is not elapsed listening, so it does not erase the banked surplus."""
    pcm_format = TEST_PCM_FORMAT
    frame_size = (pcm_format.bit_depth // 8) * pcm_format.channels
    audio_buffer = SimpleNamespace(eof=False, has_error=False, duration_available=2.0)
    hold = _TailHold(
        pcm_format,
        cast(
            "Any",
            SimpleNamespace(
                streamdetails=SimpleNamespace(buffer=audio_buffer, duration=None, seek_position=0)
            ),
        ),
    )

    hold.note_bytes(27 * pcm_format.pcm_sample_size)
    hold._started = asyncio.get_event_loop().time() - 4.0
    # the source went quiet for a while, then resumed
    hold._last_noted = asyncio.get_event_loop().time() - 30.0
    hold.note_bytes(pcm_format.pcm_sample_size)

    assert hold.hold_target(8 * pcm_format.pcm_sample_size, frame_size) > 0


async def test_tail_hold_works_without_a_source_buffer() -> None:
    """A source without a buffer still banks a holdback out of what it delivered."""
    pcm_format = TEST_PCM_FORMAT
    frame_size = (pcm_format.bit_depth // 8) * pcm_format.channels
    hold = _TailHold(
        pcm_format,
        cast(
            "Any",
            SimpleNamespace(
                streamdetails=SimpleNamespace(buffer=None, duration=None, seek_position=0)
            ),
        ),
    )

    hold.note_bytes(27 * pcm_format.pcm_sample_size)
    hold._started = asyncio.get_event_loop().time() - 4.0

    assert hold.hold_target(8 * pcm_format.pcm_sample_size, frame_size) > 0


async def test_tail_hold_counts_a_carried_lead_as_already_banked() -> None:
    """A lead earned before this stream started is holdback the source need not re-earn."""
    pcm_format = TEST_PCM_FORMAT
    frame_size = (pcm_format.bit_depth // 8) * pcm_format.channels
    audio_buffer = SimpleNamespace(eof=False, has_error=False, duration_available=2.0)
    queue_item = SimpleNamespace(
        streamdetails=SimpleNamespace(buffer=audio_buffer, duration=None, seek_position=0)
    )
    max_bytes = 45 * pcm_format.pcm_sample_size

    # a source barely above playback pace: 4s delivered in 4s banks nothing on its own
    fresh = _TailHold(pcm_format, cast("Any", queue_item))
    fresh.note_bytes(4 * pcm_format.pcm_sample_size)
    fresh._started = asyncio.get_event_loop().time() - 4.0
    assert fresh.hold_target(max_bytes, frame_size) == 0

    # the same stream, handed a lead from the boundary it faded in across: the carry
    # plus what arrived, less the reserve, half of which may be held. Sized off the
    # reserve so tuning that needs no arithmetic here.
    now = asyncio.get_event_loop().time()
    reserve = _TailHold._LEAD_RESERVE_S
    carry = reserve + 17.0
    carried = _TailHold(pcm_format, cast("Any", queue_item), carried_lead=carry, carried_at=now)
    carried.note_bytes(4 * pcm_format.pcm_sample_size)
    carried._started = now - 4.0
    expected = (carry - reserve) / 2
    target = carried.hold_target(max_bytes, frame_size)
    assert target % frame_size == 0
    assert int((expected - 0.5) * pcm_format.pcm_sample_size) < target
    assert target <= int(expected * pcm_format.pcm_sample_size)

    # a negative carry is not a way to owe the player audio
    assert _TailHold(pcm_format, cast("Any", queue_item), carried_lead=-50.0)._carried_lead == 0.0


async def test_the_banked_lead_counts_emitted_audio_not_what_arrived() -> None:
    """
    Only emitted audio may seed the next item, never what a source delivered.

    A fade consumes an overlap from both tracks and emits it once, so crediting
    arrivals banks a lead the player never received, and carrying that compounds.
    """
    pcm_format = TEST_PCM_FORMAT
    pss = pcm_format.pcm_sample_size
    queue_item = SimpleNamespace(
        streamdetails=SimpleNamespace(buffer=None, duration=None, seek_position=0)
    )

    # nothing streamed yet: nothing banked
    hold = _TailHold(pcm_format, cast("Any", queue_item), carried_lead=10.0)
    assert hold.banked_lead(30 * pss) == 0.0

    # 30s emitted in 10s, on top of a 10s carry
    now = asyncio.get_event_loop().time()
    hold.note_bytes(30 * pss)
    hold._first_noted = now - 10.0
    assert 29.5 < hold.banked_lead(30 * pss) <= 30.0

    # the source handed over 30s but the mix only emitted 12s of it: the 18s it
    # consumed for the overlap and the planner's trim never reached the player
    assert 11.5 < hold.banked_lead(12 * pss) <= 12.0

    # a stream that fell behind the wall clock reports no lead, never a debt
    behind = _TailHold(pcm_format, cast("Any", queue_item))
    behind.note_bytes(pss)
    behind._first_noted = asyncio.get_event_loop().time() - 30.0
    assert behind.banked_lead(pss) == 0.0

    # A stalled source is not lead, however much note_bytes forgave it. That
    # forgiveness moves _started, which is what keeps the in-track holdback alive
    # across a suspension; the banked value reads the unforgiven clock instead, so
    # 30s emitted over 40 real seconds is no lead whatever _started was moved to.
    frame_size = (pcm_format.bit_depth // 8) * pcm_format.channels
    stalled = _TailHold(pcm_format, cast("Any", queue_item))
    stalled.note_bytes(30 * pss)
    stalled._started = asyncio.get_event_loop().time() - 10.0
    stalled._first_noted = asyncio.get_event_loop().time() - 40.0
    assert stalled.banked_lead(30 * pss) == 0.0
    assert stalled.hold_target(45 * pss, frame_size) > 0


async def test_a_carried_lead_is_aged_by_the_gap_before_the_stream_starts() -> None:
    """The player drains while a boundary is worked out, so the carry must shrink too."""
    pcm_format = TEST_PCM_FORMAT
    frame_size = (pcm_format.bit_depth // 8) * pcm_format.channels
    queue_item = SimpleNamespace(
        streamdetails=SimpleNamespace(buffer=None, duration=None, seek_position=0)
    )
    now = asyncio.get_event_loop().time()
    max_bytes = 45 * pcm_format.pcm_sample_size

    # a 20s lead measured 15s ago is only ~5s of audio by the time this stream
    # produces, and 5 - 3 (reserve) halved is under a second of holdback
    stale = _TailHold(pcm_format, cast("Any", queue_item), carried_lead=20.0, carried_at=now - 15.0)
    stale.note_bytes(pcm_format.pcm_sample_size)
    assert stale.banked_lead(0) < 6.0
    assert stale.hold_target(max_bytes, frame_size) < 1.5 * pcm_format.pcm_sample_size

    # the same lead measured just now survives intact
    fresh = _TailHold(pcm_format, cast("Any", queue_item), carried_lead=20.0, carried_at=now)
    fresh.note_bytes(pcm_format.pcm_sample_size)
    assert fresh.banked_lead(0) > 19.0

    # a lead older than itself is spent, not a debt the next item owes
    ancient = _TailHold(
        pcm_format, cast("Any", queue_item), carried_lead=5.0, carried_at=now - 600.0
    )
    ancient.note_bytes(pcm_format.pcm_sample_size)
    assert ancient.banked_lead(0) <= 1.0
    assert ancient.hold_target(max_bytes, frame_size) == 0


def test_trailing_silence_is_measured_at_the_threshold_that_strips_it() -> None:
    """
    Quiet counts as silence at the same threshold the mixer's strip uses.

    Exact zeroes are not the interesting case: a track that ends by fading out is
    below the threshold long before it reaches them, and that is the audio the fade
    would otherwise be handed.
    """
    pcm_format = AudioFormat(
        content_type=ContentType.PCM_S16LE, sample_rate=8000, bit_depth=16, channels=1
    )
    # s16le full scale is 32768, so the threshold sits at 655
    loud = struct.pack("<4h", 9000, -9000, 9000, -9000)
    quiet = struct.pack("<4h", 100, -100, 40, 0)

    assert trailing_silence_bytes(loud, pcm_format, 0) == 0
    # audible then quiet: only the quiet end counts, two bytes per sample
    assert trailing_silence_bytes(loud + quiet, pcm_format, 0) == 8
    # a wholly quiet chunk continues the run the buffer already ended with
    assert trailing_silence_bytes(quiet, pcm_format, 8) == 16
    # audible again ends the run, whatever was carried
    assert trailing_silence_bytes(quiet + loud, pcm_format, 100) == 0
    # a flavour that cannot be read sample-wise is not guessed at
    unreadable = AudioFormat(
        content_type=ContentType.PCM_S24LE, sample_rate=8000, bit_depth=24, channels=1
    )
    assert trailing_silence_bytes(quiet, unreadable, 8) == 0


async def test_the_holdback_fills_with_audio_not_an_items_padding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A tail of digital silence must not be what the fade gets to blend.

    A live engine read above playback pace runs off the end of its content and its
    sink pads the rest with zeroes. Counting that padding as the window hands the
    mixer a silent tail and the fade collapses to nothing.
    """
    pcm_format = AudioFormat(
        content_type=ContentType.PCM_S16LE, sample_rate=8000, bit_depth=16, channels=2
    )
    pss = pcm_format.pcm_sample_size
    frames = pcm_format.sample_rate
    # a fading ending is below the strip threshold long before it reaches zero, so
    # the tail this has to reach past is quiet audio, not digital silence
    music = struct.pack("<2h", 9000, -9000) * (frames // 1)
    quiet = struct.pack("<2h", 90, -90) * (frames // 1)

    current_details = SimpleNamespace(
        duration=24,
        seek_position=0,
        seconds_streamed=0,
        uri="test://current",
        buffer=SimpleNamespace(
            eof=True,
            cancelled=False,
            has_error=False,
            max_size_seconds=300,
            duration_available=0.0,
        ),
        is_realtime=True,
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
        streamdetails=SimpleNamespace(
            audio_format=pcm_format,
            buffer=_buffer(SMART_CROSSFADE_DURATION, ready=True),
            duration=24,
            seek_position=0,
            uri="test://next",
            is_realtime=False,
            volume_normalization_mode=None,
        ),
        extra_attributes={},
        available=True,
    )
    mass = MagicMock()
    mass.player_queues.get.return_value = SimpleNamespace(
        queue_id="queue-1", display_name="Queue", index_in_buffer=0
    )
    mass.player_queues.load_next_queue_item = AsyncMock(return_value=next_item)
    mass.player_queues.index_by_id.return_value = 1
    audio = StreamsAudio(cast("Any", mass))
    audio.setup()
    audio.select_pcm_format = AsyncMock(return_value=pcm_format)  # type: ignore[method-assign]
    audio.crossfade_allowed = MagicMock(return_value=True)  # type: ignore[method-assign]

    captured: dict[str, bytes] = {}

    async def _capture_build(**kwargs: Any) -> object:
        captured["fade_out"] = kwargs["fade_out_data"]
        raise RuntimeError("the tail is all this test needs")

    monkeypatch.setattr(audio.smart_fades_mixer, "build", _capture_build)

    async def _item_stream(
        _queue_item: object, *_args: object, **_kwargs: object
    ) -> AsyncGenerator[bytes]:
        # warmup, then real audio, then the engine running off the end of it
        for _ in range(WARMUP_DURATION + 12):
            yield music
        for _ in range(10):
            yield quiet

    monkeypatch.setattr(audio, "get_queue_item_stream", _item_stream)
    stream = audio.get_queue_item_stream_with_smartfade(
        cast("Any", SimpleNamespace(player_id="player-1", name="Player")),
        cast("Any", current_item),
        pcm_format,
        crossfade_mode=CrossfadeMode.SMART_CROSSFADE,
        standard_crossfade_duration=8,
    )
    async for _chunk in stream:
        pass

    tail = captured["fade_out"]
    silent = trailing_silence_bytes(tail, pcm_format, 0)
    audible = (len(tail) - silent) / pss
    # duration 24 caps the window at 12s, and all 12 must be audio: counting the
    # padding as the window would leave only the 2s of music that precedes it
    assert audible >= 11.0, f"only {audible:.2f}s of audible tail for the fade"
    assert silent / pss <= MAX_SILENT_TAIL_HOLDBACK_SECONDS


async def test_a_tail_with_nothing_audible_left_is_a_cut_not_a_token_fade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A boundary with no audible tail left ships gapless, not a fraction of a fade.

    An item that ran out of headroom holds back mostly the silence it ended with.
    Blending the sliver of audio underneath it is heard as a cut anyway, so the
    honest outcome is no fade and the mixer is never asked for one.
    """
    pcm_format = AudioFormat(
        content_type=ContentType.PCM_S16LE, sample_rate=8000, bit_depth=16, channels=2
    )
    current_item = SimpleNamespace(
        queue_id="queue-1",
        queue_item_id="current",
        name="Current",
        streamdetails=SimpleNamespace(
            duration=60,
            seek_position=0,
            seconds_streamed=0,
            uri="test://current",
            buffer=SimpleNamespace(
                eof=True,
                cancelled=False,
                has_error=False,
                max_size_seconds=300,
                duration_available=0.0,
            ),
            is_realtime=True,
        ),
        extra_attributes={},
    )
    next_item = SimpleNamespace(
        queue_id="queue-1",
        queue_item_id="next",
        name="Next",
        streamdetails=SimpleNamespace(
            audio_format=pcm_format,
            buffer=_buffer(SMART_CROSSFADE_DURATION, ready=True),
            duration=60,
            seek_position=0,
            uri="test://next",
            is_realtime=False,
            volume_normalization_mode=None,
        ),
        extra_attributes={},
        available=True,
    )
    mass = MagicMock()
    mass.player_queues.get.return_value = SimpleNamespace(
        queue_id="queue-1", display_name="Queue", index_in_buffer=0
    )
    mass.player_queues.load_next_queue_item = AsyncMock(return_value=next_item)
    mass.player_queues.index_by_id.return_value = 1
    audio = StreamsAudio(cast("Any", mass))
    audio.setup()
    audio.select_pcm_format = AsyncMock(return_value=pcm_format)  # type: ignore[method-assign]
    audio.crossfade_allowed = MagicMock(return_value=True)  # type: ignore[method-assign]

    asked: list[int] = []

    async def _build(**kwargs: Any) -> object:
        # a raise here would be swallowed by the fallback and prove nothing, so
        # record the ask and hand back a real fade
        asked.append(len(kwargs["fade_out_data"]))
        standard = StandardCrossFade(logger=MagicMock(), crossfade_duration=8)
        standard.build(len(kwargs["fade_out_data"]), kwargs["fade_in_bytes_len"], pcm_format)
        return standard

    monkeypatch.setattr(audio.smart_fades_mixer, "build", _build)
    monkeypatch.setattr(audio.smart_fades_mixer, "mix", _empty_mix)

    async def _item_stream(
        _queue_item: object, *_args: object, **_kwargs: object
    ) -> AsyncGenerator[bytes]:
        # the warmup goes straight out, then the item runs out and pads its tail
        yield _audio(pcm_format, WARMUP_DURATION)
        yield b"\x00" * int(pcm_format.pcm_sample_size * 30)

    monkeypatch.setattr(audio, "get_queue_item_stream", _item_stream)
    stream = audio.get_queue_item_stream_with_smartfade(
        cast("Any", SimpleNamespace(player_id="player-1", name="Player")),
        cast("Any", current_item),
        pcm_format,
        crossfade_mode=CrossfadeMode.SMART_CROSSFADE,
        standard_crossfade_duration=8,
    )
    async for _chunk in stream:
        pass

    # the mixer is never asked, and nothing is stashed: the next item starts on its
    # own audio, gapless
    assert asked == [], f"a fade was planned from {asked[0] if asked else 0} bytes of tail"
    assert "queue-1" not in audio._crossfade_data


# -- StreamsAudio._select_buffered_crossfade --


def test_the_held_tail_sizes_the_fade_the_configured_mode_picks() -> None:
    """The mode decides which fade is applied; the held tail only sizes its window."""
    audio = StreamsAudio(MagicMock())

    # a realtime source barely delivers, yet the tail it banked carries the window:
    # the incoming side streams in while the blend plays
    mode, duration = audio._select_buffered_crossfade(
        _streamdetails_for_crossfade(_buffer(2, ready=True), is_realtime=True),
        CrossfadeMode.SMART_CROSSFADE,
        standard_crossfade_duration=8,
        fade_out_seconds=20,
    )
    assert (mode, duration) == (CrossfadeMode.SMART_CROSSFADE, 20)

    # a shorter tail keeps the smart fade, on a shorter window
    mode, duration = audio._select_buffered_crossfade(
        _streamdetails_for_crossfade(_buffer(2, ready=True), is_realtime=True),
        CrossfadeMode.SMART_CROSSFADE,
        standard_crossfade_duration=8,
        fade_out_seconds=6,
    )
    assert (mode, duration) == (CrossfadeMode.SMART_CROSSFADE, 6)

    # a standard fade never exceeds the configured overlap
    mode, duration = audio._select_buffered_crossfade(
        _streamdetails_for_crossfade(_buffer(2, ready=True), is_realtime=True),
        CrossfadeMode.STANDARD_CROSSFADE,
        standard_crossfade_duration=8,
        fade_out_seconds=20,
    )
    assert (mode, duration) == (CrossfadeMode.STANDARD_CROSSFADE, 8)


def test_a_finished_incoming_source_caps_the_window_at_what_it_holds() -> None:
    """A source that already ended has no more audio than what is resident."""
    audio = StreamsAudio(MagicMock())

    mode, duration = audio._select_buffered_crossfade(
        _streamdetails_for_crossfade(_buffer(6, ready=True, eof=True), is_realtime=True),
        CrossfadeMode.SMART_CROSSFADE,
        standard_crossfade_duration=8,
        fade_out_seconds=45,
    )

    assert (mode, duration) == (CrossfadeMode.SMART_CROSSFADE, 6)


def test_a_short_incoming_track_caps_the_window() -> None:
    """A long tail cannot claim more overlap than the next track can supply."""
    audio = StreamsAudio(MagicMock())
    streamdetails = _streamdetails_for_crossfade(_buffer(2, ready=True), is_realtime=True)
    streamdetails.duration = 20

    mode, duration = audio._select_buffered_crossfade(
        streamdetails,
        CrossfadeMode.SMART_CROSSFADE,
        standard_crossfade_duration=8,
        fade_out_seconds=45,
    )

    assert (mode, duration) == (CrossfadeMode.SMART_CROSSFADE, 10)


def test_a_tail_too_short_to_blend_skips_the_fade() -> None:
    """Below the minimum overlap the tail plays out and the boundary is a hard cut."""
    audio = StreamsAudio(MagicMock())

    mode, duration = audio._select_buffered_crossfade(
        _streamdetails_for_crossfade(_buffer(20, ready=True), is_realtime=True),
        CrossfadeMode.SMART_CROSSFADE,
        standard_crossfade_duration=8,
        fade_out_seconds=MIN_CROSSFADE_DURATION - 0.5,
    )

    assert mode == CrossfadeMode.DISABLED
    assert duration == 0


def test_realtime_incoming_source_not_yet_delivering_skips_the_fade() -> None:
    """A realtime source whose buffer is not ready yet means the boundary plays clean."""
    audio = StreamsAudio(MagicMock())

    mode, duration = audio._select_buffered_crossfade(
        _streamdetails_for_crossfade(_buffer(0, ready=False), is_realtime=True),
        CrossfadeMode.STANDARD_CROSSFADE,
        standard_crossfade_duration=8,
        fade_out_seconds=8,
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
        buffer=SimpleNamespace(
            eof=True, cancelled=False, has_error=False, max_size_seconds=300, duration_available=0.0
        ),
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
        yield _audio(pcm_format, 8)
        yield _audio(pcm_format, 8)

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


async def _run_smartfade_for_lead(
    monkeypatch: pytest.MonkeyPatch,
    audio: StreamsAudio,
    pcm_format: AudioFormat,
    carried_seen: list[float],
) -> None:
    """Stream one faded item, recording the lead each _TailHold was seeded with."""
    real_tail_hold = _TailHold

    def _spy(*args: Any, **kwargs: Any) -> _TailHold:
        carried_seen.append(float(kwargs.get("carried_lead", 0.0)))
        return real_tail_hold(*args, **kwargs)

    monkeypatch.setattr("music_assistant.controllers.streams.audio._TailHold", _spy)

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
        streamdetails=SimpleNamespace(
            duration=16,
            seek_position=0,
            seconds_streamed=0,
            uri="test://current",
            buffer=SimpleNamespace(
                eof=True,
                cancelled=False,
                has_error=False,
                max_size_seconds=300,
                duration_available=0.0,
            ),
            is_realtime=True,
        ),
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
    mass = cast("Any", audio.mass)
    mass.player_queues.get.return_value = SimpleNamespace(
        queue_id="queue-1", display_name="Queue", index_in_buffer=0
    )
    mass.player_queues.load_next_queue_item = AsyncMock(return_value=next_item)
    mass.player_queues.index_by_id.return_value = 1
    audio.select_pcm_format = AsyncMock(return_value=pcm_format)  # type: ignore[method-assign]
    audio.crossfade_allowed = MagicMock(return_value=True)  # type: ignore[method-assign]
    monkeypatch.setattr(
        audio.smart_fades_mixer,
        "build",
        AsyncMock(
            return_value=SimpleNamespace(
                timing_info=SimpleNamespace(
                    fadein_trimmed_duration=0.0,
                    crossfade_duration=8.0,
                    pre_crossfade_duration=0.0,
                )
            )
        ),
    )

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
        _queue_item: object, *_args: object, **_kwargs: object
    ) -> AsyncGenerator[bytes]:
        yield _audio(pcm_format, 8)
        yield _audio(pcm_format, 8)

    monkeypatch.setattr(audio, "get_queue_item_stream", _item_stream)
    stream = audio.get_queue_item_stream_with_smartfade(
        cast("Any", SimpleNamespace(player_id="player-1", name="Player")),
        cast("Any", current_item),
        pcm_format,
        crossfade_mode=CrossfadeMode.STANDARD_CROSSFADE,
        standard_crossfade_duration=8,
    )
    async for _chunk in stream:
        pass


async def test_a_lead_is_only_carried_across_a_fade_that_handed_over(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A banked lead is holdback for the next item, but only if the fade reached it."""
    pcm_format = AudioFormat(
        content_type=ContentType.PCM_S16LE, sample_rate=8000, bit_depth=16, channels=2
    )
    audio = StreamsAudio(MagicMock())
    audio.setup()

    # this item was faded into, so the lead its predecessor banked is still the player's
    audio._playback_lead["queue-1"] = (20.0, asyncio.get_event_loop().time())
    audio._crossfade_data["queue-1"] = CrossfadeData(
        data=b"",
        fade_in_media_duration=0.0,
        pcm_format=pcm_format,
        queue_item_id="current",
    )
    carried: list[float] = []
    await _run_smartfade_for_lead(monkeypatch, audio, pcm_format, carried)
    assert carried == [20.0]
    # and this item banked its own lead for whatever follows it, stamped with the time
    banked, banked_at = audio._playback_lead["queue-1"]
    assert banked > 0
    assert banked_at > 0

    # a start with nothing handed over cannot trust a lead measured before the break:
    # the player's buffer is unaccounted for, so the holdback is earned again from zero
    audio._playback_lead["queue-1"] = (20.0, asyncio.get_event_loop().time())
    audio._crossfade_data.pop("queue-1", None)
    carried.clear()
    await _run_smartfade_for_lead(monkeypatch, audio, pcm_format, carried)
    assert carried == [0.0]


async def test_the_handoff_is_claimed_before_the_fade_is_even_sized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The claim must beat the awaits that size the fade, not follow them.

    Sizing a fade waits on the incoming source, up to REALTIME_FADE_SOURCE_WAIT. A
    speaker can ask for that item's url inside that window, and it has nothing to
    wait for unless the claim is already registered.
    """
    pcm_format = AudioFormat(
        content_type=ContentType.PCM_S16LE, sample_rate=8000, bit_depth=16, channels=2
    )
    audio = StreamsAudio(MagicMock())
    audio.setup()
    claimed_during_sizing = asyncio.Event()

    async def _slow_sizing(_streamdetails: object) -> None:
        # stands in for the wait on a realtime incoming source
        if "queue-1" in audio._crossfade_pending:
            claimed_during_sizing.set()
        await asyncio.sleep(0)

    monkeypatch.setattr(audio, "_await_realtime_fade_source", _slow_sizing)
    carried: list[float] = []
    await _run_smartfade_for_lead(monkeypatch, audio, pcm_format, carried)

    assert claimed_during_sizing.is_set(), (
        "the incoming item had nothing to wait for while its fade was being sized"
    )
    # and the claim is gone once the boundary is done with it
    assert "queue-1" not in audio._crossfade_pending


async def test_the_incoming_item_waits_for_a_fade_still_being_mixed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A speaker asking for the next url early must not lose a nearly-ready fade."""
    # the real bound has a speaker waiting on its first byte, so it is seconds long;
    # this test only cares that the wait is bounded at all
    monkeypatch.setattr("music_assistant.controllers.streams.audio.CROSSFADE_HANDOFF_WAIT", 0.2)
    pcm_format = AudioFormat(
        content_type=ContentType.PCM_S16LE, sample_rate=8000, bit_depth=16, channels=2
    )
    audio = StreamsAudio(MagicMock())
    queue = cast("Any", SimpleNamespace(queue_id="queue-1", display_name="Queue"))
    item = cast("Any", SimpleNamespace(queue_item_id="next", name="Next"))

    # nothing being mixed: the caller is told so straight away
    assert await audio._await_pending_crossfade(queue, item) is None

    # a fade being mixed for a different item is not this item's to wait for
    audio._crossfade_pending["queue-1"] = ("other", asyncio.Event())
    assert await audio._await_pending_crossfade(queue, item) is None

    # a fade being mixed for this item is waited for, and picked up when it lands
    handoff = asyncio.Event()
    audio._crossfade_pending["queue-1"] = ("next", handoff)
    expected = CrossfadeData(
        data=b"", fade_in_media_duration=0.0, pcm_format=pcm_format, queue_item_id="next"
    )

    async def _land_it() -> None:
        await asyncio.sleep(0.05)
        audio._crossfade_data["queue-1"] = expected
        handoff.set()

    task = asyncio.create_task(_land_it())
    assert await audio._await_pending_crossfade(queue, item) is expected
    await task

    # a mix that never finishes costs the fade, not the stream
    audio._crossfade_data.pop("queue-1", None)
    audio._crossfade_pending["queue-1"] = ("next", asyncio.Event())
    started = asyncio.get_event_loop().time()
    assert await audio._await_pending_crossfade(queue, item) is None
    assert asyncio.get_event_loop().time() - started >= 0.2


async def test_smartfade_still_filling_source_fades_from_what_it_banked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A source still delivering fades from the audio it banked ahead of playback."""
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
        yield _audio(pcm_format, 8)
        yield _audio(pcm_format, 8)

    monkeypatch.setattr(audio, "get_queue_item_stream", _item_stream)
    stream = audio.get_queue_item_stream_with_smartfade(
        cast("Any", player),
        cast("Any", current_item),
        pcm_format,
        crossfade_mode=CrossfadeMode.STANDARD_CROSSFADE,
        standard_crossfade_duration=8,
    )

    output = b"".join([chunk async for chunk in stream])

    # how much tail the holdback banked depends on the wall clock, so only the
    # invariants are asserted: the source's own audio is all there, and it faded
    assert len(output) >= pcm_format.pcm_sample_size * 16
    build.assert_awaited_once()
    crossfade_data = audio._crossfade_data.get("queue-1")
    assert crossfade_data is not None
    assert crossfade_data.queue_item_id == "next"


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
        _audio(pcm_format, 8),
        _audio(pcm_format, 8),
    ]
    next_chunks = [_audio(pcm_format, 2)]

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
        yield _audio(pcm_format, 8)
        for _ in range(30):
            yield _audio(pcm_format, 1 / 3)

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
        yield _audio(pcm_format, 8)
        yield _audio(pcm_format, 26)

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
        yield _audio(pcm_format, 8)
        yield _audio(pcm_format, 2)

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


async def test_flow_reports_no_fade_for_a_realtime_item_until_one_renders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A realtime item is not credited with any fade up front.

    A fade is only reported once one is really rendered at its boundary; the
    source-delegation reporting is gone along with the delegation itself.
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
        yield _audio(pcm_format, 4)

    monkeypatch.setattr(audio, "get_queue_item_stream", _item_stream)
    stream = audio.get_queue_flow_stream(
        cast("Any", queue), cast("Any", realtime_item), pcm_format, session_id="session-1"
    )

    async for _chunk in stream:
        pass

    update_item_context.assert_called()
    reported = update_item_context.call_args.kwargs["queue_processing"]
    assert reported.crossfade_mode == CrossfadeMode.DISABLED


@pytest.mark.parametrize("mix_emits_audio", [False, True])
async def test_flow_carries_its_lead_from_one_track_to_the_next(
    monkeypatch: pytest.MonkeyPatch,
    mix_emits_audio: bool,
) -> None:
    """One flow stream feeds the whole queue, so the lead it earned is not remeasured."""
    pcm_format = AudioFormat(
        content_type=ContentType.PCM_S16LE,
        sample_rate=8000,
        bit_depth=16,
        channels=2,
    )
    carried_seen: list[float] = []
    real_tail_hold = _TailHold

    def _spy(*args: Any, **kwargs: Any) -> _TailHold:
        carried_seen.append(float(kwargs.get("carried_lead", 0.0)))
        return real_tail_hold(*args, **kwargs)

    monkeypatch.setattr("music_assistant.controllers.streams.audio._TailHold", _spy)

    def _details(uri: str) -> SimpleNamespace:
        # each track is both the outgoing and the incoming side of a boundary here,
        # so the buffer has to satisfy both: resident audio and a finished source
        return SimpleNamespace(
            audio_format=pcm_format,
            buffer=_buffer(SMART_CROSSFADE_DURATION, ready=True, eof=True),
            fade_in=False,
            stream_error=False,
            uri=uri,
            seek_position=0,
            seconds_streamed=0,
            duration=300,
            is_realtime=False,
            volume_normalization_mode=None,
        )

    def _item(item_id: str, name: str, details: SimpleNamespace) -> SimpleNamespace:
        return SimpleNamespace(
            queue_id="queue-1",
            queue_item_id=item_id,
            name=name,
            media_type=MediaType.TRACK,
            media_item=None,
            streamdetails=details,
            duration=300,
            extra_attributes={},
        )

    first_item = _item("item-1", "First", _details("test://first"))
    second_item = _item("item-2", "Second", _details("test://second"))
    third_item = _item("item-3", "Third", _details("test://third"))
    fourth_item = _item("item-4", "Fourth", _details("test://fourth"))
    queue = SimpleNamespace(
        queue_id="queue-1",
        display_name="Queue",
        flow_mode=True,
        overlay_enabled=False,
        overlay_source=None,
    )
    mass = MagicMock()
    mass.player_queues.queue_data.return_value = SimpleNamespace(
        session_id="session-1", flow_mode_stream_log=[]
    )
    mass.player_queues.load_next_queue_item = AsyncMock(
        side_effect=[second_item, third_item, fourth_item, QueueEmpty]
    )
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
    # a standard fade, so every boundary really runs the mixer and its overlap
    standard = StandardCrossFade(logger=MagicMock(), crossfade_duration=8)
    standard.build(
        pcm_format.pcm_sample_size * SMART_CROSSFADE_DURATION,
        pcm_format.pcm_sample_size * SMART_CROSSFADE_DURATION,
        pcm_format,
    )
    monkeypatch.setattr(audio.smart_fades_mixer, "build", AsyncMock(return_value=standard))

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

    # both shapes matter: a mix that emits audio runs the split between this item and
    # the outgoing share it also has to count, while one that emits none leaves the
    # bound below tight enough to fail on an arrivals-based carry (45s claimed on 16s)
    monkeypatch.setattr(
        audio.smart_fades_mixer, "mix", _concat_mix if mix_emits_audio else _empty_mix
    )

    async def _item_stream(*_args: object, **_kwargs: object) -> AsyncGenerator[bytes]:
        # delivered far faster than playback, so this track banks a real lead, and
        # enough of it that the holdback arms and every boundary really mixes
        for _ in range(60):
            yield bytes(pcm_format.pcm_sample_size)

    monkeypatch.setattr(audio, "get_queue_item_stream", _item_stream)
    stream = audio.get_queue_flow_stream(
        cast("Any", queue), cast("Any", first_item), pcm_format, session_id="session-1"
    )
    emitted_at_carry: list[float] = []
    emitted = 0
    seen = 0
    async for chunk in stream:
        emitted += len(chunk)
        # record what had actually gone out by the time each carry was claimed
        while seen < len(carried_seen):
            emitted_at_carry.append(emitted / pcm_format.pcm_sample_size)
            seen += 1

    # the first track starts from nothing; the later ones inherit what was earned
    assert len(carried_seen) >= 3
    assert carried_seen[0] == 0.0
    assert carried_seen[1] > 0.0

    # No carry may exceed the audio the player was actually sent by then. Crediting
    # what a source delivered instead double-counts every fade's overlap, which
    # compounds into a holdback larger than the real lead - the generator then
    # withholds audio the player needs and it drops out mid-track. On the arrivals
    # basis the first boundary here claims 45s of lead on 16s of emitted audio.
    for index, carried in enumerate(carried_seen):
        assert carried <= emitted_at_carry[index] + 0.001, (
            f"carry {index} claimed {carried}s of lead with only {emitted_at_carry[index]}s emitted"
        )


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


async def test_single_item_handler_keeps_crossfade_for_a_buffered_item() -> None:
    """A buffered item still gets the queue's configured crossfade."""
    controller, request, seen = _single_item_handler(is_realtime=False)

    with pytest.raises(_PcmFormatRequested):
        await controller.serve_queue_item_stream(request)

    assert seen["crossfade_enabled"] is True
    controller.get_crossfade_mode.assert_called_once()


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
