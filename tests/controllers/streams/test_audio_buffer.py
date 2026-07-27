"""Tests for the AudioBuffer class."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator
from contextlib import suppress
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.controllers.streams.audio_buffer import (
    AudioBuffer,
    AudioBufferDiscarded,
    AudioBufferEOF,
)
from music_assistant.controllers.streams.constants import (
    BUFFER_SIZE_MAP,
    RADIO_BUFFER_SIZE,
    SEEK_WAIT_THRESHOLD,
    BufferMode,
    BufferSize,
)

# Standard test PCM format: 44100Hz, 16-bit, stereo
TEST_PCM_FORMAT = AudioFormat(
    content_type=ContentType.PCM_S16LE,
    sample_rate=44100,
    bit_depth=16,
    channels=2,
)

# One second of silence in the test format
ONE_SECOND_CHUNK = b"\x00" * TEST_PCM_FORMAT.pcm_sample_size


def _make_chunk(value: int = 0) -> bytes:
    """Create a 1-second PCM chunk filled with a byte value."""
    return bytes([value % 256]) * TEST_PCM_FORMAT.pcm_sample_size


async def _make_source(num_chunks: int) -> AsyncGenerator[bytes]:
    """Create an async generator that yields numbered chunks."""
    for i in range(num_chunks):
        yield _make_chunk(i)


def _make_stream_details(
    media_type: MediaType,
    *,
    duration: int | None,
    allow_seek: bool,
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
        duration=duration,
        can_seek=allow_seek,
        allow_seek=allow_seek,
        queue_id=queue_id,
    )


def _make_mass_for_get_buffer(
    *, queue: Any | None = None
) -> tuple[MagicMock, AsyncMock, list[asyncio.Task[None]]]:
    """Build a minimal mass stub for AudioBuffer.get_buffer tests."""

    def _get_media_stream(*_args: Any, **_kwargs: Any) -> AsyncGenerator[bytes]:
        return _make_source(1)

    mass = MagicMock()
    mass.config.get_raw_core_config_value.return_value = BufferSize.BALANCED.value
    mass.player_queues.get.return_value = queue
    start_analysis = AsyncMock(return_value=None)
    mass.streams = SimpleNamespace(
        audio_analysis=SimpleNamespace(start_analysis=start_analysis),
        audio=SimpleNamespace(get_media_stream=_get_media_stream),
    )
    scheduled_tasks: list[asyncio.Task[None]] = []

    def _create_task(coro: Any) -> asyncio.Task[None]:
        task = asyncio.create_task(coro)
        scheduled_tasks.append(task)
        return task

    mass.create_task.side_effect = _create_task
    return mass, start_analysis, scheduled_tasks


# -- Init and properties --


def test_init_defaults() -> None:
    """AudioBuffer initializes with correct defaults."""
    buf = AudioBuffer(TEST_PCM_FORMAT)
    assert buf.pcm_format == TEST_PCM_FORMAT
    assert buf.mode == BufferMode.SEEKABLE
    assert buf.max_size_seconds == BUFFER_SIZE_MAP[BufferSize.BALANCED]
    assert buf.size_seconds == 0
    assert buf.seconds_available == 0
    assert not buf.cancelled
    assert not buf.has_error
    assert not buf.ready.is_set()


def test_init_minimal_buffer() -> None:
    """AudioBuffer with MINIMAL preset has correct max size."""
    buf = AudioBuffer(TEST_PCM_FORMAT, buffer_size=BufferSize.MINIMAL)
    assert buf.max_size_seconds == BUFFER_SIZE_MAP[BufferSize.MINIMAL]


def test_init_rolling_mode() -> None:
    """ROLLING mode uses radio buffer size regardless of preset."""
    buf = AudioBuffer(TEST_PCM_FORMAT, buffer_size=BufferSize.MAXIMUM, mode=BufferMode.ROLLING)
    assert buf.max_size_seconds == RADIO_BUFFER_SIZE


# -- Put and get --


@pytest.mark.asyncio
async def test_put_and_get() -> None:
    """Basic put/get cycle works correctly."""
    buf = AudioBuffer(TEST_PCM_FORMAT, buffer_size=BufferSize.MINIMAL)
    await buf._put(ONE_SECOND_CHUNK)
    result = await buf._get(chunk_number=0)
    assert result == ONE_SECOND_CHUNK


@pytest.mark.asyncio
async def test_put_sets_ready_default_threshold() -> None:
    """Ready event is set after 1 chunk with default threshold."""
    buf = AudioBuffer(TEST_PCM_FORMAT)
    assert not buf.ready.is_set()
    await buf._put(ONE_SECOND_CHUNK)
    assert buf.ready.is_set()


@pytest.mark.asyncio
async def test_put_sets_ready_custom_threshold() -> None:
    """Ready event is set after ready_threshold chunks are buffered."""
    buf = AudioBuffer(TEST_PCM_FORMAT, ready_threshold=3)
    assert not buf.ready.is_set()
    await buf._put(ONE_SECOND_CHUNK)
    assert not buf.ready.is_set()
    await buf._put(ONE_SECOND_CHUNK)
    assert not buf.ready.is_set()
    await buf._put(ONE_SECOND_CHUNK)
    assert buf.ready.is_set()


@pytest.mark.asyncio
async def test_eof_sets_ready_below_threshold() -> None:
    """EOF sets ready even when fewer than threshold chunks are buffered."""
    buf = AudioBuffer(TEST_PCM_FORMAT, ready_threshold=5)
    await buf._put(ONE_SECOND_CHUNK)
    assert not buf.ready.is_set()
    await buf._set_eof()
    assert buf.ready.is_set()


@pytest.mark.asyncio
async def test_get_waits_for_data() -> None:
    """Get waits until data is available."""
    buf = AudioBuffer(TEST_PCM_FORMAT)

    async def _delayed_put() -> None:
        await asyncio.sleep(0.05)
        await buf._put(ONE_SECOND_CHUNK)

    asyncio.get_event_loop().create_task(_delayed_put())
    result = await buf._get(chunk_number=0)
    assert result == ONE_SECOND_CHUNK


@pytest.mark.asyncio
async def test_get_raises_on_eof() -> None:
    """Get raises AudioBufferEOF when EOF is set and chunk not available."""
    buf = AudioBuffer(TEST_PCM_FORMAT)
    await buf._set_eof()
    with pytest.raises(AudioBufferEOF):
        await buf._get(chunk_number=0)


@pytest.mark.asyncio
async def test_get_after_cancel() -> None:
    """Get raises AudioBufferEOF when buffer is cleared."""
    buf = AudioBuffer(TEST_PCM_FORMAT)
    await buf._put(ONE_SECOND_CHUNK)
    await buf.clear()
    with pytest.raises(AudioBufferEOF):
        await buf._get(chunk_number=0)


# -- Fill and stream --


@pytest.mark.asyncio
async def test_fill_and_raw_stream() -> None:
    """Fill from async generator and iterate via get_raw_stream."""
    buf = AudioBuffer(TEST_PCM_FORMAT, buffer_size=BufferSize.MINIMAL)
    buf.fill(_make_source(5), source_name="test")

    # wait for fill to complete
    await asyncio.sleep(0.1)

    chunks = []
    async for chunk in buf.get_raw_stream():
        chunks.append(chunk)

    assert len(chunks) == 5
    # verify chunk content matches what we generated
    for i, chunk in enumerate(chunks):
        assert chunk == _make_chunk(i)


@pytest.mark.asyncio
async def test_fill_sets_eof() -> None:
    """Fill sets EOF when the source generator completes."""
    buf = AudioBuffer(TEST_PCM_FORMAT, buffer_size=BufferSize.MINIMAL)
    buf.fill(_make_source(3), source_name="test")
    await asyncio.sleep(0.1)
    assert buf._eof_received


@pytest.mark.asyncio
async def test_fill_error_propagation() -> None:
    """When the source errors after producing data, valid chunks are still delivered."""

    async def _failing_source() -> AsyncGenerator[bytes]:
        yield ONE_SECOND_CHUNK
        msg = "test error"
        raise RuntimeError(msg)

    buf = AudioBuffer(TEST_PCM_FORMAT, buffer_size=BufferSize.MINIMAL)
    buf.fill(_failing_source(), source_name="test")
    await asyncio.sleep(0.1)

    assert buf.has_error

    # consumer should receive the valid chunk and get a clean EOF
    result: list[bytes] = []
    async for chunk in buf.get_raw_stream():
        result.append(chunk)
    assert len(result) == 1
    assert result[0] == ONE_SECOND_CHUNK


@pytest.mark.asyncio
async def test_fill_error_surfaces_to_analysis_reader() -> None:
    """An aborted source raises its error to the analysis reader instead of a clean EOF."""

    async def _failing_source() -> AsyncGenerator[bytes]:
        yield ONE_SECOND_CHUNK
        msg = "test error"
        raise RuntimeError(msg)

    buf = AudioBuffer(TEST_PCM_FORMAT, buffer_size=BufferSize.MINIMAL)
    buf.fill(_failing_source(), source_name="test")

    # buffered chunks are still delivered before the error surfaces
    assert await buf.read_chunk_for_analysis(0) == ONE_SECOND_CHUNK
    with pytest.raises(RuntimeError, match="test error"):
        await buf.read_chunk_for_analysis(1)


@pytest.mark.asyncio
async def test_fill_error_no_data() -> None:
    """When the source errors without producing any data, the error propagates."""

    async def _failing_source() -> AsyncGenerator[bytes]:
        msg = "test error"
        raise RuntimeError(msg)
        yield  # type: ignore[unreachable]

    buf = AudioBuffer(TEST_PCM_FORMAT, buffer_size=BufferSize.MINIMAL)
    buf.fill(_failing_source(), source_name="test")
    await asyncio.sleep(0.1)

    assert buf.has_error

    async def _consume() -> list[bytes]:
        result = []
        async for chunk in buf.get_raw_stream():
            result.append(chunk)
        return result

    with pytest.raises(RuntimeError, match="test error"):
        await _consume()


@pytest.mark.asyncio
@pytest.mark.parametrize("media_type", [MediaType.SOUND_EFFECT, MediaType.AUDIO_SOURCE])
async def test_get_buffer_skips_analysis_for_non_analyzed_types(media_type: MediaType) -> None:
    """get_buffer skips audio analysis for sound effects and audio sources."""
    mass, start_analysis, scheduled_tasks = _make_mass_for_get_buffer()
    streamdetails = _make_stream_details(
        media_type,
        duration=30 if media_type == MediaType.SOUND_EFFECT else None,
        allow_seek=media_type == MediaType.SOUND_EFFECT,
    )

    buffer = await AudioBuffer.get_buffer(mass, streamdetails, reason="test")

    assert scheduled_tasks == []
    start_analysis.assert_not_called()
    await buffer.clear()


@pytest.mark.asyncio
async def test_get_buffer_still_starts_analysis_for_track() -> None:
    """get_buffer still schedules audio analysis for tracks."""
    mass, start_analysis, scheduled_tasks = _make_mass_for_get_buffer()
    streamdetails = _make_stream_details(MediaType.TRACK, duration=180, allow_seek=True)

    buffer = await AudioBuffer.get_buffer(mass, streamdetails, reason="test")

    assert len(scheduled_tasks) == 1
    await asyncio.gather(*scheduled_tasks)
    start_analysis.assert_awaited_once()
    await buffer.clear()


@pytest.mark.asyncio
async def test_get_buffer_sound_effect_uses_default_ready_threshold_without_crossfade() -> None:
    """Sound effects should not use the larger crossfade buffering threshold."""
    mass, start_analysis, scheduled_tasks = _make_mass_for_get_buffer(
        queue=SimpleNamespace(crossfade_enabled=True)
    )
    streamdetails = _make_stream_details(
        MediaType.SOUND_EFFECT,
        duration=30,
        allow_seek=True,
        queue_id="queue-1",
    )

    buffer = await AudioBuffer.get_buffer(mass, streamdetails, reason="test")

    assert buffer._ready_at_chunk == 2
    assert scheduled_tasks == []
    start_analysis.assert_not_called()
    await buffer.clear()


@pytest.mark.asyncio
async def test_fill_closes_source_on_cancel() -> None:
    """The source generator is finalized immediately when the fill task is cancelled."""
    source_closed = asyncio.Event()

    async def _endless_source() -> AsyncGenerator[bytes]:
        try:
            while True:
                yield ONE_SECOND_CHUNK
        finally:
            source_closed.set()

    buf = AudioBuffer(TEST_PCM_FORMAT, buffer_size=BufferSize.MINIMAL)
    buf.fill(_endless_source(), source_name="test")
    # let the fill task run until it blocks on the full buffer
    await asyncio.sleep(0.1)

    # clear() cancels the fill task, which must close the source generator
    await buf.clear()
    await asyncio.wait_for(source_closed.wait(), timeout=1)


# -- Seek and is_valid --


@pytest.mark.asyncio
async def test_is_valid_basic() -> None:
    """is_valid returns True for buffered positions."""
    buf = AudioBuffer(TEST_PCM_FORMAT, buffer_size=BufferSize.MINIMAL)
    for _ in range(10):
        await buf._put(ONE_SECOND_CHUNK)

    assert buf.is_valid(seek_position_ms=0)
    assert buf.is_valid(seek_position_ms=5000)
    assert buf.is_valid(seek_position_ms=9000)


@pytest.mark.asyncio
async def test_is_valid_cancelled() -> None:
    """is_valid returns False for cancelled buffer."""
    buf = AudioBuffer(TEST_PCM_FORMAT)
    await buf._put(ONE_SECOND_CHUNK)
    await buf.clear()
    assert not buf.is_valid()


@pytest.mark.asyncio
async def test_is_valid_seek_ahead_within_threshold() -> None:
    """is_valid returns True when seek is within SEEK_WAIT_THRESHOLD of buffered data."""
    buf = AudioBuffer(TEST_PCM_FORMAT, buffer_size=BufferSize.MINIMAL)
    for _ in range(10):
        await buf._put(ONE_SECOND_CHUNK)

    # 10 chunks buffered, seek to 10+SEEK_WAIT_THRESHOLD seconds should be valid
    seek_ms = (10 + SEEK_WAIT_THRESHOLD) * 1000
    assert buf.is_valid(seek_position_ms=seek_ms)


@pytest.mark.asyncio
async def test_is_valid_seek_ahead_beyond_threshold() -> None:
    """is_valid returns False when seek is beyond SEEK_WAIT_THRESHOLD."""
    buf = AudioBuffer(TEST_PCM_FORMAT, buffer_size=BufferSize.MINIMAL)
    for _ in range(10):
        await buf._put(ONE_SECOND_CHUNK)

    seek_ms = (10 + SEEK_WAIT_THRESHOLD + 1) * 1000
    assert not buf.is_valid(seek_position_ms=seek_ms)


@pytest.mark.asyncio
async def test_is_valid_with_eof() -> None:
    """is_valid returns True for any position when EOF is received."""
    buf = AudioBuffer(TEST_PCM_FORMAT, buffer_size=BufferSize.MINIMAL)
    for _ in range(5):
        await buf._put(ONE_SECOND_CHUNK)
    await buf._set_eof()

    # even beyond buffered data, is_valid returns True with EOF
    assert buf.is_valid(seek_position_ms=100_000)


@pytest.mark.asyncio
async def test_seek_in_raw_stream() -> None:
    """get_raw_stream with seek_position_ms skips to correct chunk."""
    buf = AudioBuffer(TEST_PCM_FORMAT, buffer_size=BufferSize.MINIMAL)
    buf.fill(_make_source(10), source_name="test")
    await asyncio.sleep(0.1)

    chunks = []
    async for chunk in buf.get_raw_stream(seek_position_ms=5000):
        chunks.append(chunk)

    assert len(chunks) == 5
    # first chunk should be chunk #5
    assert chunks[0] == _make_chunk(5)


# -- Analysis reader (read_chunk_for_analysis) --


@pytest.mark.asyncio
async def test_read_chunk_for_analysis_returns_buffered_chunk() -> None:
    """A passive reader gets a retained chunk without discarding it."""
    buf = AudioBuffer(TEST_PCM_FORMAT, buffer_size=BufferSize.MINIMAL)
    await buf._put(_make_chunk(0))
    await buf._put(_make_chunk(1))

    assert await buf.read_chunk_for_analysis(0) == _make_chunk(0)
    assert await buf.read_chunk_for_analysis(1) == _make_chunk(1)
    # Reading must not have discarded anything — both chunks are still buffered.
    assert buf.seconds_available == 2
    assert buf.first_buffered_chunk == 0


@pytest.mark.asyncio
async def test_read_chunk_for_analysis_waits_then_returns() -> None:
    """A reader ahead of the filled position waits until the chunk is produced."""
    buf = AudioBuffer(TEST_PCM_FORMAT, buffer_size=BufferSize.MINIMAL)
    reader = asyncio.ensure_future(buf.read_chunk_for_analysis(0))
    await asyncio.sleep(0.05)
    assert not reader.done()  # nothing buffered yet

    await buf._put(_make_chunk(0))
    assert await asyncio.wait_for(reader, timeout=1.0) == _make_chunk(0)


@pytest.mark.asyncio
async def test_read_chunk_for_analysis_raises_eof_past_end() -> None:
    """Reading past the last chunk of an ended stream raises AudioBufferEOF."""
    buf = AudioBuffer(TEST_PCM_FORMAT, buffer_size=BufferSize.MINIMAL)
    await buf._put(_make_chunk(0))
    await buf._set_eof()

    assert await buf.read_chunk_for_analysis(0) == _make_chunk(0)
    with pytest.raises(AudioBufferEOF):
        await buf.read_chunk_for_analysis(1)


@pytest.mark.asyncio
async def test_read_chunk_for_analysis_raises_discarded_when_evicted() -> None:
    """Requesting a chunk that has been evicted from the window raises AudioBufferDiscarded."""
    buf = AudioBuffer(TEST_PCM_FORMAT, buffer_size=BufferSize.MINIMAL)
    await buf._put(_make_chunk(0))
    # Simulate the playback consumer sliding the window past chunk 0.
    buf._chunks.popleft()
    buf._discarded_chunks += 1
    assert buf.first_buffered_chunk == 1

    with pytest.raises(AudioBufferDiscarded):
        await buf.read_chunk_for_analysis(0)


@pytest.mark.asyncio
async def test_read_chunk_for_analysis_raises_discarded_on_clear() -> None:
    """A reader blocked on a torn-down buffer is released with AudioBufferDiscarded."""
    buf = AudioBuffer(TEST_PCM_FORMAT, buffer_size=BufferSize.MINIMAL)
    reader = asyncio.ensure_future(buf.read_chunk_for_analysis(0))
    await asyncio.sleep(0.05)
    await buf.clear()
    with pytest.raises(AudioBufferDiscarded):
        await asyncio.wait_for(reader, timeout=1.0)


# -- Buffer size limits --


@pytest.mark.asyncio
async def test_rolling_buffer_fifo() -> None:
    """ROLLING mode works as a FIFO — get pops the oldest chunk."""
    buf = AudioBuffer(TEST_PCM_FORMAT, mode=BufferMode.ROLLING)

    for i in range(5):
        await buf._put(_make_chunk(i))

    assert buf.size_seconds == 5

    # get pops the oldest chunk and frees space
    result = await buf._get(chunk_number=0)
    assert result == _make_chunk(0)
    assert buf.size_seconds == 4
    assert buf._discarded_chunks == 1

    # next get returns the next chunk
    result = await buf._get(chunk_number=1)
    assert result == _make_chunk(1)
    assert buf.size_seconds == 3
    assert buf._discarded_chunks == 2


@pytest.mark.asyncio
async def test_seekable_buffer_backpressure() -> None:
    """SEEKABLE mode waits on put when full, consumer frees space on get."""
    buf = AudioBuffer(TEST_PCM_FORMAT, buffer_size=BufferSize.MINIMAL)
    max_size = buf.max_size_seconds

    # use fill() so there's an active producer task (eviction only happens
    # when the producer is running and needs space)
    buf.fill(_make_source(max_size + 5), source_name="test")
    await asyncio.sleep(0.1)

    assert buf.size_seconds == max_size

    # reading from a full buffer frees space for the producer
    chunk = await buf._get(chunk_number=0)
    assert chunk == _make_chunk(0)
    assert buf._discarded_chunks == 1


@pytest.mark.asyncio
async def test_seekable_no_eviction_after_eof() -> None:
    """After EOF, reads from a full buffer do not evict chunks."""
    buf = AudioBuffer(TEST_PCM_FORMAT, buffer_size=BufferSize.MINIMAL)
    max_size = buf.max_size_seconds

    buf.fill(_make_source(max_size), source_name="test")
    await asyncio.sleep(0.1)

    assert buf._eof_received
    assert buf.size_seconds == max_size

    # read should NOT evict since producer is done
    chunk = await buf._get(chunk_number=0)
    assert chunk == _make_chunk(0)
    assert buf._discarded_chunks == 0
    assert buf.size_seconds == max_size


# -- get_stream passthrough --


@pytest.mark.asyncio
async def test_get_stream_no_filters() -> None:
    """get_stream without filters passes through raw data."""
    buf = AudioBuffer(TEST_PCM_FORMAT, buffer_size=BufferSize.MINIMAL)
    buf.fill(_make_source(3), source_name="test")
    await asyncio.sleep(0.1)

    chunks = []
    async for chunk in buf.get_stream(output_format=TEST_PCM_FORMAT):
        chunks.append(chunk)

    assert len(chunks) == 3
    assert chunks[0] == _make_chunk(0)


# -- Rolling mode --


@pytest.mark.asyncio
async def test_rolling_mode_max_size() -> None:
    """ROLLING mode uses RADIO_BUFFER_SIZE."""
    buf = AudioBuffer(TEST_PCM_FORMAT, mode=BufferMode.ROLLING)
    assert buf.max_size_seconds == RADIO_BUFFER_SIZE


# -- Ready threshold with seek offset --


@pytest.mark.asyncio
async def test_ready_accounts_for_seek_offset() -> None:
    """Ready fires only after enough data past the seek point is buffered."""
    buf = AudioBuffer(TEST_PCM_FORMAT, ready_threshold=3)
    # simulate get_buffer setting the offset for a seek to 100s
    buf._discarded_chunks = 100
    buf._ready_at_chunk = 100 + 3  # seek_chunk + threshold

    await buf._put(ONE_SECOND_CHUNK)  # chunk 100
    assert not buf.ready.is_set()
    await buf._put(ONE_SECOND_CHUNK)  # chunk 101
    assert not buf.ready.is_set()
    await buf._put(ONE_SECOND_CHUNK)  # chunk 102
    assert buf.ready.is_set()


@pytest.mark.asyncio
async def test_chunk_numbering_with_seek_offset() -> None:
    """Chunks are numbered correctly when buffer starts at a seek offset."""
    buf = AudioBuffer(TEST_PCM_FORMAT, buffer_size=BufferSize.MINIMAL)
    # simulate a buffer created for a seek to 300s
    buf._discarded_chunks = 300

    for i in range(5):
        await buf._put(_make_chunk(i))

    # chunk 300 should be the first chunk (value 0)
    result = await buf._get(chunk_number=300)
    assert result == _make_chunk(0)
    # chunk 304 should be the fifth chunk (value 4)
    result = await buf._get(chunk_number=304)
    assert result == _make_chunk(4)


@pytest.mark.asyncio
async def test_is_valid_with_seek_offset() -> None:
    """is_valid works correctly with a seek offset."""
    buf = AudioBuffer(TEST_PCM_FORMAT, buffer_size=BufferSize.MINIMAL)
    buf._discarded_chunks = 300

    for _ in range(10):
        await buf._put(ONE_SECOND_CHUNK)

    # positions before the offset are invalid (discarded)
    assert not buf.is_valid(seek_position_ms=299_000)
    # positions within the buffer are valid
    assert buf.is_valid(seek_position_ms=300_000)
    assert buf.is_valid(seek_position_ms=305_000)


@pytest.mark.asyncio
async def test_raw_stream_with_seek_offset() -> None:
    """get_raw_stream works correctly when buffer has a seek offset."""
    buf = AudioBuffer(TEST_PCM_FORMAT, buffer_size=BufferSize.MINIMAL)
    buf._discarded_chunks = 300

    for i in range(5):
        await buf._put(_make_chunk(i))
    await buf._set_eof()

    chunks = []
    async for chunk in buf.get_raw_stream(seek_position_ms=300_000):
        chunks.append(chunk)

    assert len(chunks) == 5
    assert chunks[0] == _make_chunk(0)
    assert chunks[4] == _make_chunk(4)


# -- Callback error isolation --


@pytest.mark.asyncio
async def test_clear_fires_cancel_callbacks() -> None:
    """clear() fires registered cancel callbacks before removing them."""
    cancel_called = False

    def _cancel_callback() -> None:
        nonlocal cancel_called
        cancel_called = True

    buf = AudioBuffer(TEST_PCM_FORMAT, buffer_size=BufferSize.MINIMAL)
    buf.register_cancel_callback(_cancel_callback)
    await buf._put(ONE_SECOND_CHUNK)

    await buf.clear()
    assert cancel_called is True
    assert len(buf._cancel_callbacks) == 0


# -- Inactivity monitor --


@pytest.mark.asyncio
async def test_inactivity_monitor_releases_drained_buffer() -> None:
    """
    A buffer that has drained to empty is still released by the inactivity monitor.

    Regression test: the monitor previously only cleared when chunks remained, so an
    abandoned rolling buffer that drained to zero chunks looped forever and leaked it
    (and its producer/ffmpeg) until the process exited.
    """
    buf = AudioBuffer(TEST_PCM_FORMAT, mode=BufferMode.ROLLING)
    # no chunks buffered and last access long ago -> the buffer is inactive
    assert buf.size_seconds == 0
    buf._last_access_time = time.time() - 10_000

    await buf._monitor_inactivity(inactivity_timeout=0.01, check_interval=0.01)

    assert buf.cancelled is True


@pytest.mark.asyncio
async def test_inactivity_monitor_keeps_active_buffer() -> None:
    """A buffer that is still being accessed is not cleared by the inactivity monitor."""
    buf = AudioBuffer(TEST_PCM_FORMAT, mode=BufferMode.ROLLING)
    buf._last_access_time = time.time()

    monitor = asyncio.create_task(
        buf._monitor_inactivity(inactivity_timeout=5, check_interval=0.01)
    )
    await asyncio.sleep(0.05)

    assert not monitor.done()
    assert buf.cancelled is False

    monitor.cancel()
    with suppress(asyncio.CancelledError):
        await monitor
