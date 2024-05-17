"""Context manager using asyncio_throttle that catches and re-raises RetriesExhausted."""

import asyncio
import functools
import logging
from collections.abc import Awaitable, Callable, Coroutine
from typing import TYPE_CHECKING, Any, Concatenate, ParamSpec, TypeVar

from asyncio_throttle import Throttler

from music_assistant.common.models.errors import ResourceTemporarilyUnavailable, RetriesExhausted
from music_assistant.constants import MASS_LOGGER_NAME

if TYPE_CHECKING:
    from music_assistant.server.models.provider import Provider

_ProviderT = TypeVar("_ProviderT", bound="Provider")
_R = TypeVar("_R")
_P = ParamSpec("_P")
LOGGER = logging.getLogger(f"{MASS_LOGGER_NAME}.throttle_retry")


class ThrottlerManager(Throttler):
    """Throttler manager that extends asyncio Throttle by retrying."""

    def __init__(self, rate_limit: int, period: float = 1, retry_attempts=5, initial_backoff=5):
        """Initialize the AsyncThrottledContextManager."""
        super().__init__(rate_limit=rate_limit, period=period, retry_interval=0.1)
        self.retry_attempts = retry_attempts
        self.initial_backoff = initial_backoff

    async def wrap(
        self,
        func: Callable[_P, Awaitable[_R]],
        *args: _P.args,
        **kwargs: _P.kwargs,
    ):
        """Async function wrapper with retry logic."""
        backoff_time = self.initial_backoff
        for attempt in range(self.retry_attempts):
            try:
                async with self:
                    return await func(self, *args, **kwargs)
            except ResourceTemporarilyUnavailable as e:
                if e.backoff_time:
                    backoff_time = e.backoff_time
                level = logging.DEBUG if attempt > 1 else logging.INFO
                LOGGER.log(level, f"Attempt {attempt + 1}/{self.retry_attempts} failed: {e}")
                if attempt < self.retry_attempts - 1:
                    LOGGER.log(level, f"Retrying in {backoff_time} seconds...")
                    await asyncio.sleep(backoff_time)
                    backoff_time *= 2
        else:  # noqa: PLW0120
            msg = f"Retries exhausted, failed after {self.retry_attempts} attempts"
            raise RetriesExhausted(msg)


def throttle_with_retries(
    func: Callable[Concatenate[_ProviderT, _P], Awaitable[_R]],
) -> Callable[Concatenate[_ProviderT, _P], Coroutine[Any, Any, _R | None]]:
    """Call async function using the throttler with retries."""

    @functools.wraps(func)
    async def wrapper(self: _ProviderT, *args: _P.args, **kwargs: _P.kwargs) -> _R | None:
        """Call async function using the throttler with retries."""
        # the trottler attribute must be present on the class
        throttler = self.throttler
        backoff_time = throttler.initial_backoff
        async with throttler:
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
