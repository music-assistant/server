"""Helper for adding buffering to async audio generators."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator, Callable
from functools import wraps
from typing import Any, Final, ParamSpec

from music_assistant_models.streamdetails import AudioFormat

from music_assistant.helpers.util import close_async_generator

# Type variables for the buffered decorator
_P = ParamSpec("_P")

DEFAULT_BUFFER_SIZE: Final = 30
DEFAULT_MIN_BUFFER_BEFORE_YIELD: Final = 5

# Keep strong references to producer tasks to prevent garbage collection
# The event loop only keeps weak references to tasks
_ACTIVE_PRODUCER_TASKS: set[asyncio.Task[Any]] = set()


async def buffered_audio(
    generator: AsyncGenerator[bytes, None],
    pcm_format: AudioFormat,
    buffer_size: int = DEFAULT_BUFFER_SIZE,
    min_buffer_before_yield: int = DEFAULT_MIN_BUFFER_BEFORE_YIELD,
) -> AsyncGenerator[bytes, None]:
    """
    Add buffering to an async audio generator that yields PCM audio bytes.

    This function uses a shared buffer with asyncio.Condition to decouple the producer
    (reading from the stream) from the consumer (yielding to the client).

    Ensures chunks yielded to the consumer are exactly 1 second of audio
    (critical for sync timing calculations).

    Args:
        generator: The async generator to buffer
        pcm_format: AudioFormat - defines chunk size for 1-second audio chunks
        buffer_size: Maximum number of 1-second chunks to buffer (default: 30)
        min_buffer_before_yield: Minimum chunks to buffer before starting to yield (default: 5)

    Example:
        async for chunk in buffered_audio(my_generator(), pcm_format, buffer_size=100):
            # Each chunk is exactly 1 second of audio
            process(chunk)
    """
    # Shared state between producer and consumer
    data_buffer = bytearray()  # Shared buffer for audio data
    condition = asyncio.Condition()  # Synchronization primitive
    producer_error: Exception | None = None
    producer_done = False
    cancelled = False

    # Calculate chunk size and buffer limits
    chunk_size = pcm_format.pcm_sample_size  # Size of 1 second of audio
    max_buffer_bytes = buffer_size * chunk_size

    if buffer_size <= 1:
        # No buffering needed, yield directly
        async for chunk in generator:
            yield chunk
        return

    async def producer() -> None:
        """Read from the original generator and fill the buffer."""
        nonlocal producer_error, producer_done, cancelled
        generator_consumed = False
        try:
            async for chunk in generator:
                generator_consumed = True
                if cancelled:
                    break

                # Wait if buffer is too full
                async with condition:
                    while len(data_buffer) >= max_buffer_bytes and not cancelled:
                        await condition.wait()

                    if cancelled:
                        break

                    # Append to shared buffer
                    data_buffer.extend(chunk)
                    # Notify consumer that data is available
                    condition.notify()

                # Yield to event loop to prevent blocking
                # Use 10ms delay to ensure I/O operations (pipe writes) can complete
                await asyncio.sleep(0.01)

        except Exception as err:
            producer_error = err
            if isinstance(err, asyncio.CancelledError):
                raise
        finally:
            # Clean up the generator if needed
            if not generator_consumed:
                await close_async_generator(generator)
            # Signal end of stream
            async with condition:
                producer_done = True
                condition.notify()

    # Start the producer task
    loop = asyncio.get_running_loop()
    producer_task = loop.create_task(producer())

    # Keep a strong reference to prevent garbage collection issues
    # The event loop only keeps weak references to tasks
    _ACTIVE_PRODUCER_TASKS.add(producer_task)

    # Remove from set when done
    producer_task.add_done_callback(_ACTIVE_PRODUCER_TASKS.discard)

    # Calculate minimum buffer level before yielding
    min_buffer_bytes = min_buffer_before_yield * chunk_size

    try:
        # Wait for initial buffer to fill
        async with condition:
            while len(data_buffer) < min_buffer_bytes and not producer_done:
                await condition.wait()

        # Consume from buffer and yield 1-second audio chunks
        while True:
            async with condition:
                # Wait for enough data or end of stream
                while len(data_buffer) < chunk_size and not producer_done:
                    await condition.wait()

                # Check if we're done
                if len(data_buffer) < chunk_size and producer_done:
                    # Yield any remaining partial chunk
                    if data_buffer:
                        chunk = bytes(data_buffer)
                        data_buffer.clear()
                        condition.notify()
                        yield chunk
                    if producer_error:
                        raise producer_error
                    break

                # Extract exactly 1 second of audio
                chunk = bytes(data_buffer[:chunk_size])
                del data_buffer[:chunk_size]

                # Notify producer that space is available
                condition.notify()

            # Yield outside the lock to avoid holding it during I/O
            yield chunk

    finally:
        # Signal the producer to stop
        async with condition:
            cancelled = True
            condition.notify()
        # Wait for the producer to finish cleanly with a timeout to prevent blocking
        with contextlib.suppress(asyncio.CancelledError, RuntimeError, asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(producer_task), timeout=1.0)


def use_audio_buffer(
    pcm_format_arg: str = "pcm_format",
    buffer_size: int = DEFAULT_BUFFER_SIZE,
    min_buffer_before_yield: int = DEFAULT_MIN_BUFFER_BEFORE_YIELD,
) -> Callable[
    [Callable[_P, AsyncGenerator[bytes, None]]],
    Callable[_P, AsyncGenerator[bytes, None]],
]:
    """
    Add buffering to async audio generator functions that yield PCM audio bytes (decorator).

    This decorator uses a shared buffer with asyncio.Condition to decouple the producer
    (reading from the stream) from the consumer (yielding to the client).

    Ensures chunks yielded are exactly 1 second of audio (critical for sync timing).

    Args:
        pcm_format_arg: Name of the argument containing AudioFormat (default: "pcm_format")
        buffer_size: Maximum number of 1-second chunks to buffer (default: 30)
        min_buffer_before_yield: Minimum chunks to buffer before starting to yield (default: 5)

    Example:
        @use_audio_buffer(pcm_format_arg="pcm_format", buffer_size=100)
        async def my_stream(pcm_format: AudioFormat) -> AsyncGenerator[bytes, None]:
            # Generator can yield variable-sized chunks
            # Decorator ensures output is exactly 1-second chunks
            ...
    """

    def decorator(
        func: Callable[_P, AsyncGenerator[bytes, None]],
    ) -> Callable[_P, AsyncGenerator[bytes, None]]:
        @wraps(func)
        async def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> AsyncGenerator[bytes, None]:
            # Extract pcm_format from function arguments
            pcm_format = kwargs.get(pcm_format_arg)
            if pcm_format is None:
                msg = f"Audio buffer decorator requires '{pcm_format_arg}' argument"
                raise ValueError(msg)

            async for chunk in buffered_audio(
                func(*args, **kwargs),
                pcm_format=pcm_format,
                buffer_size=buffer_size,
                min_buffer_before_yield=min_buffer_before_yield,
            ):
                yield chunk

        return wrapper

    return decorator
