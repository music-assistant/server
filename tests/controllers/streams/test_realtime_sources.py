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
    tail_hold_target,
)
from music_assistant.controllers.streams.audio_buffer import AudioBuffer
from music_assistant.controllers.streams.constants import (
    SINGLE_ITEM_LEAD_OUT_MAX_SECONDS,
    BufferSize,
)
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


async def test_the_response_waits_for_the_player_to_play_out_its_backlog() -> None:
    """
    A response that handed over faster than playback must not close on the backlog.

    Some players drop whatever they are still holding the moment the response closes,
    which takes the end of the track with it - heard as a silence where the track
    should have finished. So the idle connection is held for as long as the player
    still has audio to render, and abandoned if the queue moves on meanwhile.
    """
    controller = StreamsController.__new__(StreamsController)
    controller.logger = MagicMock()
    queue = cast("Any", SimpleNamespace(display_name="Queue"))
    loop = asyncio.get_event_loop()

    def _item(streamed: float | None) -> Any:
        return cast(
            "Any",
            SimpleNamespace(name="Track", streamdetails=SimpleNamespace(seconds_streamed=streamed)),
        )

    live = cast("Any", SimpleNamespace(session_id="s1"))

    # handed over 2s of audio in about no time: the wait is that whole backlog
    started = loop.time()
    await controller._await_player_playout(queue, _item(2.0), "s1", live, started)
    assert 1.5 < loop.time() - started < 3.0

    # handed over no faster than it plays: nothing is waiting, so no wait
    started = loop.time()
    await controller._await_player_playout(queue, _item(2.0), "s1", live, started - 10.0)
    assert loop.time() - started < 0.2

    # nothing streamed (a failed item): no wait
    started = loop.time()
    await controller._await_player_playout(queue, _item(None), "s1", live, started)
    assert loop.time() - started < 0.2

    # a skip takes over mid-wait: the player wants the next stream now
    taken_over = cast("Any", SimpleNamespace(session_id="s1"))

    async def _skip() -> None:
        await asyncio.sleep(0.3)
        taken_over.session_id = "s2"

    task = asyncio.create_task(_skip())
    started = loop.time()
    await controller._await_player_playout(queue, _item(20.0), "s1", taken_over, started)
    assert loop.time() - started < 2.0, "a takeover must abandon the wait"
    await task

    # and the cap bounds a mis-measurement rather than holding forever
    assert SINGLE_ITEM_LEAD_OUT_MAX_SECONDS <= 60


# -- the holdback decision --


def test_nothing_is_held_back_until_the_source_has_delivered_it_all() -> None:
    """
    The whole holdback decision: nothing before the source is done, the window after.

    Anything held while the source is still delivering has to come out of audio the
    player was waiting for, and is heard as a dropout at the boundary. Once the
    source is finished, what is left in hand arrived after it and the player is not
    waiting on any of it.
    """
    pcm_format = TEST_PCM_FORMAT
    frame_size = (pcm_format.bit_depth // 8) * pcm_format.channels
    window = 45 * pcm_format.pcm_sample_size

    def _item(buffer: object) -> Any:
        return cast(
            "Any",
            SimpleNamespace(
                streamdetails=SimpleNamespace(buffer=buffer, duration=300, seek_position=0)
            ),
        )

    # still delivering, however far ahead it has run: nothing may be held
    filling = SimpleNamespace(eof=False, has_error=False, duration_available=300.0)
    assert tail_hold_target(_item(filling), window, frame_size) == 0

    # delivered in full: the whole window, aligned to a frame
    done = SimpleNamespace(eof=True, has_error=False, duration_available=300.0)
    target = tail_hold_target(_item(done), window, frame_size)
    assert target == window
    assert target % frame_size == 0

    # a failed source is skipped without a fade, so its remainder plays out
    failed = SimpleNamespace(eof=True, has_error=True, duration_available=300.0)
    assert tail_hold_target(_item(failed), window, frame_size) == 0

    # no buffer yet: opening the stream is what creates it
    assert tail_hold_target(_item(None), window, frame_size) == 0
    assert tail_hold_target(cast("Any", SimpleNamespace(streamdetails=None)), window, 4) == 0

    # the buffer is read at decision time, so a capacity reselection that replaces
    # the item's details is picked up rather than remembered from before
    item = _item(filling)
    assert tail_hold_target(item, window, frame_size) == 0
    item.streamdetails = SimpleNamespace(buffer=done, duration=300, seek_position=0)
    assert tail_hold_target(item, window, frame_size) == window

    # a window narrower than the source has left is still the cap: the caller keeps
    # yielding above it, so only the last part of the item is retained
    narrow = 8 * pcm_format.pcm_sample_size
    assert tail_hold_target(_item(done), narrow, frame_size) == narrow


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


async def _run_smartfade_boundary(
    monkeypatch: pytest.MonkeyPatch,
    audio: StreamsAudio,
    pcm_format: AudioFormat,
) -> None:
    """Stream one item through a boundary with the mixer and next item stubbed out."""
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
    await _run_smartfade_boundary(monkeypatch, audio, pcm_format)

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


async def test_smartfade_a_source_still_delivering_hands_over_gapless(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A source that has not finished delivering has no tail to spare for a fade.

    Holding one back would take audio the player is waiting for, and the boundary
    is heard as a dropout rather than a blend. Gapless is the honest handover.
    """
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

    # every byte the source produced reaches the player, and no fade is planned
    assert len(output) >= pcm_format.pcm_sample_size * 16
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
