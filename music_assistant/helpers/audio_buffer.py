"""Audio buffer implementation for PCM audio streaming."""

from __future__ import annotations

import asyncio
import gc
import logging
import time
from collections import deque
from collections.abc import AsyncGenerator
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from music_assistant_models.errors import AudioError

from music_assistant.constants import MASS_LOGGER_NAME, VERBOSE_LOG_LEVEL

if TYPE_CHECKING:
    from music_assistant_models.media_items import AudioFormat

LOGGER = logging.getLogger(f"{MASS_LOGGER_NAME}.audio_buffer")

DEFAULT_MAX_BUFFER_SIZE_SECONDS: int = 60 * 5  # 5 minutes


class AudioBuffer:
    """Simple buffer to hold (PCM) audio chunks with seek capability.

    Each chunk represents exactly 1 second of audio.
    Chunks are stored in a deque for efficient O(1) append and popleft operations.
    """

    def __init__(
        self,
        pcm_format: AudioFormat,
        checksum: str,
        max_size_seconds: int = DEFAULT_MAX_BUFFER_SIZE_SECONDS,
    ) -> None:
        """
        Initialize AudioBuffer.

        Args:
            pcm_format: The PCM audio format specification
            checksum: The checksum for the audio data (for validation purposes)
            max_size_seconds: Maximum buffer size in seconds
        """
        self.pcm_format = pcm_format
        self.checksum = checksum
        self.max_size_seconds = max_size_seconds
        # Store chunks in a deque for O(1) append and popleft operations
        self._chunks: deque[bytes] = deque()
        # Track how many chunks have been discarded from the start
        self._discarded_chunks = 0
        self._lock = asyncio.Lock()
        self._data_available = asyncio.Condition(self._lock)
        self._space_available = asyncio.Condition(self._lock)
        self._eof_received = False
        self._buffer_fill_task: asyncio.Task[None] | None = None
        self._last_access_time: float = time.time()
        self._inactivity_task: asyncio.Task[None] | None = None
        self._cancelled = False  # Set to True when buffer is cleared/cancelled

    @property
    def cancelled(self) -> bool:
        """Return whether the buffer has been cancelled or cleared."""
        if self._cancelled:
            return True
        return self._buffer_fill_task is not None and self._buffer_fill_task.cancelled()

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
        """Return number of seconds of audio currently available in the buffer."""
        return len(self._chunks)

    def is_valid(self, checksum: str, seek_position: int = 0) -> bool:
        """
        Validate the buffer's checksum and check if seek position is available.

        Args:
            checksum: The checksum to validate against
            seek_position: The position we want to seek to (0-based)

        Returns:
            True if buffer is valid and seek position is available
        """
        if self.cancelled:
            return False

        if self.checksum != checksum:
            return False

        # Check if buffer is close to inactivity timeout (within 30 seconds)
        # to prevent race condition where buffer gets cleared right after validation
        time_since_access = time.time() - self._last_access_time
        inactivity_timeout = 60 * 5  # 5 minutes
        if time_since_access > (inactivity_timeout - 30):
            # Buffer is close to being cleared, don't reuse it
            return False

        # Check if the seek position has already been discarded
        return seek_position >= self._discarded_chunks

    async def put(self, chunk: bytes) -> None:
        """
        Put a chunk of data into the buffer.

        Each chunk represents exactly 1 second of PCM audio.
        Waits if buffer is full.

        Args:
            chunk: Bytes representing 1 second of PCM audio
        """
        async with self._space_available:
            # Wait until there's space in the buffer
            while len(self._chunks) >= self.max_size_seconds and not self._eof_received:
                if LOGGER.isEnabledFor(VERBOSE_LOG_LEVEL):
                    LOGGER.log(
                        VERBOSE_LOG_LEVEL,
                        "AudioBuffer.put: Buffer full (%s/%s), waiting for space...",
                        len(self._chunks),
                        self.max_size_seconds,
                    )
                await self._space_available.wait()

            if self._eof_received:
                # Don't accept new data after EOF
                LOGGER.log(
                    VERBOSE_LOG_LEVEL, "AudioBuffer.put: EOF already received, rejecting chunk"
                )
                return

            # Add chunk to the list (index = second position)
            self._chunks.append(chunk)
            if LOGGER.isEnabledFor(VERBOSE_LOG_LEVEL):
                LOGGER.log(
                    VERBOSE_LOG_LEVEL,
                    "AudioBuffer.put: Added chunk at position %s (size: %s bytes, buffer size: %s)",
                    self._discarded_chunks + len(self._chunks) - 1,
                    len(chunk),
                    len(self._chunks),
                )

            # Notify waiting consumers
            self._data_available.notify_all()

    async def get(self, chunk_number: int = 0) -> bytes:
        """
        Get one second of data from the buffer at the specified chunk number.

        Waits until requested chunk is available.
        Discards old chunks if buffer is full.

        Args:
            chunk_number: The chunk index to retrieve (0-based, absolute position).

        Returns:
            Bytes containing one second of audio data

        Raises:
            AudioError: If EOF is reached before chunk is available or
                       if chunk has been discarded
        """
        # Update last access time
        self._last_access_time = time.time()

        async with self._data_available:
            # Check if the chunk was already discarded
            if chunk_number < self._discarded_chunks:
                msg = (
                    f"Chunk {chunk_number} has been discarded "
                    f"(buffer starts at {self._discarded_chunks})"
                )
                raise AudioError(msg)

            # Wait until the requested chunk is available or EOF
            buffer_index = chunk_number - self._discarded_chunks
            while buffer_index >= len(self._chunks):
                if self._eof_received:
                    raise AudioError("EOF")
                await self._data_available.wait()
                buffer_index = chunk_number - self._discarded_chunks

            # If buffer is at max size, discard the oldest chunk to make room
            if len(self._chunks) >= self.max_size_seconds:
                discarded = self._chunks.popleft()  # O(1) operation with deque
                self._discarded_chunks += 1
                if LOGGER.isEnabledFor(VERBOSE_LOG_LEVEL):
                    LOGGER.log(
                        VERBOSE_LOG_LEVEL,
                        "AudioBuffer.get: Discarded chunk %s (size: %s bytes) to free space",
                        self._discarded_chunks - 1,
                        len(discarded),
                    )
                # Notify producers waiting for space
                self._space_available.notify_all()
                # Recalculate buffer index after discard
                buffer_index = chunk_number - self._discarded_chunks

            # Return the chunk at the requested index
            return self._chunks[buffer_index]

    async def iter(self, seek_position: int = 0) -> AsyncGenerator[bytes, None]:
        """
        Iterate over seconds of audio data until EOF.

        Args:
            seek_position: Optional starting position in seconds (default: 0).

        Yields:
            Bytes containing one second of audio data
        """
        chunk_number = seek_position
        while True:
            try:
                yield await self.get(chunk_number=chunk_number)
                chunk_number += 1
            except AudioError:
                break  # EOF reached

    async def clear(self) -> None:
        """Reset the buffer completely, clearing all data."""
        chunk_count = len(self._chunks)
        LOGGER.log(
            VERBOSE_LOG_LEVEL,
            "AudioBuffer.clear: Resetting buffer (had %s chunks, has fill task: %s)",
            chunk_count,
            self._buffer_fill_task is not None,
        )
        if self._buffer_fill_task:
            self._buffer_fill_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._buffer_fill_task
        if self._inactivity_task:
            self._inactivity_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._inactivity_task
        async with self._lock:
            self._chunks.clear()
            self._discarded_chunks = 0
            self._eof_received = False
            self._cancelled = True  # Mark buffer as cancelled
            # Notify all waiting tasks
            self._data_available.notify_all()
            self._space_available.notify_all()

        # Run garbage collection in executor to reclaim memory from large buffers
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, gc.collect)

    async def set_eof(self) -> None:
        """Signal that no more data will be added to the buffer."""
        async with self._lock:
            LOGGER.log(
                VERBOSE_LOG_LEVEL,
                "AudioBuffer.set_eof: Marking EOF (buffer has %s chunks)",
                len(self._chunks),
            )
            self._eof_received = True
            # Wake up all waiting consumers and producers
            self._data_available.notify_all()
            self._space_available.notify_all()

    async def _monitor_inactivity(self) -> None:
        """Monitor buffer for inactivity and clear if inactive for 5 minutes."""
        inactivity_timeout = 60 * 5  # 5 minutes
        check_interval = 30  # Check every 30 seconds
        while True:
            await asyncio.sleep(check_interval)

            # Check if buffer has been inactive (no data and no activity)
            time_since_access = time.time() - self._last_access_time

            # If buffer is empty and hasn't been accessed for timeout period,
            # it likely means the producer failed or stream was abandoned
            if len(self._chunks) == 0 and time_since_access > inactivity_timeout:
                LOGGER.log(
                    VERBOSE_LOG_LEVEL,
                    "AudioBuffer: Empty buffer with no activity for %.1f seconds, "
                    "clearing (likely abandoned stream)",
                    time_since_access,
                )
                await self.clear()
                break  # Stop monitoring after clearing

            # If buffer has data but hasn't been consumed, clear it
            if len(self._chunks) > 0 and time_since_access > inactivity_timeout:
                LOGGER.log(
                    VERBOSE_LOG_LEVEL,
                    "AudioBuffer: No activity for %.1f seconds, clearing buffer (had %s chunks)",
                    time_since_access,
                    len(self._chunks),
                )
                await self.clear()
                break  # Stop monitoring after clearing

    def attach_fill_task(self, task: asyncio.Task[Any]) -> None:
        """Attach a background task that fills the buffer."""
        self._buffer_fill_task = task

        # Start inactivity monitor if not already running
        if self._inactivity_task is None or self._inactivity_task.done():
            self._last_access_time = time.time()  # Initialize access time
            loop = asyncio.get_running_loop()
            self._inactivity_task = loop.create_task(self._monitor_inactivity())
