"""Context manager using asyncio_throttle that catches and re-raises RetriesExhausted."""

import asyncio
import logging

from asyncio_throttle import Throttler

from music_assistant.common.models.errors import (
    ResourceTemporarilyUnavailable,
    RetriesExhausted,
)
from music_assistant.constants import MASS_LOGGER_NAME

LOGGER = logging.getLogger(f"{MASS_LOGGER_NAME}.throttle_retry")


class AsyncThrottleWithRetryContextManager:
    """Context manager using asyncio_throttle that catches and re-raises RetriesExhausted."""

    def __init__(self, rate_limit, period, retry_attempts=5, initial_backoff=5):
        """Initialize the AsyncThrottledContextManager."""
        self.rate_limit = rate_limit
        self.period = period
        self.retry_attempts = retry_attempts
        self.initial_backoff = initial_backoff
        self.throttler = Throttler(rate_limit=rate_limit, period=period)

    async def __aenter__(self):
        """Acquire the throttle when entering the async context."""
        await self.throttler.acquire()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        """Release the throttle. If a RetriesExhausted occurs, re-raise it."""
        self.throttler.flush()
        if isinstance(exc, RetriesExhausted):
            raise exc

    async def wrapped_function_with_retry(self, func, *args, **kwargs):
        """Async function wrapper with retry logic."""
        backoff_time = self.initial_backoff
        for attempt in range(self.retry_attempts):
            try:
                return await func(*args, **kwargs)
            except ResourceTemporarilyUnavailable as e:
                LOGGER.warning(f"Attempt {attempt + 1}/{self.retry_attempts} failed: {e}")
                if attempt < self.retry_attempts - 1:
                    LOGGER.warning(f"Retrying in {backoff_time} seconds...")
                    await asyncio.sleep(backoff_time)
                    backoff_time *= 2
        else:  # noqa: PLW0120
            msg = f"Retries exhausted, failed after {self.retry_attempts} attempts"
            raise RetriesExhausted(msg)
