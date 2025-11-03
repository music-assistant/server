"""Implementation of a simple multi-client stream task/job."""

import asyncio
import logging
from collections import deque
from collections.abc import AsyncGenerator
from contextlib import suppress
from uuid import uuid4

from music_assistant_models.media_items import AudioFormat

from music_assistant.helpers.ffmpeg import get_ffmpeg_stream

LOGGER = logging.getLogger(__name__)

# Minimum buffer retention time in microseconds (10 seconds)
# TODO: tweak this dynamically based on current condition?
# So, we need 5s since that is the buffer size in aioresonate (query/provide to aioresonate)
# Plus we need some headroom for ffmpeg processing, +5s seams to work well with 44.1kHz, but other
# sample rates may require testing
# all in all, 10s buffer is a good starting point
MIN_BUFFER_DURATION_US = 10_000_000


class MultiClientStream:
    """Implementation of a simple multi-client (audio) stream task/job."""

    def __init__(
        self,
        audio_source: AsyncGenerator[bytes, None],
        audio_format: AudioFormat,
    ) -> None:
        """Initialize MultiClientStream."""
        self.audio_source = audio_source
        self.audio_format = audio_format

        # Shared buffer - store chunks with their timestamps
        # Each item is a tuple of (chunk_data, timestamp_us)
        self.shared_buffer: deque[tuple[bytes, float]] = deque()

        # Track subscriber positions and their read state
        # key: subscriber_id, value: position (index into shared_buffer)
        self.subscriber_positions: dict[str, int] = {}

        # Lock for thread-safe buffer access
        self.buffer_lock = asyncio.Lock()

        # Lock to ensure only one subscriber reads from source at a time
        self.source_read_lock = asyncio.Lock()

        # Track if stream has ended
        self.stream_ended = False

        # Track current position in microseconds (from stream start) - float for precision
        self.current_position_us = 0.0

    def _get_bytes_per_second(self) -> int:
        """Get bytes per second for the audio format."""
        return (
            self.audio_format.sample_rate
            * self.audio_format.channels
            * (self.audio_format.bit_depth // 8)
        )

    def _bytes_to_microseconds(self, num_bytes: int) -> float:
        """Convert bytes to microseconds based on audio format."""
        bytes_per_second = self._get_bytes_per_second()
        if bytes_per_second == 0:
            return 0.0
        return (num_bytes * 1_000_000) / bytes_per_second

    def _get_buffer_duration_us(self) -> float:
        """Calculate total duration of buffered chunks in microseconds."""
        if not self.shared_buffer:
            return 0.0
        # Duration is from first chunk timestamp to current position
        first_chunk_timestamp = self.shared_buffer[0][1]
        return self.current_position_us - first_chunk_timestamp

    async def _cleanup_old_chunks(self) -> None:
        """Remove old chunks when all subscribers read them and min duration exceeded."""
        if not self.shared_buffer:
            return

        # Find the oldest position still needed by any subscriber
        if self.subscriber_positions:
            min_position = min(self.subscriber_positions.values())
        else:
            min_position = len(self.shared_buffer)

        # Calculate target oldest timestamp (t=-5 from current position)
        # This ensures buffer contains at least 5 seconds of recent data
        target_oldest_us = self.current_position_us - MIN_BUFFER_DURATION_US

        # Find how many chunks we can remove (respecting min_position)
        chunks_to_remove = 0
        for i in range(min_position):
            _chunk_bytes, chunk_timestamp_us = self.shared_buffer[i]
            # Remove chunks older than target, but stop when we reach chunks we want to keep
            if chunk_timestamp_us < target_oldest_us:
                chunks_to_remove += 1
            else:
                break

        # Remove old chunks and adjust subscriber positions
        for _ in range(chunks_to_remove):
            self.shared_buffer.popleft()

        # Adjust all subscriber positions
        for sub_id in self.subscriber_positions:
            pos = self.subscriber_positions[sub_id]
            self.subscriber_positions[sub_id] = max(0, pos - chunks_to_remove)

    async def _read_chunk_from_source(self, subscriber_id: str) -> None:
        """Read next chunk from audio source and add to buffer."""
        try:
            chunk = await self.audio_source.__anext__()
            LOGGER.debug(
                "Subscriber %s got chunk of %d bytes from source",
                subscriber_id,
                len(chunk),
            )
            async with self.buffer_lock:
                # Calculate timestamp for this chunk
                chunk_timestamp_us = self.current_position_us
                chunk_duration_us = self._bytes_to_microseconds(len(chunk))

                # Append chunk with its timestamp
                self.shared_buffer.append((chunk, chunk_timestamp_us))

                # Update current position
                self.current_position_us += chunk_duration_us
        except StopAsyncIteration:
            # Source exhausted, add EOF marker
            async with self.buffer_lock:
                self.shared_buffer.append((b"", self.current_position_us))
                self.stream_ended = True
        except Exception:
            # Source errored or was canceled, mark stream as ended
            async with self.buffer_lock:
                self.stream_ended = True
            raise

    async def _check_buffer_after_source_lock(self, subscriber_id: str) -> bool | None:
        """Check if buffer has grown or stream ended after acquiring source lock.

        Returns:
            True if should continue reading loop (chunk found in buffer),
            False if should break (stream ended),
            None if should proceed to read from source.
        """
        async with self.buffer_lock:
            position = self.subscriber_positions[subscriber_id]
            if position < len(self.shared_buffer):
                # Another subscriber already read the chunk
                LOGGER.debug(
                    "Subscriber %s found chunk in buffer, continuing",
                    subscriber_id,
                )
                return True
            if self.stream_ended:
                # Stream ended while waiting for source lock
                LOGGER.debug("Subscriber %s found stream ended", subscriber_id)
                return False
        return None  # Continue to read from source

    async def _get_chunk_from_buffer(self, subscriber_id: str) -> bytes | None:
        """Get next chunk from buffer for subscriber.

        Returns:
            Chunk bytes if available, None if no chunk available, or empty bytes for EOF.
        """
        async with self.buffer_lock:
            position = self.subscriber_positions[subscriber_id]

            # Check if we have a chunk at this position
            if position < len(self.shared_buffer):
                # Chunk available in buffer
                chunk_data, _ = self.shared_buffer[position]

                # Move to next position
                self.subscriber_positions[subscriber_id] = position + 1

                # Cleanup old chunks that no one needs
                await self._cleanup_old_chunks()
                return chunk_data
            if self.stream_ended:
                # Stream ended and we've read all buffered chunks
                return b""
        return None

    async def _cleanup_subscriber(self, subscriber_id: str) -> None:
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
    ) -> tuple[AsyncGenerator[bytes, None], int]:
        """Get (client specific encoded) ffmpeg stream.

        Returns:
            A tuple of (audio generator, actual position in microseconds)
        """
        audio_gen, position_us = await self.subscribe_raw()

        async def _stream_with_ffmpeg() -> AsyncGenerator[bytes, None]:
            async for chunk in get_ffmpeg_stream(
                audio_input=audio_gen,
                input_format=self.audio_format,
                output_format=output_format,
                filter_params=filter_params,
            ):
                yield chunk

        return _stream_with_ffmpeg(), position_us

    async def subscribe_raw(self) -> tuple[AsyncGenerator[bytes, None], int]:
        """Subscribe to the raw/unaltered audio stream.

        Returns:
            A tuple of (audio generator, actual position in microseconds).
            The position indicates where in the stream the first chunk will be from.
        """
        subscriber_id = str(uuid4())

        # Atomically capture starting position and register subscriber while holding lock
        async with self.buffer_lock:
            if self.shared_buffer:
                _, starting_position_us_float = self.shared_buffer[0]
                # Log buffer time range for debugging
                oldest_ts = self.shared_buffer[0][1]
                newest_ts = self.shared_buffer[-1][1]
                oldest_relative_ms = (oldest_ts - self.current_position_us) / 1000
                newest_relative_ms = (newest_ts - self.current_position_us) / 1000
                LOGGER.info(
                    "New subscriber joining: buffer contains %.0f ms (from t%.0fms to t%.0fms, "
                    "current_position=%.3fs)",
                    (newest_ts - oldest_ts) / 1000,
                    oldest_relative_ms,
                    newest_relative_ms,
                    self.current_position_us / 1_000_000,
                )
            else:
                starting_position_us_float = self.current_position_us
                LOGGER.info(
                    "New subscriber joining: buffer is empty, starting at current_position=%.3fs",
                    self.current_position_us / 1_000_000,
                )
            # Register subscriber at position 0 (start of buffer)
            self.subscriber_positions[subscriber_id] = 0

        # Convert to int for external API
        starting_position_us = int(starting_position_us_float)

        async def _generate() -> AsyncGenerator[bytes, None]:
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
                        LOGGER.debug(
                            "Subscriber %s read chunk of size %d bytes",
                            subscriber_id,
                            len(chunk_bytes),
                        )
                        yield chunk_bytes
                    else:
                        # No chunk available, need to read from source
                        # Use source_read_lock to ensure only one subscriber reads at a time
                        LOGGER.debug("Subscriber %s waiting for source_read_lock", subscriber_id)
                        async with self.source_read_lock:
                            LOGGER.debug("Subscriber %s acquired source_read_lock", subscriber_id)
                            # Check again if buffer has grown or stream ended while waiting
                            check_result = await self._check_buffer_after_source_lock(subscriber_id)
                            if check_result is True:
                                # Another subscriber already read the chunk
                                continue
                            if check_result is False:
                                # Stream ended while waiting for source lock
                                break

                            # Read next chunk from source (check_result is None)
                            # Note: This may block if the audio_source does synchronous I/O
                            LOGGER.debug("Subscriber %s reading from audio_source", subscriber_id)
                            await self._read_chunk_from_source(subscriber_id)

            finally:
                await self._cleanup_subscriber(subscriber_id)

        # Return generator and starting position in microseconds
        return _generate(), starting_position_us
