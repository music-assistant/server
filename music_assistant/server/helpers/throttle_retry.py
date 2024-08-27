"""Context manager using asyncio_throttle that catches and re-raises RetriesExhausted."""

import asyncio
import functools
import logging
import time
from collections import deque
from collections.abc import AsyncGenerator, Awaitable, Callable, Coroutine
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, Concatenate, ParamSpec, TypeVar

from music_assistant.common.models.errors import ResourceTemporarilyUnavailable, RetriesExhausted
from music_assistant.constants import MASS_LOGGER_NAME

if TYPE_CHECKING:
    from music_assistant.server.models.provider import Provider

_ProviderT = TypeVar("_ProviderT", bound="Provider")
_R = TypeVar("_R")
_P = ParamSpec("_P")
LOGGER = logging.getLogger(f"{MASS_LOGGER_NAME}.throttle_retry")

BYPASS_THROTTLER: ContextVar[bool] = ContextVar("BYPASS_THROTTLER", default=False)


class Throttler:
    """asyncio_throttle (https://github.com/hallazzang/asyncio-throttle).

    With improvements:
    - Accurate sleep without "busy waiting" (PR #4)
    - Return the delay caused by acquire()
    """

    def __init__(self, rate_limit: int, period=1.0):
        """Initialize the Throttler."""
        self.rate_limit = rate_limit
        self.period = period
        self._task_logs: deque[float] = deque()

    def _flush(self):
        now = time.monotonic()
        while self._task_logs:
            if now - self._task_logs[0] > self.period:
                self._task_logs.popleft()
            else:
                break

    async def acquire(self) -> float:
        """Acquire a free slot from the Throttler, returns the throttled time."""
        cur_time = time.monotonic()
        start_time = cur_time
        while True:
            self._flush()
            if len(self._task_logs) < self.rate_limit:
                break
            # sleep the exact amount of time until the oldest task can be flushed
            time_to_release = self._task_logs[0] + self.period - cur_time
            await asyncio.sleep(time_to_release)
            cur_time = time.monotonic()

        self._task_logs.append(cur_time)
        return cur_time - start_time  # exactly 0 if not throttled

    async def __aenter__(self) -> float:
        """Wait until the lock is acquired, return the time delay."""
        return await self.acquire()

    async def __aexit__(self, exc_type, exc, tb):
        """Nothing to do on exit."""


class ThrottlerManager:
    """Throttler manager that extends asyncio Throttle by retrying."""

    def __init__(self, rate_limit: int, period: float = 1, retry_attempts=5, initial_backoff=5):
        """Initialize the AsyncThrottledContextManager."""
        self.retry_attempts = retry_attempts
        self.initial_backoff = initial_backoff
        self.throttler = Throttler(rate_limit, period)

    @asynccontextmanager
    async def acquire(self) -> AsyncGenerator[None, float]:
        """Acquire a free slot from the Throttler, returns the throttled time."""
        if BYPASS_THROTTLER.get():
            yield 0
        else:
            yield await self.throttler.acquire()

    @asynccontextmanager
    async def bypass(self) -> AsyncGenerator[None, None]:
        """Bypass the throttler."""
        try:
            token = BYPASS_THROTTLER.set(True)
            yield None
        finally:
            BYPASS_THROTTLER.reset(token)


def throttle_with_retries(
    func: Callable[Concatenate[_ProviderT, _P], Awaitable[_R]],
) -> Callable[Concatenate[_ProviderT, _P], Coroutine[Any, Any, _R]]:
    """Call async function using the throttler with retries."""

    @functools.wraps(func)
    async def wrapper(self: _ProviderT, *args: _P.args, **kwargs: _P.kwargs) -> _R:
        """Call async function using the throttler with retries."""
        # the trottler attribute must be present on the class
        throttler: ThrottlerManager = self.throttler
        backoff_time = throttler.initial_backoff
        async with throttler.acquire() as delay:
            if delay != 0:
                self.logger.debug(
                    "%s was delayed for %.3f secs due to throttling", func.__name__, delay
                )
            for attempt in range(throttler.retry_attempts):
                try:
                    return await func(self, *args, **kwargs)
                except ResourceTemporarilyUnavailable as e:
                    backoff_time += e.backoff_time
                    self.logger.info(
                        f"Attempt {attempt + 1}/{throttler.retry_attempts} failed: {e}"
                    )
                    if attempt < throttler.retry_attempts - 1:
                        self.logger.info(f"Retrying in {backoff_time} seconds...")
                        await asyncio.sleep(backoff_time)
                        backoff_time *= 2
            else:  # noqa: PLW0120
                msg = f"Retries exhausted, failed after {throttler.retry_attempts} attempts"
                raise RetriesExhausted(msg)

    return wrapper
