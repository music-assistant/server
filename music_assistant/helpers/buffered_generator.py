"""Helper for adding buffering to async (audio) generators."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator, Callable
from functools import wraps
from typing import Any, Final, ParamSpec

from music_assistant.helpers.util import close_async_generator, empty_queue

# Type variables for the buffered decorator
_P = ParamSpec("_P")

DEFAULT_BUFFER_SIZE: Final = 30
DEFAULT_MIN_BUFFER_BEFORE_YIELD: Final = 5

# Keep strong references to producer tasks to prevent garbage collection
# The event loop only keeps weak references to tasks
_ACTIVE_PRODUCER_TASKS: set[asyncio.Task[Any]] = set()


async def buffered(
    generator: AsyncGenerator[bytes, None],
    buffer_size: int = DEFAULT_BUFFER_SIZE,
    min_buffer_before_yield: int = DEFAULT_MIN_BUFFER_BEFORE_YIELD,
) -> AsyncGenerator[bytes, None]:
    """
    Add buffering to an async generator that yields (chunks of) bytes.

    This function uses an asyncio.Queue to decouple the producer (reading from the stream)
    from the consumer (yielding to the client). The producer runs in a separate task and
    fills the buffer, while the consumer yields from the buffer.

    Args:
        generator: The async generator to buffer
        buffer_size: Maximum number of chunks to buffer (default: 30)
        min_buffer_before_yield: Minimum chunks to buffer before starting to yield (default: 5)

    Example:
        async for chunk in buffered(my_generator(), buffer_size=100):
            process(chunk)
    """
    buffer: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=buffer_size)
    producer_error: Exception | None = None
    threshold_reached = asyncio.Event()
    cancelled = asyncio.Event()

    if buffer_size <= 1:
        # No buffering needed, yield directly
        async for chunk in generator:
            yield chunk
        return

    async def producer() -> None:
        """Read from the original generator and fill the buffer.

        Note: When the buffer is full, buffer.put() will naturally wait for the consumer
        to drain items. This is the intended buffering behavior and may trigger asyncio
        "slow callback" warnings (typically 0.1-0.2s) which are harmless and expected.
        These warnings are filtered out in the main logging configuration.
        """
        nonlocal producer_error
        generator_consumed = False
        try:
            async for chunk in generator:
                generator_consumed = True
                if cancelled.is_set():
                    # Consumer has stopped, exit cleanly
                    break
                await buffer.put(chunk)
                if not threshold_reached.is_set() and buffer.qsize() >= min_buffer_before_yield:
                    threshold_reached.set()
                # Yield to event loop every chunk to prevent blocking
                await asyncio.sleep(0)
        except Exception as err:
            producer_error = err
            if isinstance(err, asyncio.CancelledError):
                raise
        finally:
            threshold_reached.set()
            # Clean up the generator if needed
            if not generator_consumed:
                await close_async_generator(generator)
            # Signal end of stream by putting None
            # We must wait for space in the queue if needed, otherwise the consumer may
            # hang waiting for data that will never come
            if not cancelled.is_set():
                await buffer.put(None)

    # Start the producer task
    loop = asyncio.get_running_loop()
    producer_task = loop.create_task(producer())

    # Keep a strong reference to prevent garbage collection issues
    # The event loop only keeps weak references to tasks
    _ACTIVE_PRODUCER_TASKS.add(producer_task)

    # Remove from set when done
    producer_task.add_done_callback(_ACTIVE_PRODUCER_TASKS.discard)

    try:
        # Wait for initial buffer to fill
        await threshold_reached.wait()

        # Consume from buffer and yield
        while True:
            data = await buffer.get()
            if data is None:
                # End of stream
                if producer_error:
                    raise producer_error
                break
            yield data

    finally:
        # Signal the producer to stop
        cancelled.set()
        # Drain the queue to unblock the producer if it's waiting on put()
        empty_queue(buffer)
        # Wait for the producer to finish cleanly with a timeout to prevent blocking
        with contextlib.suppress(asyncio.CancelledError, RuntimeError, asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(producer_task), timeout=1.0)


def use_buffer(
    buffer_size: int = DEFAULT_BUFFER_SIZE,
    min_buffer_before_yield: int = DEFAULT_MIN_BUFFER_BEFORE_YIELD,
) -> Callable[
    [Callable[_P, AsyncGenerator[bytes, None]]],
    Callable[_P, AsyncGenerator[bytes, None]],
]:
    """
    Add buffering to async generator functions that yield bytes (decorator).

    This decorator uses an asyncio.Queue to decouple the producer (reading from the stream)
    from the consumer (yielding to the client). The producer runs in a separate task and
    fills the buffer, while the consumer yields from the buffer.

    Args:
        buffer_size: Maximum number of chunks to buffer (default: 30)
        min_buffer_before_yield: Minimum chunks to buffer before starting to yield (default: 5)

    Example:
        @use_buffer(buffer_size=100)
        async def my_stream() -> AsyncGenerator[bytes, None]:
            ...
    """

    def decorator(
        func: Callable[_P, AsyncGenerator[bytes, None]],
    ) -> Callable[_P, AsyncGenerator[bytes, None]]:
        @wraps(func)
        async def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> AsyncGenerator[bytes, None]:
            async for chunk in buffered(
                func(*args, **kwargs),
                buffer_size=buffer_size,
                min_buffer_before_yield=min_buffer_before_yield,
            ):
                yield chunk

        return wrapper

    return decorator
