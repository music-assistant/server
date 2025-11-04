"""Implementation of a simple multi-client stream task/job."""

import asyncio
import logging
from collections import deque
from collections.abc import AsyncGenerator
from contextlib import suppress
from uuid import UUID, uuid4

from music_assistant_models.media_items import AudioFormat

from music_assistant.helpers.ffmpeg import get_ffmpeg_stream

LOGGER = logging.getLogger(__name__)

# Minimum buffer retention time in seconds (10 seconds)
# TODO: tweak this dynamically based on current condition?
# So, we need 5s since that is the buffer size in aioresonate (query/provide to aioresonate)
# Plus we need some headroom for ffmpeg processing, +5s seams to work well with 44.1kHz, but other
# sample rates may require testing
# all in all, 10s buffer is a good starting point
MIN_BUFFER_DURATION = 10.0


### This is not just a simple multi-client stream, it is specifically designed to work with
### aioresonate. explain that. also in the module level docstring.
### maybe even rename?
class MultiClientStream:
    """Implementation of a simple multi-client (audio) stream task/job."""

    def __init__(
        self,
        audio_source: AsyncGenerator[bytes, None],
        audio_format: AudioFormat,
    ) -> None:
        """Initialize MultiClientStream."""
        ### move properties from __init_ to class level.
        ### and docstrings there too
        self.audio_source = audio_source
        self.audio_format = audio_format

        # Buffer storing chunks with their timestamps in seconds
        # Each item is a tuple of (chunk_data, timestamp_seconds)
        self.chunk_buffer: deque[tuple[bytes, float]] = deque()

        # Track subscriber positions and their read state
        # key: subscriber_id, value: position (index into chunk_buffer)
        self.subscriber_positions: dict[UUID, int] = {}

        # Lock for buffer and shared state access
        self.buffer_lock = asyncio.Lock()
        # Lock to serialize audio source reads
        self.source_read_lock = asyncio.Lock()

        # Track if stream has ended
        self.stream_ended = False

        # Track current position in seconds (from stream start)
        self.current_position = 0.0

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

    async def _cleanup_old_chunks(self) -> None:
        """Remove old chunks when all subscribers read them and min duration exceeded."""
        ### unneeded check? maybe not the only one in this file?
        if not self.chunk_buffer:
            return

        # Find the oldest position still needed by any subscriber
        if self.subscriber_positions:
            min_position = min(self.subscriber_positions.values())
        else:
            min_position = len(self.chunk_buffer)

        # Calculate target oldest timestamp
        # This ensures buffer contains at least MIN_BUFFER_DURATION seconds of recent data
        target_oldest = self.current_position - MIN_BUFFER_DURATION

        # Find how many chunks we can remove (respecting min_position)
        ### why not refactor so we pop in this loop?
        chunks_to_remove = 0
        for i in range(min_position):
            _chunk_bytes, chunk_timestamp = self.chunk_buffer[i]
            # Remove chunks older than target, but stop when we reach chunks we want to keep
            if chunk_timestamp < target_oldest:
                chunks_to_remove += 1
            else:
                break

        # Remove old chunks and adjust subscriber positions
        for _ in range(chunks_to_remove):
            self.chunk_buffer.popleft()

        # Adjust all subscriber positions
        ### do we need this? i mean with min_position set as above?
        for sub_id in self.subscriber_positions:
            pos = self.subscriber_positions[sub_id]
            self.subscriber_positions[sub_id] = max(0, pos - chunks_to_remove)

    async def _read_chunk_from_source(self, subscriber_id: UUID) -> None:
        """Read next chunk from audio source and add to buffer."""
        try:
            chunk = await self.audio_source.__anext__()
            async with self.buffer_lock:
                # Calculate timestamp for this chunk
                chunk_timestamp = self.current_position
                chunk_duration = self._bytes_to_seconds(len(chunk))

                # Append chunk with its timestamp
                self.chunk_buffer.append((chunk, chunk_timestamp))

                # Update current position
                self.current_position += chunk_duration
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

    async def _check_buffer_after_source_lock(self, subscriber_id: UUID) -> bool | None:
        """
        Check if buffer has grown or stream ended after acquiring source lock.

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
                await self._cleanup_old_chunks()
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
            async for chunk in get_ffmpeg_stream(
                audio_input=audio_gen,
                input_format=self.audio_format,
                output_format=output_format,
                filter_params=filter_params,
            ):
                yield chunk

        return _stream_with_ffmpeg(), position

    ### Review this method carefully
    async def subscribe_raw(self) -> tuple[AsyncGenerator[bytes, None], float]:
        """
        Subscribe to the raw/unaltered audio stream.

        Returns:
            A tuple of (audio generator, actual position in seconds).
            The position indicates where in the stream the first chunk will be from.
        """
        subscriber_id = uuid4()

        # Atomically capture starting position and register subscriber while holding lock
        async with self.buffer_lock:
            if self.chunk_buffer:
                _, starting_position = self.chunk_buffer[0]
                # Log buffer time range for debugging
                oldest_ts = self.chunk_buffer[0][1]
                newest_ts = self.chunk_buffer[-1][1]
                oldest_relative = oldest_ts - self.current_position
                newest_relative = newest_ts - self.current_position
                LOGGER.debug(
                    "New subscriber joining: buffer contains %.3fs (from %.3fs to %.3fs, "
                    "current_position=%.3fs)",
                    newest_ts - oldest_ts,
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
                        yield chunk_bytes
                    else:
                        # No chunk available, need to read from source
                        # Use source_read_lock to ensure only one subscriber reads at a time
                        async with self.source_read_lock:
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
                            await self._read_chunk_from_source(subscriber_id)

            finally:
                await self._cleanup_subscriber(subscriber_id)

        # Return generator and starting position in seconds
        return _generate(), starting_position
