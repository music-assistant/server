"""Helper for adding buffering to async generators."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator, Callable
from functools import wraps
from typing import Final, ParamSpec

# Type variables for the buffered decorator
_P = ParamSpec("_P")

DEFAULT_BUFFER_SIZE: Final = 30
DEFAULT_MIN_BUFFER_BEFORE_YIELD: Final = 5


async def buffered(
    generator: AsyncGenerator[bytes, None],
    buffer_size: int = DEFAULT_BUFFER_SIZE,
    min_buffer_before_yield: int = DEFAULT_MIN_BUFFER_BEFORE_YIELD,
) -> AsyncGenerator[bytes, None]:
    """
    Add buffering to an async generator that yields bytes.

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

    if buffer_size <= 1:
        # No buffering needed, yield directly
        async for chunk in generator:
            yield chunk
        return

    async def producer() -> None:
        """Read from the original generator and fill the buffer."""
        nonlocal producer_error
        try:
            async for chunk in generator:
                await buffer.put(chunk)
        except Exception as err:
            producer_error = err
            # Consumer probably stopped consuming, close the original generator to prevent
            # "Task was destroyed but it is pending!" warnings
            with contextlib.suppress(RuntimeError, asyncio.CancelledError):
                await generator.aclose()
        finally:
            # Signal end of stream by putting None
            await buffer.put(None)

    # Start the producer task
    loop = asyncio.get_running_loop()
    producer_task = loop.create_task(producer(), eager_start=True)  # type: ignore[call-arg]

    try:
        # Wait for initial buffer to fill
        chunks_buffered = 0
        while chunks_buffered < min_buffer_before_yield:
            data = await buffer.get()
            if data is None:
                # Stream ended before minimum buffer was reached
                if producer_error:
                    raise producer_error
                return
            chunks_buffered += 1
            # Put it back for the consumer loop
            await buffer.put(data)

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
        # Ensure the producer task is cleaned up
        if not producer_task.done():
            producer_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await producer_task


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
        buffer_size: Maximum number of chunks to buffer (default: 60)
        min_buffer_before_yield: Minimum chunks to buffer before starting to yield (default: 10)

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
