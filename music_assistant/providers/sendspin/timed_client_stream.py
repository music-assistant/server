"""
Timestamped multi-client audio stream for position-aware playback.

This module provides a multi-client streaming implementation optimized for
aiosendspin's synchronized multi-room audio playback. Each audio chunk is
timestamped, allowing late-joining players to start at the correct position
for synchronized playback across multiple devices.
"""

import asyncio
import logging
from collections import deque
from collections.abc import AsyncGenerator
from contextlib import suppress
from uuid import UUID, uuid4

from music_assistant_models.media_items import AudioFormat

from music_assistant.helpers.ffmpeg import get_ffmpeg_stream

LOGGER = logging.getLogger(__name__)

# Minimum/target buffer retention time in seconds
# This 10s buffer is currently required since:
# - aiosendspin currently uses a fixed 5s buffer to allow up to ~4s of network interruption
# - ~2s allows for ffmpeg processing time and some margin
# - ~3s are currently needed internally by aiosendspin for initial buffering
MIN_BUFFER_DURATION = 10.0
# Maximum buffer duration before raising an error (safety mechanism)
MAX_BUFFER_DURATION = MIN_BUFFER_DURATION + 5.0


class TimedClientStream:
    """Multi-client audio stream with timestamped chunks for synchronized playback."""

    audio_source: AsyncGenerator[bytes, None]
    """The source audio stream to read from."""
    audio_format: AudioFormat
    """The audio format of the source stream."""
    chunk_buffer: deque[tuple[bytes, float]]
    """Buffer storing chunks with their timestamps in seconds (chunk_data, timestamp_seconds)."""
    subscriber_positions: dict[UUID, int]
    """Subscriber positions: maps subscriber_id to position (index into chunk_buffer)."""
    buffer_lock: asyncio.Lock
    """Lock for buffer and shared state access."""
    source_read_lock: asyncio.Lock
    """Lock to serialize audio source reads."""
    stream_ended: bool = False
    """Track if stream has ended."""
    current_position: float = 0.0
    """Current position in seconds (from stream start)."""

    def __init__(
        self,
        audio_source: AsyncGenerator[bytes, None],
        audio_format: AudioFormat,
    ) -> None:
        """Initialize TimedClientStream."""
        self.audio_source = audio_source
        self.audio_format = audio_format
        self.chunk_buffer = deque()
        self.subscriber_positions = {}
        self.buffer_lock = asyncio.Lock()
        self.source_read_lock = asyncio.Lock()

    def _get_bytes_per_second(self) -> int:
        """Get bytes per second for the audio format."""
        return (
            self.audio_format.sample_rate
            * self.audio_format.channels
            * (self.audio_format.bit_depth // 8)
        )

    def _bytes_to_seconds(self, num_bytes: int) -> float:
        """Convert bytes to seconds based on audio format."""
        bytes_per_second = self._get_bytes_per_second()
        if bytes_per_second == 0:
            return 0.0
        return num_bytes / bytes_per_second

    def _get_buffer_duration(self) -> float:
        """Calculate total duration of buffered chunks in seconds."""
        if not self.chunk_buffer:
            return 0.0
        # Duration is from first chunk timestamp to current position
        first_chunk_timestamp = self.chunk_buffer[0][1]
        return self.current_position - first_chunk_timestamp

    def _cleanup_old_chunks(self) -> None:
        """Remove old chunks when all subscribers read them and min duration exceeded."""
        # Find the oldest position still needed by any subscriber
        if self.subscriber_positions:
            min_position = min(self.subscriber_positions.values())
        else:
            min_position = len(self.chunk_buffer)

        # Calculate target oldest timestamp
        # This ensures buffer contains at least MIN_BUFFER_DURATION seconds of recent data
        target_oldest = self.current_position - MIN_BUFFER_DURATION

        # Remove old chunks that meet both conditions:
        # 1. Before min_position (no subscriber needs them)
        # 2. Older than target_oldest (outside minimum retention window)
        chunks_removed = 0
        while chunks_removed < min_position and self.chunk_buffer:
            _chunk_bytes, chunk_timestamp = self.chunk_buffer[0]
            if chunk_timestamp < target_oldest:
                self.chunk_buffer.popleft()
                chunks_removed += 1
            else:
                # Stop when we reach chunks we want to keep
                break

        # Adjust all subscriber positions to account for removed chunks
        for sub_id in self.subscriber_positions:
            self.subscriber_positions[sub_id] -= chunks_removed

    async def _read_chunk_from_source(self) -> None:
        """Read next chunk from audio source and add to buffer."""
        try:
            chunk = await anext(self.audio_source)
            async with self.buffer_lock:
                # Calculate timestamp for this chunk
                chunk_timestamp = self.current_position
                chunk_duration = self._bytes_to_seconds(len(chunk))

                # Append chunk with its timestamp
                self.chunk_buffer.append((chunk, chunk_timestamp))

                # Update current position
                self.current_position += chunk_duration

                # Safety check: ensure buffer doesn't grow unbounded
                if self._get_buffer_duration() > MAX_BUFFER_DURATION:
                    msg = f"Buffer exceeded maximum duration ({MAX_BUFFER_DURATION}s)"
                    raise RuntimeError(msg)
        except StopAsyncIteration:
            # Source exhausted, add EOF marker
            async with self.buffer_lock:
                self.chunk_buffer.append((b"", self.current_position))
                self.stream_ended = True
        except Exception:
            # Source errored or was canceled, mark stream as ended
            async with self.buffer_lock:
                self.stream_ended = True
            raise

    async def _check_buffer(self, subscriber_id: UUID) -> bool | None:
        """
        Check if buffer has grown or stream ended.

        REQUIRES: Caller must hold self.source_read_lock before calling.

        Returns:
            True if should continue reading loop (chunk found in buffer),
            False if should break (stream ended),
            None if should proceed to read from source.
        """
        async with self.buffer_lock:
            position = self.subscriber_positions[subscriber_id]
            if position < len(self.chunk_buffer):
                # Another subscriber already read the chunk
                return True
            if self.stream_ended:
                # Stream ended while waiting for source lock
                return False
        return None  # Continue to read from source

    async def _get_chunk_from_buffer(self, subscriber_id: UUID) -> bytes | None:
        """
        Get next chunk from buffer for subscriber.

        Returns:
            Chunk bytes if available, None if no chunk available, or empty bytes for EOF.
        """
        async with self.buffer_lock:
            position = self.subscriber_positions[subscriber_id]

            # Check if we have a chunk at this position
            if position < len(self.chunk_buffer):
                # Chunk available in buffer
                chunk_data, _ = self.chunk_buffer[position]

                # Move to next position
                self.subscriber_positions[subscriber_id] = position + 1

                # Cleanup old chunks that no one needs
                self._cleanup_old_chunks()
                return chunk_data
            if self.stream_ended:
                # Stream ended and we've read all buffered chunks
                return b""
        return None

    async def _cleanup_subscriber(self, subscriber_id: UUID) -> None:
        """Clean up subscriber and close stream if no subscribers left."""
        async with self.buffer_lock:
            if subscriber_id in self.subscriber_positions:
                del self.subscriber_positions[subscriber_id]

            # If no subscribers left, close the stream
            if not self.subscriber_positions and not self.stream_ended:
                self.stream_ended = True
                # Close the audio source generator to prevent resource leak
                with suppress(Exception):
                    await self.audio_source.aclose()

    async def get_stream(
        self,
        output_format: AudioFormat,
        filter_params: list[str] | None = None,
    ) -> tuple[AsyncGenerator[bytes, None], float]:
        """
        Get (client specific encoded) ffmpeg stream.

        Returns:
            A tuple of (audio generator, actual position in seconds)
        """
        audio_gen, position = await self.subscribe_raw()

        async def _stream_with_ffmpeg() -> AsyncGenerator[bytes, None]:
            try:
                async for chunk in get_ffmpeg_stream(
                    audio_input=audio_gen,
                    input_format=self.audio_format,
                    output_format=output_format,
                    filter_params=filter_params,
                ):
                    yield chunk
            finally:
                # Ensure audio_gen cleanup runs immediately
                with suppress(Exception):
                    await audio_gen.aclose()

        return _stream_with_ffmpeg(), position

    async def _generate(self, subscriber_id: UUID) -> AsyncGenerator[bytes, None]:
        """
        Generate audio chunks for a subscriber.

        Yields chunks from the buffer until the stream ends, reading from the source
        as needed. Automatically cleans up the subscriber on exit.
        """
        try:
            # Position already set above atomically with timestamp capture
            while True:
                # Try to get chunk from buffer
                chunk_bytes = await self._get_chunk_from_buffer(subscriber_id)

                # Release lock before yielding to avoid deadlock
                if chunk_bytes is not None:
                    if chunk_bytes == b"":
                        # End of stream marker
                        break
                    yield chunk_bytes
                else:
                    # No chunk available, need to read from source
                    # Use source_read_lock to ensure only one subscriber reads at a time
                    async with self.source_read_lock:
                        # Check again if buffer has grown or stream ended while waiting
                        check_result = await self._check_buffer(subscriber_id)
                        if check_result is True:
                            # Another subscriber already read the chunk
                            continue
                        if check_result is False:
                            # Stream ended while waiting for source lock
                            break

                        # Read next chunk from source (check_result is None)
                        # Note: This may block if the audio_source does synchronous I/O
                        await self._read_chunk_from_source()

        finally:
            await self._cleanup_subscriber(subscriber_id)

    async def subscribe_raw(self) -> tuple[AsyncGenerator[bytes, None], float]:
        """
        Subscribe to the raw/unaltered audio stream.

        Returns:
            A tuple of (audio generator, actual position in seconds).
            The position indicates where in the stream the first chunk will be from.

        Note:
            Callers must properly consume or cancel the returned generator to prevent
            resource leaks.
        """
        subscriber_id = uuid4()

        # Atomically capture starting position and register subscriber while holding lock
        async with self.buffer_lock:
            if self.chunk_buffer:
                _, starting_position = self.chunk_buffer[0]
                # Log buffer time range for debugging
                newest_ts = self.chunk_buffer[-1][1]
                oldest_relative = starting_position - self.current_position
                newest_relative = newest_ts - self.current_position
                LOGGER.debug(
                    "New subscriber joining: buffer contains %.3fs (from %.3fs to %.3fs, "
                    "current_position=%.3fs)",
                    newest_ts - starting_position,
                    oldest_relative,
                    newest_relative,
                    self.current_position,
                )
            else:
                starting_position = self.current_position
                LOGGER.debug(
                    "New subscriber joining: buffer is empty, starting at current_position=%.3fs",
                    self.current_position,
                )
            # Register subscriber at position 0 (start of buffer)
            self.subscriber_positions[subscriber_id] = 0

        # Return generator and starting position in seconds
        return self._generate(subscriber_id), starting_position
