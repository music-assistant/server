"""Context manager using asyncio_throttle that catches and re-raises RetriesExhausted."""

import asyncio
import functools
import logging
import random
import time
from collections import deque
from collections.abc import AsyncGenerator, Awaitable, Callable, Coroutine
from contextlib import asynccontextmanager
from contextvars import ContextVar
from email.utils import parsedate_to_datetime
from types import TracebackType
from typing import Any, Concatenate, Protocol

from music_assistant_models.errors import (
    RateLimited,
    ResourceTemporarilyUnavailable,
    RetriesExhausted,
)

from music_assistant.constants import MASS_LOGGER_NAME
from music_assistant.helpers.datetime import utc

LOGGER = logging.getLogger(f"{MASS_LOGGER_NAME}.throttle_retry")

BYPASS_THROTTLER: ContextVar[bool] = ContextVar("BYPASS_THROTTLER", default=False)

# Cap exponential backoff to prevent absurd wait times
MAX_BACKOFF = 120

# Cap a server-provided Retry-After, in case it is absurd or hostile
MAX_RETRY_AFTER = 3600


def parse_retry_after(value: str | None) -> int:
    """
    Parse a Retry-After header value per RFC 9110 Section 10.2.3.

    Supports both valid formats: delay-seconds (integer) and HTTP-date.

    :param value: The raw Retry-After header value, or None if absent.
    :returns: Non-negative integer seconds to wait, or 0 if unparsable/absent.
    """
    if value is None:
        return 0
    # Try delay-seconds (non-negative integer) first — the common case
    try:
        return max(0, int(value))
    except ValueError, TypeError:
        pass
    # Try HTTP-date format (e.g., "Fri, 31 Dec 1999 23:59:59 GMT")
    try:
        target = parsedate_to_datetime(value)
        delta = (target - utc()).total_seconds()
        return max(0, int(delta))
    except ValueError, TypeError:
        return 0


class Throttler:
    """
    asyncio_throttle (https://github.com/hallazzang/asyncio-throttle).

    With improvements:
    - Accurate sleep without "busy waiting" (PR #4)
    - Return the delay caused by acquire()
    """

    def __init__(self, rate_limit: int, period: float = 1.0) -> None:
        """Initialize the Throttler."""
        self.rate_limit = rate_limit
        self.period = period
        self._task_logs: deque[float] = deque()

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

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> bool | None:
        """Nothing to do on exit."""

    def _flush(self) -> None:
        now = time.monotonic()
        while self._task_logs:
            if now - self._task_logs[0] > self.period:
                self._task_logs.popleft()
            else:
                break


class ThrottlerManager:
    """Throttler manager that extends asyncio Throttle by retrying."""

    def __init__(
        self, rate_limit: int, period: float = 1, retry_attempts: int = 5, initial_backoff: int = 5
    ):
        """Initialize the AsyncThrottledContextManager."""
        self.retry_attempts = retry_attempts
        self.initial_backoff = initial_backoff
        self.throttler = Throttler(rate_limit, period)

    @asynccontextmanager
    async def acquire(self) -> AsyncGenerator[float]:
        """Acquire a free slot from the Throttler, returns the throttled time."""
        if BYPASS_THROTTLER.get():
            yield 0
        else:
            yield await self.throttler.acquire()

    @asynccontextmanager
    async def bypass(self) -> AsyncGenerator[None]:
        """Bypass the throttler."""
        try:
            token = BYPASS_THROTTLER.set(True)
            yield None
        finally:
            BYPASS_THROTTLER.reset(token)


class _Throttleable(Protocol):
    """Protocol for objects that can use the @throttle_with_retries decorator."""

    @property
    def logger(self) -> logging.Logger: ...

    @property
    def throttler(self) -> ThrottlerManager: ...


def throttle_with_retries[ProviderT: _Throttleable, **P, R](
    func: Callable[Concatenate[ProviderT, P], Awaitable[R]],
) -> Callable[Concatenate[ProviderT, P], Coroutine[Any, Any, R]]:
    """Call async function using the throttler with retries."""

    @functools.wraps(func)
    async def wrapper(self: ProviderT, *args: P.args, **kwargs: P.kwargs) -> R:
        """Call async function using the throttler with retries."""
        throttler = self.throttler
        exp_backoff = throttler.initial_backoff
        async with throttler.acquire() as delay:
            if delay != 0:
                self.logger.debug(
                    "%s was delayed for %.3f secs due to throttling", func.__name__, delay
                )
            for attempt in range(throttler.retry_attempts):
                try:
                    return await func(self, *args, **kwargs)
                except ResourceTemporarilyUnavailable as e:
                    self.logger.info(
                        f"Attempt {attempt + 1}/{throttler.retry_attempts} failed: {e}"
                    )
                    if attempt < throttler.retry_attempts - 1:
                        server_wait = min(max(float(e.backoff_time), 0.0), MAX_RETRY_AFTER)
                        if isinstance(e, RateLimited):
                            # Retry-After is a floor, not a target: escalate above it,
                            # jittering up only so we never retry sooner than asked
                            base = max(server_wait, min(exp_backoff, MAX_BACKOFF))
                            sleep_time = base * random.uniform(1.0, 1.1)
                            exp_backoff = min(exp_backoff * 2, MAX_BACKOFF)
                        elif server_wait:
                            # Server named a recovery time — respect it, with citizen jitter
                            sleep_time = server_wait * random.uniform(1.0, 1.1)
                        else:
                            # No server guidance — exponential backoff with jitter
                            sleep_time = min(exp_backoff * random.uniform(0.75, 1.25), MAX_BACKOFF)
                            exp_backoff = min(exp_backoff * 2, MAX_BACKOFF)
                        self.logger.info(f"Retrying in {sleep_time:.1f} seconds...")
                        await asyncio.sleep(sleep_time)
            else:  # noqa: PLW0120
                msg = f"Retries exhausted, failed after {throttler.retry_attempts} attempts"
                raise RetriesExhausted(msg)

    return wrapper
