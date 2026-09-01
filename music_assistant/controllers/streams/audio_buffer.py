"""
Audio buffer implementation for PCM audio streaming.

AudioBuffer is the primary interface for all buffered audio streaming in Music Assistant.
It stores raw decoded PCM audio (no filters applied) and provides methods to:
- Fill the buffer from any async generator of audio chunks
- Get raw or processed (filtered/resampled) audio streams
- Seek within buffered audio
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import AsyncGenerator, Callable
from contextlib import aclosing, suppress
from typing import TYPE_CHECKING, Any, Final

from music_assistant_models.enums import (
    ContentType,
    MediaType,
    VolumeNormalizationMode,
)
from music_assistant_models.errors import AudioError
from music_assistant_models.media_items import AudioFormat

from music_assistant.constants import MASS_LOGGER_NAME, VERBOSE_LOG_LEVEL
from music_assistant.controllers.streams.constants import (
    BUFFER_SIZE_MAP,
    CONF_BUFFER_SIZE,
    CONF_BUFFER_SIZE_DEFAULT,
    RADIO_BUFFER_SIZE,
    SEEK_WAIT_THRESHOLD,
    STREAM_SLOT_WAIT_TIMEOUT,
    BufferMode,
    BufferSize,
)
from music_assistant.helpers.audio import arriving_audio_format
from music_assistant.helpers.ffmpeg import get_ffmpeg_stream
from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from music_assistant_models.streamdetails import StreamDetails

    from music_assistant.mass import MusicAssistant

LOGGER = logging.getLogger(f"{MASS_LOGGER_NAME}.audio_buffer")

# Callback signature for cancel observers: invoked when the buffer is cancelled/cleared.
CancelCallback = Callable[[], None]

# Maximum seconds to wait for the first playable audio, on top of any time the producer
# is allowed to spend waiting for a provider source-stream slot.
BUFFER_READY_TIMEOUT: Final[int] = 15


class AudioBufferEOF(Exception):
    """Exception raised when the audio buffer reaches end-of-file."""


class AudioBufferDiscarded(Exception):
    """
    Raised when a passive (analysis) reader requests a chunk evicted from the retained window.

    Means the reader is a full window behind playback, so its session is dropped.
    """


class AudioBuffer:
    """
    Raw PCM audio buffer with seek support and optional filter processing.

    Stores audio in the original sample rate and bit depth.
    Use get_raw_stream() for unprocessed PCM and get_stream() for
    filtered/resampled output.
    """

    def __init__(
        self,
        pcm_format: AudioFormat,
        buffer_size: BufferSize = BufferSize.BALANCED,
        mode: BufferMode = BufferMode.SEEKABLE,
        ready_threshold: int = 1,
        is_realtime: bool = False,
    ) -> None:
        """
        Initialize AudioBuffer.

        :param pcm_format: The PCM audio format specification.
        :param buffer_size: Buffer size preset.
        :param mode: Buffer mode (SEEKABLE for tracks, ROLLING for radio).
        :param ready_threshold: Seconds of audio to buffer before signaling ready.
        :param is_realtime: Whether the source hands its audio over at playback pace.
        """
        self.pcm_format = pcm_format
        self.is_realtime = is_realtime
        self.max_size_seconds = (
            RADIO_BUFFER_SIZE if mode == BufferMode.ROLLING else BUFFER_SIZE_MAP[buffer_size]
        )
        self.mode = mode
        self._ready_threshold = ready_threshold
        self._ready_at_chunk = ready_threshold  # updated by get_buffer to account for seek
        self._chunks: deque[bytes] = deque()
        self._discarded_chunks = 0
        # furthest chunk playback has taken, as an absolute chunk number; passive
        # (analysis) reads deliberately leave it alone
        self._served_chunks = 0
        self._lock = asyncio.Lock()
        self._data_available = asyncio.Condition(self._lock)
        self._space_available = asyncio.Condition(self._lock)
        self._eof_received = False
        self._producer_task: asyncio.Task[None] | None = None
        self._fill_started = time.monotonic()
        self._source_name = "unknown"
        self._last_access_time: float = time.time()
        self._inactivity_task: asyncio.Task[None] | None = None
        self._cancelled = False
        self._producer_error: Exception | None = None
        self._background_tasks: set[asyncio.Task[None]] = set()
        self.ready = asyncio.Event()
        self._cancel_callbacks: list[CancelCallback] = []
        self._ready_wait_lock = asyncio.Lock()

    # -- Properties --

    @property
    def cancelled(self) -> bool:
        """Return whether the buffer has been cancelled or cleared."""
        if self._cancelled:
            return True
        return self._producer_task is not None and self._producer_task.cancelled()

    @property
    def has_error(self) -> bool:
        """Return whether the producer encountered an error."""
        return self._producer_error is not None

    @property
    def chunk_size_bytes(self) -> int:
        """Return the size in bytes of one second of PCM audio."""
        return self.pcm_format.pcm_sample_size

    @property
    def size_seconds(self) -> int:
        """Return current size of the buffer in seconds."""
        return len(self._chunks)

    @property
    def seconds_available(self) -> int:
        """Return number of seconds of audio currently available."""
        return len(self._chunks)

    @property
    def duration_available(self) -> float:
        """Return the exact duration of resident PCM audio in seconds."""
        return sum(len(chunk) for chunk in self._chunks) / self.pcm_format.pcm_sample_size

    @property
    def undrained_seconds(self) -> float:
        """
        Return the seconds of buffered audio past the furthest position playback has read.

        What a source that produces faster than playback is held back on.
        """
        produced = self._discarded_chunks + len(self._chunks)
        return max(0.0, float(produced - self._served_chunks))

    @property
    def is_buffering(self) -> bool:
        """Return whether the upstream source producer is still active."""
        return self._producer_task is not None and not self._producer_task.done()

    @property
    def eof(self) -> bool:
        """
        Return whether the source stopped producing.

        A source that failed after delivering audio also ends here, so pair this with
        ``has_error`` when a clean finish is what matters.
        """
        return self._eof_received

    @property
    def first_buffered_chunk(self) -> int:
        """Return the chunk number of the oldest chunk still retained in the buffer."""
        return self._discarded_chunks

    # -- Public methods --

    def register_cancel_callback(self, callback: CancelCallback) -> None:
        """
        Register a callback to be invoked when the buffer is cancelled or cleared.

        :param callback: Callable with no arguments, invoked on cancel.
        """
        self._cancel_callbacks.append(callback)

    def is_valid(self, seek_position_ms: int = 0) -> bool:
        """
        Check if the buffer can serve the given seek position.

        :param seek_position_ms: The position to seek to in milliseconds.
        """
        if self.cancelled:
            return False

        # reset inactivity timer — checking validity is activity
        self._last_access_time = time.time()

        seek_chunk = seek_position_ms // 1000

        if seek_chunk < self._discarded_chunks:
            return False

        total_chunks = self._discarded_chunks + len(self._chunks)
        if seek_chunk < total_chunks or self._eof_received:
            return True

        # The position is ahead of what the producer has made. One that runs
        # faster than playback covers that in a fraction of the time, so waiting
        # beats starting a new producer - but one that hands its audio over at
        # playback pace needs exactly as long as the gap, while a fresh producer
        # starts at the position right away (see get_buffer below).
        chunks_ahead = seek_chunk - total_chunks
        return chunks_ahead <= (0 if self.is_realtime else SEEK_WAIT_THRESHOLD)

    async def get_raw_stream(
        self, seek_position_ms: int = 0, exact_seek: bool = False
    ) -> AsyncGenerator[bytes]:
        """
        Get raw (unprocessed) PCM audio from the buffer.

        :param seek_position_ms: Starting position in milliseconds.
        :param exact_seek: Preserve millisecond precision instead of quantizing to 100 ms.
        """
        if not exact_seek:
            # align regular user seeks to 100ms steps to avoid rounding issues
            seek_position_ms = (seek_position_ms // 100) * 100
        chunk_number = seek_position_ms // 1000
        # handle fractional seek: trim leading samples from the first chunk
        fractional_ms = seek_position_ms % 1000
        trim_bytes = 0
        if fractional_ms > 0:
            samples_to_trim = self.pcm_format.sample_rate * fractional_ms // 1000
            bytes_per_sample = (self.pcm_format.bit_depth // 8) * self.pcm_format.channels
            trim_bytes = samples_to_trim * bytes_per_sample

        while True:
            try:
                self._last_access_time = time.time()
                chunk = await self._get(chunk_number=chunk_number)
                if trim_bytes > 0:
                    chunk = chunk[trim_bytes:]
                    trim_bytes = 0
                yield chunk
                chunk_number += 1
            except AudioBufferEOF:
                break

    async def read_chunk_for_analysis(self, chunk_number: int) -> bytes:
        """
        Return one PCM chunk for a passive (analysis) reader, waiting until it is available.

        A read-only accessor: it leaves the buffer untouched — no discard, no producer-space
        signalling, no inactivity-timer reset — so an analysis reader never affects playback's
        buffering.

        :param chunk_number: Absolute chunk index to read.
        :raises AudioBufferEOF: the stream ended before this chunk.
        :raises AudioBufferDiscarded: the chunk has been evicted from the retained window (the
            reader is a full window behind playback) or the buffer was torn down.
        """
        async with self._data_available:
            while True:
                if self.cancelled:
                    raise AudioBufferDiscarded
                if chunk_number < self._discarded_chunks:
                    raise AudioBufferDiscarded
                index = chunk_number - self._discarded_chunks
                if index < len(self._chunks):
                    return self._chunks[index]
                if self._producer_error:
                    raise self._producer_error
                if self._eof_received:
                    raise AudioBufferEOF
                await self._data_available.wait()

    async def get_stream(
        self,
        output_format: AudioFormat,
        seek_position_ms: int = 0,
        filter_params: list[str] | None = None,
        exact_seek: bool = False,
    ) -> AsyncGenerator[bytes]:
        """
        Get processed audio from the buffer.

        Returns audio in the requested output format with optional filters applied.
        If no processing is needed, yields directly from the buffer.

        :param output_format: The desired output PCM format.
        :param seek_position_ms: Starting position in milliseconds.
        :param filter_params: FFmpeg filter parameters to apply.
        :param exact_seek: Preserve millisecond precision for the input buffer position.
        """
        needs_ffmpeg = bool(filter_params) or self.pcm_format != output_format

        if not needs_ffmpeg:
            async for chunk in self.get_raw_stream(
                seek_position_ms=seek_position_ms, exact_seek=exact_seek
            ):
                yield chunk
            return

        async for chunk in get_ffmpeg_stream(
            audio_input=self.get_raw_stream(
                seek_position_ms=seek_position_ms, exact_seek=exact_seek
            ),
            input_format=self.pcm_format,
            output_format=output_format,
            filter_params=filter_params,
        ):
            yield chunk

    def fill(self, audio_source: AsyncGenerator[bytes], source_name: str = "unknown") -> None:
        """
        Start filling the buffer from an async generator of PCM audio chunks.

        :param audio_source: Async generator yielding 1-second PCM audio chunks.
        :param source_name: Name for logging purposes.
        """
        self._fill_started = time.monotonic()
        self._source_name = source_name

        async def _fill_task() -> None:
            chunk_count = 0
            status = "running"
            try:
                # aclosing guarantees the source generator (and any ffmpeg chain
                # behind it) is finalized immediately when this task is cancelled,
                # instead of lingering until garbage collection.
                async with aclosing(audio_source):
                    async for chunk in audio_source:
                        chunk_count += 1
                        await self._put(chunk)
                        await asyncio.sleep(0)
                await self._set_eof()
            except asyncio.CancelledError:
                status = "cancelled"
                raise
            except Exception as err:
                status = "aborted with error"
                # record the error before the EOF signal below, so readers that
                # check for a producer error never observe the abort as a clean EOF
                self._producer_error = err
                raise
            finally:
                # signal EOF even on error if we produced valid chunks,
                # so the consumer can read all buffered data before seeing the error
                if status == "aborted with error" and chunk_count > 0:
                    await self._set_eof()
                LOGGER.log(
                    VERBOSE_LOG_LEVEL,
                    "fill: %s (%s chunks) for %s",
                    status,
                    chunk_count,
                    source_name,
                )

        loop = asyncio.get_running_loop()
        task = loop.create_task(_fill_task())
        self._attach_producer_task(task)

    async def clear(self, cancel_inactivity_task: bool = True) -> None:
        """Reset the buffer, clearing all data and cancelling active tasks."""
        chunk_count = len(self._chunks)
        LOGGER.log(
            VERBOSE_LOG_LEVEL,
            "AudioBuffer.clear: Resetting buffer (had %s chunks, producer: %s)",
            chunk_count,
            self._producer_task is not None,
        )
        if self._producer_task and not self._producer_task.done():
            self._producer_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._producer_task

        if cancel_inactivity_task and self._inactivity_task and not self._inactivity_task.done():
            self._inactivity_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._inactivity_task

        # signal cancel callbacks only if the stream did not complete normally
        if not self._eof_received:
            for callback in list(self._cancel_callbacks):
                try:
                    callback()
                except Exception:
                    LOGGER.exception("Cancel callback failed during clear")

        async with self._lock:
            self._chunks = deque()
            self._discarded_chunks = 0
            self._eof_received = False
            self._cancelled = True
            self._producer_error = None
            self.ready.clear()
            self._cancel_callbacks.clear()
            self._data_available.notify_all()
            self._space_available.notify_all()

    @staticmethod
    async def get_buffer(
        mass: MusicAssistant,
        streamdetails: StreamDetails,
        seek_position_ms: int = 0,
        wait_ready: bool = False,
        reason: str = "",
        source_wait_timeout: float | None = STREAM_SLOT_WAIT_TIMEOUT,
    ) -> AudioBuffer:
        """
        Get or create an AudioBuffer for the given streamdetails.

        Reuses an existing valid buffer if available.
        Buffer size is determined from the streams controller configuration.

        :param mass: The MusicAssistant instance.
        :param streamdetails: The stream details for the media.
        :param seek_position_ms: Position in milliseconds to start from.
        :param wait_ready: If True, wait for the first chunk before returning.
        :param reason: Caller context for logging (e.g. 'prepare', 'streaming').
        :param source_wait_timeout: Maximum seconds the producer may wait for a free
            source-stream slot on the providing music provider, or None to wait
            without a timeout.
        :raises AudioError: If the buffer does not become ready, wrapping the typed
            producer error (e.g. ProviderStreamLimitError) when there is one.
        """
        log_prefix = f"get_buffer[{reason}]" if reason else "get_buffer"
        # the producer may spend its source wait before the first byte arrives,
        # so the readiness budget covers that wait on top of the audio itself
        ready_timeout = BUFFER_READY_TIMEOUT + (source_wait_timeout or 0)

        # reuse existing valid buffer
        existing_buffer: AudioBuffer | None = streamdetails.buffer
        if existing_buffer is not None:
            if existing_buffer.has_error or not existing_buffer.is_valid(seek_position_ms):
                LOGGER.debug(
                    "%s: Existing buffer invalid for %s (seek_ms: %s, discarded: %s)",
                    log_prefix,
                    streamdetails.uri,
                    seek_position_ms,
                    existing_buffer._discarded_chunks,
                )
                streamdetails.buffer = None
                # a still-filling producer holds one of the provider's source-stream slots.
                # The replacement needs a slot, so take this one back only when the provider
                # has none free - otherwise a superseded consumer keeps draining its audio.
                provider = mass.get_provider(streamdetails.provider, return_unavailable=True)
                must_release_slot = (
                    existing_buffer.is_buffering
                    and isinstance(provider, MusicProvider)
                    and provider.max_concurrent_streams is not None
                    and not provider.has_available_stream_slot
                )
                if must_release_slot or time.time() - existing_buffer._last_access_time > 30:
                    await asyncio.shield(existing_buffer.clear())
                # else: an active consumer is still reading via its local reference;
                # the inactivity monitor will clean up after it finishes
            else:
                LOGGER.debug(
                    "%s: Reusing buffer for %s - available: %ss, seek_ms: %s, discarded: %s",
                    log_prefix,
                    streamdetails.uri,
                    existing_buffer.seconds_available,
                    seek_position_ms,
                    existing_buffer._discarded_chunks,
                )
                if wait_ready:
                    await existing_buffer._wait_until_ready(
                        streamdetails, ready_timeout, log_prefix
                    )
                return existing_buffer

        audio_buffer, buffer_seek_seconds = _new_buffer(
            mass, streamdetails, seek_position_ms, log_prefix
        )

        # start filling from the media stream (seek in seconds for FFmpeg)
        audio_source = mass.streams.audio.get_media_stream(
            streamdetails,
            audio_buffer.pcm_format,
            seek_position=buffer_seek_seconds,
            filter_params=None,
            source_wait_timeout=source_wait_timeout,
        )
        audio_buffer.fill(audio_source, source_name=streamdetails.uri)

        if wait_ready:
            await audio_buffer._wait_until_ready(streamdetails, ready_timeout, log_prefix)

        return audio_buffer

    @staticmethod
    def open_provider_fill(
        mass: MusicAssistant, streamdetails: StreamDetails, reason: str = ""
    ) -> ProviderAudioFill:
        """
        Create the buffer for an item whose provider writes its PCM in, and return the handle.

        For a source that produces an item's audio on its own schedule rather than on
        request — the audio exists only while the source is on that item — so its buffer
        has to be there to receive it. The buffer is attached to the given stream details,
        where a later playback request finds and reuses it.

        :param mass: The MusicAssistant instance.
        :param streamdetails: Stream details of the item the audio belongs to.
        :param reason: Caller context for logging.
        """
        log_prefix = f"open_provider_fill[{reason}]" if reason else "open_provider_fill"
        audio_buffer, _ = _new_buffer(mass, streamdetails, 0, log_prefix)
        fill = ProviderAudioFill(audio_buffer.pcm_format, streamdetails, buffer=audio_buffer)
        audio_buffer.fill(
            # the buffer counts one chunk as one second, so the provider's own write
            # sizes are gathered into that before they reach it
            _in_chunks_of(fill.stream(), audio_buffer.chunk_size_bytes),
            source_name=streamdetails.uri,
        )
        return fill

    # -- Private methods --

    async def _wait_until_ready(
        self, streamdetails: StreamDetails, ready_timeout: float, log_prefix: str
    ) -> None:
        """
        Wait until this buffer can serve playback or raise its producer failure.

        :param streamdetails: Stream details currently referencing this buffer.
        :param ready_timeout: Maximum seconds to wait for enough buffered audio.
        :param log_prefix: Caller context for logging.
        """
        async with self._ready_wait_lock:
            if not self.ready.is_set():
                try:
                    await asyncio.wait_for(self.ready.wait(), timeout=ready_timeout)
                except TimeoutError as err:
                    # clear() does not wake this wait, and only marks the buffer cancelled
                    # once the producer is gone - so a buffer released elsewhere (to free a
                    # stream slot) lands here on an abort of our own making
                    producer = self._producer_task
                    releasing = self.cancelled or bool(producer and producer.cancelling())
                    if not releasing:
                        LOGGER.warning(
                            "%s: Gave up on %s (%s) after %.2fs, %ss buffered",
                            log_prefix,
                            streamdetails.provider,
                            streamdetails.uri,
                            time.monotonic() - self._fill_started,
                            self.seconds_available,
                        )
                    producer_error = await self._clear_failed_buffer(streamdetails)
                    if isinstance(producer_error, AudioError):
                        raise producer_error from err
                    raise AudioError("Timeout waiting for audio data") from (producer_error or err)
            # ready was signaled but check if it was due to a producer error
            # (ready is also set by _notify_on_producer_error)
            if not self.has_error:
                return
            producer_error = await self._clear_failed_buffer(streamdetails)
            # surface a typed producer failure (e.g. a source capacity limit) as-is,
            # so callers can act on it instead of on a generic wrapper
            if isinstance(producer_error, AudioError):
                raise producer_error
            raise AudioError("Failed to stream audio") from producer_error

    async def _clear_failed_buffer(self, streamdetails: StreamDetails) -> Exception | None:
        """
        Detach and clear this buffer after preparation failed.

        :param streamdetails: Stream details currently referencing this buffer.
        :return: The producer error recorded before the buffer was cleared.
        """
        producer_error = self._producer_error
        if streamdetails.buffer is self:
            streamdetails.buffer = None
        await asyncio.shield(self.clear())
        return producer_error

    async def _put(self, chunk: bytes) -> None:
        """
        Put a 1-second chunk of PCM audio into the buffer.

        Waits for space when the buffer is full (backpressure).
        """
        async with self._lock:
            if self._cancelled:
                return

            if self._eof_received:
                LOGGER.log(
                    VERBOSE_LOG_LEVEL, "AudioBuffer._put: EOF already received, rejecting chunk"
                )
                return

            # wait for the consumer to free space when buffer is full
            await self._wait_for_space()

            chunk_position = self._discarded_chunks + len(self._chunks)
            self._chunks.append(chunk)
            if LOGGER.isEnabledFor(VERBOSE_LOG_LEVEL):
                LOGGER.log(
                    VERBOSE_LOG_LEVEL,
                    "AudioBuffer._put: Added chunk at position %s (size: %s bytes, buffer: %s)",
                    chunk_position,
                    len(chunk),
                    len(self._chunks),
                )

            if not self.ready.is_set() and (
                self._discarded_chunks + len(self._chunks) >= self._ready_at_chunk
                or len(self._chunks) >= self.max_size_seconds
            ):
                self._mark_ready()

            self._data_available.notify_all()

    async def _set_eof(self) -> None:
        """Signal that no more data will be added to the buffer."""
        async with self._lock:
            LOGGER.log(
                VERBOSE_LOG_LEVEL,
                "AudioBuffer._set_eof: Marking EOF (buffer has %s chunks)",
                len(self._chunks),
            )
            self._eof_received = True
            if not self.ready.is_set():
                self._mark_ready()
            self._data_available.notify_all()
            self._space_available.notify_all()

    def _mark_ready(self) -> None:
        """Signal that the buffer holds audio a consumer can start playing."""
        self.ready.set()
        # a source that failed or ended empty also lands here, without the
        # buffer ever having become playable
        if self._chunks and not self.has_error:
            LOGGER.debug(
                "AudioBuffer: %s became ready after %.2fs",
                self._source_name,
                time.monotonic() - self._fill_started,
            )

    async def _get(self, chunk_number: int = 0) -> bytes:
        """
        Get one second of audio at the given chunk position.

        Waits until the chunk is available. Discards old chunks when full.

        :raises AudioBufferEOF: If EOF is reached or the buffer was cleared.
        :raises AudioError: If the chunk has been discarded or the producer failed.
        """
        async with self._data_available:
            if len(self._chunks) == 0:
                # Producer errors also set EOF after buffered data; preserve the real failure.
                if self._producer_error:
                    raise self._producer_error
                if self._eof_received or self.cancelled:
                    raise AudioBufferEOF
            if self.cancelled:
                raise AudioBufferEOF

            if self.mode == BufferMode.ROLLING:
                return await self._get_rolling()

            return await self._get_seekable(chunk_number)

    async def _get_rolling(self) -> bytes:
        """
        Pop the next chunk from the buffer (FIFO).

        Must be called while holding _data_available lock.
        """
        while len(self._chunks) == 0:
            if self._producer_error:
                raise self._producer_error
            if self.cancelled or self._eof_received:
                raise AudioBufferEOF
            await self._data_available.wait()

        result = self._chunks.popleft()
        self._discarded_chunks += 1
        self._served_chunks = self._discarded_chunks
        self._space_available.notify_all()
        return result

    async def _get_seekable(self, chunk_number: int) -> bytes:
        """
        Get a specific chunk by number from the buffer.

        Must be called while holding _data_available lock.
        """
        if chunk_number < self._discarded_chunks:
            msg = (
                f"Chunk {chunk_number} has been discarded "
                f"(buffer starts at {self._discarded_chunks})"
            )
            raise AudioError(msg)

        buffer_index = chunk_number - self._discarded_chunks
        while buffer_index >= len(self._chunks):
            # Producer errors also set EOF after buffered data; preserve the real failure.
            if self._producer_error:
                raise self._producer_error
            if self.cancelled or self._eof_received:
                raise AudioBufferEOF
            # if the buffer is full and we need a chunk that hasn't arrived yet,
            # the producer is blocked waiting for space — evict to unblock it
            if len(self._chunks) >= self.max_size_seconds:
                self._chunks.popleft()
                self._discarded_chunks += 1
                buffer_index = chunk_number - self._discarded_chunks
                self._space_available.notify_all()
                continue
            await self._data_available.wait()
            buffer_index = chunk_number - self._discarded_chunks

        result = self._chunks[buffer_index]
        self._served_chunks = max(self._served_chunks, chunk_number + 1)

        # free space for the producer when buffer is at capacity,
        # but only if the producer is still running and needs space
        if (
            len(self._chunks) >= self.max_size_seconds
            and not self._eof_received
            and self._producer_task
            and not self._producer_task.done()
        ):
            self._chunks.popleft()
            self._discarded_chunks += 1
            self._space_available.notify_all()

        return result

    async def _wait_for_space(self) -> None:
        """Wait until buffer has space. Must be called while holding _lock."""
        while len(self._chunks) >= self.max_size_seconds:
            if self._cancelled:
                return
            await self._space_available.wait()

    def _attach_producer_task(self, task: asyncio.Task[Any]) -> None:
        """Attach a background task that fills the buffer."""
        self._producer_task = task

        def _on_producer_done(t: asyncio.Task[Any]) -> None:
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None and isinstance(exc, Exception):
                self._producer_error = exc
                loop = asyncio.get_running_loop()
                task = loop.create_task(self._notify_on_producer_error())
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)

        task.add_done_callback(_on_producer_done)

        if self._inactivity_task is None or self._inactivity_task.done():
            self._last_access_time = time.time()
            loop = asyncio.get_running_loop()
            self._inactivity_task = loop.create_task(self._monitor_inactivity())

    async def _monitor_inactivity(
        self, inactivity_timeout: float = 300, check_interval: float = 30
    ) -> None:
        """
        Clear the buffer once it has been inactive for inactivity_timeout seconds.

        :param inactivity_timeout: Seconds without access before the buffer is released.
        :param check_interval: Seconds between inactivity checks.
        """
        while True:
            await asyncio.sleep(check_interval)
            time_since_access = time.time() - self._last_access_time
            # break on inactivity regardless of how many chunks remain: a rolling buffer
            # that has drained to empty (e.g. an abandoned radio stream) must still release
            # its resources and stop this monitor, otherwise the task loops forever
            if time_since_access > inactivity_timeout:
                LOGGER.log(
                    VERBOSE_LOG_LEVEL,
                    "AudioBuffer: No activity for %.1fs, clearing (%s chunks)",
                    time_since_access,
                    len(self._chunks),
                )
                break
        await self.clear(cancel_inactivity_task=False)

    async def _notify_on_producer_error(self) -> None:
        """Notify waiting consumers that the producer has failed."""
        async with self._lock:
            if not self.ready.is_set():
                self.ready.set()
            self._data_available.notify_all()


class ProviderAudioFill:
    """
    Write side of a buffer a provider fills itself, for audio nothing has requested yet.

    Not a buffer of its own: an adapter between the provider pushing chunks and the
    ``AudioBuffer`` pulling them, carrying the release contract back to the provider.

    The provider writes whole PCM sample frames in ``pcm_format`` as its source produces
    them, and ends the item with :meth:`close` (its audio has all been handed over) or
    :meth:`fail` (it was cut short). Writes to a handle that is no longer ``active`` are
    dropped: whatever was going to read them is gone.
    """

    def __init__(
        self,
        pcm_format: AudioFormat,
        streamdetails: StreamDetails | None = None,
        buffer: AudioBuffer | None = None,
    ) -> None:
        """
        Initialize the handle.

        :param pcm_format: The PCM format the provider writes.
        :param streamdetails: The stream details the audio belongs to, when known.
        :param buffer: The buffer being filled, when the audio goes to one.
        """
        self.pcm_format = pcm_format
        self.streamdetails = streamdetails
        self._buffer = buffer
        self._chunks: deque[bytes] = deque()
        self._pending_bytes = 0
        self._error: Exception | None = None
        self._data_available = asyncio.Event()
        self._closed = False
        self._released = False
        if buffer is not None:
            # a buffer released elsewhere (a queue stop, a reselection) takes the handle
            # with it, so the provider stops writing into audio nothing can read
            buffer.register_cancel_callback(self._release)

    @property
    def active(self) -> bool:
        """Return whether audio written to this handle can still reach a consumer."""
        return not self._closed and not self._released

    @property
    def pending_seconds(self) -> float:
        """Return the seconds of written audio playback has not consumed yet."""
        pending = self._pending_bytes / self.pcm_format.pcm_sample_size
        # the buffer written to directly, or - for a handle handed over as a stream -
        # the one the item's audio is being streamed into
        buffer = self._buffer or (self.streamdetails.buffer if self.streamdetails else None)
        if isinstance(buffer, AudioBuffer):
            pending += buffer.undrained_seconds
        return pending

    def write(self, chunk: bytes) -> None:
        """
        Hand over the next PCM audio for this item.

        :param chunk: Whole sample frames in this handle's PCM format.
        """
        if not self.active or not chunk:
            return
        self._chunks.append(chunk)
        self._pending_bytes += len(chunk)
        self._data_available.set()

    def close(self) -> None:
        """End the item: everything it was going to deliver has been written."""
        if self._closed:
            return
        self._closed = True
        self._data_available.set()

    def fail(self, error: Exception) -> None:
        """
        End the item on a failure, so a consumer sees the real cause.

        :param error: Why the item's audio was not delivered in full.
        """
        if self._closed:
            return
        self._error = error
        self._closed = True
        self._data_available.set()

    async def stream(self) -> AsyncGenerator[bytes]:
        """Yield the written audio as it arrives, ending where the provider ended it."""
        try:
            while True:
                while self._chunks:
                    chunk = self._chunks.popleft()
                    self._pending_bytes -= len(chunk)
                    yield chunk
                if self._closed:
                    if self._error is not None:
                        raise self._error
                    return
                self._data_available.clear()
                await self._data_available.wait()
        finally:
            self._release()

    def _release(self) -> None:
        """Drop the handle: nothing is going to read what is written from here on."""
        self._released = True
        self._chunks.clear()
        self._pending_bytes = 0


async def _in_chunks_of(source: AsyncGenerator[bytes], chunk_size: int) -> AsyncGenerator[bytes]:
    """
    Re-cut an audio source into chunks of the given size.

    The trailing part-chunk is yielded as it is: it is still the source's audio.

    :param source: The audio to re-cut.
    :param chunk_size: Size in bytes of each yielded chunk.
    """
    held = bytearray()
    async with aclosing(source):
        async for chunk in source:
            held.extend(chunk)
            while len(held) >= chunk_size:
                yield bytes(held[:chunk_size])
                del held[:chunk_size]
    if held:
        yield bytes(held)


def _new_buffer(
    mass: MusicAssistant,
    streamdetails: StreamDetails,
    seek_position_ms: int,
    log_prefix: str,
) -> tuple[AudioBuffer, int]:
    """
    Create the buffer for the given stream details and attach it to them.

    :param mass: The MusicAssistant instance.
    :param streamdetails: The stream details the buffer belongs to.
    :param seek_position_ms: Position in milliseconds playback starts from.
    :param log_prefix: Caller context for logging.
    :return: The buffer and the position (in seconds) its producer should start at.
    """
    # determine buffer size from config
    buffer_size = BufferSize(
        mass.config.get_raw_core_config_value("streams", CONF_BUFFER_SIZE, CONF_BUFFER_SIZE_DEFAULT)
    )
    mode = (
        BufferMode.ROLLING
        if (not streamdetails.duration or not streamdetails.allow_seek)
        else BufferMode.SEEKABLE
    )

    # convert ms to seconds for get_media_stream (FFmpeg works in seconds)
    seek_seconds = seek_position_ms // 1000

    # for large seeks without existing buffer, start at seek position.
    # A realtime source can not produce the skipped audio any faster than playback,
    # so it always seeks at the source instead of buffering up to the seek point.
    buffer_seek_seconds = seek_seconds if streamdetails.is_realtime or seek_seconds > 60 else 0

    pcm_format = _buffer_pcm_format(streamdetails)

    # determine ready threshold: how many seconds of audio must be buffered
    # before signaling ready for playback
    queue = mass.player_queues.get(streamdetails.queue_id) if streamdetails.queue_id else None
    crossfade_enabled = bool(
        queue and queue.crossfade_enabled and streamdetails.media_type == MediaType.TRACK
    )
    dynamic_normalization = (
        streamdetails.volume_normalization_mode == VolumeNormalizationMode.DYNAMIC
    )
    if streamdetails.is_realtime:
        # A realtime source fills the buffer at playback pace, so every second of
        # audio asked for here is a second of extra startup delay - on a seek or a
        # track change as much as on a start. The queue's crossfade setting buys
        # nothing for such a source, because its fade streams in as it arrives and
        # is sized by the tail the outgoing track banked, not by what is resident
        # here. Only dynamic normalization, which genuinely needs lookahead, raises
        # this.
        ready_threshold = 2 if dynamic_normalization else 1
    elif crossfade_enabled:
        ready_threshold = 8
    elif dynamic_normalization:
        # radio streams are continuous so the normalization will converge quickly,
        # use a lower threshold to reduce startup latency
        ready_threshold = 3 if streamdetails.media_type == MediaType.RADIO else 5
    else:
        ready_threshold = 2

    # cap threshold at buffer capacity to prevent deadlock
    max_size = RADIO_BUFFER_SIZE if mode == BufferMode.ROLLING else BUFFER_SIZE_MAP[buffer_size]
    ready_threshold = min(ready_threshold, max_size)

    LOGGER.debug(
        "%s: Creating new buffer for %s (mode: %s, size: %s, seek_ms: %s)",
        log_prefix,
        streamdetails.uri,
        mode,
        buffer_size,
        seek_position_ms,
    )
    audio_buffer = AudioBuffer(
        pcm_format,
        buffer_size,
        mode,
        ready_threshold=ready_threshold,
        is_realtime=streamdetails.is_realtime,
    )
    # align chunk numbering with the actual stream start position so that
    # get_raw_stream(seek_position_ms) requests the correct chunk number
    audio_buffer._discarded_chunks = buffer_seek_seconds
    # nothing has been read yet, so the source is not ahead of playback here
    audio_buffer._served_chunks = buffer_seek_seconds
    # set the chunk number at which the buffer should signal ready,
    # accounting for seek position so we have enough data past the seek point
    seek_chunk = seek_position_ms // 1000
    audio_buffer._ready_at_chunk = seek_chunk + ready_threshold
    streamdetails.buffer = audio_buffer

    # attach analyze jobs for ahead-of-time processing
    # skip AudioSource and SoundEffect — they should not feed the long-running analyzer flow
    # (radio still runs analysis; the analyzer caps it at 10 minutes)
    if seek_position_ms == 0 and streamdetails.media_type not in (
        MediaType.AUDIO_SOURCE,
        MediaType.SOUND_EFFECT,
    ):
        # audio analysis providers (loudness, beat tracking, key detection, etc.).
        # Fire-and-forget: analysis setup — including a possible model (re)load — must never
        # delay the buffer fill. The analysis worker reads the retained chunks once ready.
        mass.create_task(mass.streams.audio_analysis.start_analysis(audio_buffer, streamdetails))

    return audio_buffer, buffer_seek_seconds


def _buffer_pcm_format(streamdetails: StreamDetails) -> AudioFormat:
    """
    Return the PCM format a buffer for these streamdetails holds.

    The buffer stores decoded PCM, so it follows the audio that actually
    arrives: ``audio_format`` may describe a source the provider decoded on our
    behalf and can differ in depth or rate, in which case deriving the buffer
    from it would resample or truncate real audio.

    :param streamdetails: The stream the buffer is for.
    """
    arriving = arriving_audio_format(streamdetails)
    return AudioFormat(
        content_type=ContentType.from_bit_depth(arriving.bit_depth),
        sample_rate=arriving.sample_rate,
        bit_depth=arriving.bit_depth,
        # buffer the stereo fold of a surround source, so audio analysis measures
        # the same audio that is played back rather than the untouched surround mix
        channels=min(arriving.channels, 2),
    )
