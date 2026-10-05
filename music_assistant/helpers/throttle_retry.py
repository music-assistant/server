"""Context manager using asyncio_throttle that catches and re-raises RetriesExhausted."""

import asyncio
import functools
import itertools
import logging
import random
import time
from bisect import insort
from collections import deque
from collections.abc import AsyncGenerator, Awaitable, Callable, Coroutine, Generator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from enum import IntEnum
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


class RequestPriority(IntEnum):
    """Priority of a request that asks a throttler for a slot."""

    LOW = 0  # background work: library sync, metadata scans, cache refreshes
    NORMAL = 1  # a user action: browsing, searching, opening an item
    HIGH = 2  # playback


# a request made without a priority set counts as a user action
REQUEST_PRIORITY: ContextVar[RequestPriority] = ContextVar(
    "REQUEST_PRIORITY", default=RequestPriority.NORMAL
)

# share of the rate limit low priority requests may use,
# the rest is headroom for user actions and playback
LOW_PRIORITY_SHARE = 0.5

# a wait at or below this many seconds counts as no wait, guards against float rounding
_WAIT_EPSILON = 1e-9

# Cap exponential backoff to prevent absurd wait times
MAX_BACKOFF = 120

# Cap a server-provided Retry-After, in case it is absurd or hostile
MAX_RETRY_AFTER = 86400

# Longest single wait a server can ask of a call, a call asked to wait longer fails instead
MAX_WAIT_TIME = 60


def current_priority() -> RequestPriority:
    """Return the throttler priority of the current context."""
    return REQUEST_PRIORITY.get()


def set_request_priority(priority: RequestPriority) -> None:
    """
    Set the throttler priority for the rest of the current context.

    Meant for the entry point of a task, use request_priority for a block of code.

    :param priority: The priority of the requests made from this context.
    """
    REQUEST_PRIORITY.set(priority)


@contextmanager
def request_priority(priority: RequestPriority) -> Generator[None]:
    """
    Set the throttler priority for the duration of the block.

    :param priority: The priority of the requests made within the block.
    """
    token = REQUEST_PRIORITY.set(priority)
    try:
        yield
    finally:
        REQUEST_PRIORITY.reset(token)


def with_request_priority[**P, R](
    priority: RequestPriority,
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Coroutine[Any, Any, R]]]:
    """
    Run each call of the decorated async function with the given throttler priority.

    :param priority: The priority of the requests made during the call.
    """

    def decorator(func: Callable[P, Awaitable[R]]) -> Callable[P, Coroutine[Any, Any, R]]:
        @functools.wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            with request_priority(priority):
                return await func(*args, **kwargs)

        return wrapper

    return decorator


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
    Rate limiter that grants at most rate_limit slots within any period.

    Requests are served by priority and, within the same priority, in order of arrival.
    Low priority requests may use only LOW_PRIORITY_SHARE of the rate limit and are
    spread evenly over the period, so user actions and playback always find headroom.
    """

    def __init__(self, rate_limit: int, period: float = 1.0) -> None:
        """Initialize the Throttler."""
        self.rate_limit = rate_limit
        self.period = period
        self._task_logs: deque[float] = deque()
        self._last_low_grant: float | None = None
        self._waiters: list[_Waiter] = []
        self._sequence = itertools.count()

    async def acquire(self, priority: RequestPriority | None = None) -> float:
        """
        Acquire a free slot from the Throttler, returns the throttled time.

        :param priority: Priority of the request, None for the priority of the current context.
        """
        if priority is None:
            priority = current_priority()
        waiter = _Waiter(priority, next(self._sequence))
        insort(self._waiters, waiter, key=_Waiter.sort_key)
        start_time = now = time.monotonic()
        try:
            while True:
                # only the first waiter in line may take a slot, the others wait their turn
                if self._waiters[0] is not waiter:
                    waiter.turn.clear()
                    await waiter.turn.wait()
                    now = time.monotonic()
                    continue
                delay = self._time_until_free(priority, now)
                if delay <= _WAIT_EPSILON:
                    break
                await asyncio.sleep(delay)
                now = time.monotonic()
        finally:
            self._waiters.remove(waiter)
            if self._waiters:
                self._waiters[0].turn.set()
        self._task_logs.append(now)
        if priority is RequestPriority.LOW:
            self._last_low_grant = now
        return now - start_time  # exactly 0 if not throttled

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

    def _time_until_free(self, priority: RequestPriority, now: float) -> float:
        """Return the seconds until a request of the given priority may take a slot."""
        # a slot stops counting once a full period has passed since it was granted
        while self._task_logs and self._task_logs[0] + self.period - now <= _WAIT_EPSILON:
            self._task_logs.popleft()
        low_share = self.rate_limit * LOW_PRIORITY_SHARE
        limit = max(1, int(low_share)) if priority is RequestPriority.LOW else self.rate_limit
        delay = 0.0
        if (excess := len(self._task_logs) - limit) >= 0:
            delay = self._task_logs[excess] + self.period - now
        if priority is RequestPriority.LOW and self._last_low_grant is not None:
            # the pacer spreads the low priority share evenly over the period
            delay = max(delay, self._last_low_grant + self.period / low_share - now)
        return delay


class ThrottlerManager:
    """Throttler manager that extends asyncio Throttle by retrying."""

    def __init__(
        self, rate_limit: int, period: float = 1, retry_attempts: int = 5, initial_backoff: int = 5
    ):
        """Initialize the AsyncThrottledContextManager."""
        self.retry_attempts = retry_attempts
        self.initial_backoff = initial_backoff
        self.throttler = Throttler(rate_limit, period)
        self._cooldown_until: float = 0.0

    @property
    def cooldown_remaining(self) -> float:
        """Seconds a server-imposed rate limit still holds every caller back, 0 when clear."""
        return max(0.0, self._cooldown_until - time.monotonic())

    @asynccontextmanager
    async def acquire(self, honored_until: float = 0.0) -> AsyncGenerator[float]:
        """
        Acquire a free slot from the Throttler, returns the throttled time.

        :param honored_until: Monotonic deadline the caller already waited out, so a
            cooldown no later than it does not hold the caller back a second time.
        :raises RateLimited: When a server-imposed cooldown holds for longer than MAX_WAIT_TIME.
        """
        priority = current_priority()
        if priority is RequestPriority.HIGH:
            # playback neither sits out nor fails on a cooldown
            yield await self.throttler.acquire(priority)
            return
        delay = 0.0
        honored = honored_until
        while True:
            # each deadline is waited out once, however often it is extended meanwhile
            while (target := self._cooldown_until) > honored:
                if (remaining := self.cooldown_remaining) > MAX_WAIT_TIME:
                    msg = f"Rate limited for another {remaining:.0f} seconds"
                    raise RateLimited(msg, backoff_time=round(remaining))
                delay += await self._wait_until(target)
                honored = target
            delay += await self.throttler.acquire(priority)
            # a cooldown can be armed while we wait for a free slot, so only leave
            # the gate once it is still clear with the slot in hand
            if self._cooldown_until <= honored:
                break
        yield delay

    def set_cooldown(self, seconds: float) -> None:
        """
        Hold back every caller of this throttler for the given number of seconds.

        :param seconds: How long the server-imposed rate limit still applies.
        """
        self._cooldown_until = max(self._cooldown_until, time.monotonic() + seconds)

    def set_rate_limit(self, rate_limit: int, period: float = 1) -> None:
        """
        Change the rate limit of this throttler, an active cooldown stays in place.

        :param rate_limit: Number of requests allowed per period.
        :param period: Length of the period in seconds.
        """
        self.throttler.rate_limit = rate_limit
        self.throttler.period = period

    async def _wait_until(self, deadline: float) -> float:
        """Sleep until the given monotonic deadline, return the time waited."""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return 0.0
        await asyncio.sleep(remaining)
        return remaining


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
        honored_until = 0.0
        for attempt in range(throttler.retry_attempts):
            # every attempt goes through the gate: a cooldown another caller armed while
            # we were backing off must hold this retry too, and a retry is a request like
            # any other, so it takes a rate limit slot of its own
            try:
                async with throttler.acquire(honored_until) as delay:
                    if delay != 0:
                        self.logger.debug(
                            "%s was delayed for %.3f secs due to throttling", func.__name__, delay
                        )
                    try:
                        return await func(self, *args, **kwargs)
                    except ResourceTemporarilyUnavailable as e:
                        self.logger.info(
                            f"Attempt {attempt + 1}/{throttler.retry_attempts} failed: {e}"
                        )
                        server_wait = min(max(float(e.backoff_time), 0.0), MAX_RETRY_AFTER)
                        if server_wait > MAX_WAIT_TIME:
                            if isinstance(e, RateLimited):
                                throttler.set_cooldown(server_wait)
                            self.logger.warning(
                                "Not retrying %s, the server asked to wait %.0f seconds",
                                func.__name__,
                                server_wait,
                            )
                            raise _give_up(e, server_wait) from e
                        if attempt < throttler.retry_attempts - 1:
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
                                sleep_time = min(
                                    exp_backoff * random.uniform(0.75, 1.25), MAX_BACKOFF
                                )
                                exp_backoff = min(exp_backoff * 2, MAX_BACKOFF)
                            if isinstance(e, RateLimited):
                                # a rate limit applies to the whole account, so hold back every
                                # other caller for as long as we back off ourselves
                                throttler.set_cooldown(sleep_time)
                            self.logger.info(f"Retrying in {sleep_time:.1f} seconds...")
                            honored_until = time.monotonic() + sleep_time
                            await asyncio.sleep(sleep_time)
                        elif isinstance(e, RateLimited):
                            # out of retries while still limited: keep the other callers back,
                            # on the escalated backoff since Retry-After can be absent or low
                            throttler.set_cooldown(max(server_wait, min(exp_backoff, MAX_BACKOFF)))
            except RateLimited as e:
                # raised by the gate: a rate limit of the call itself is handled above
                self.logger.debug("%s skipped during a rate limit cooldown: %s", func.__name__, e)
                raise _give_up(e, e.backoff_time) from e
        msg = f"Retries exhausted, failed after {throttler.retry_attempts} attempts"
        raise RetriesExhausted(msg)

    return wrapper


def _give_up(err: ResourceTemporarilyUnavailable, wait: float) -> RetriesExhausted:
    """
    Return the error of a call that does not sit out the wait asked of it.

    :param err: The error that asked for the wait, its localization is carried over.
    :param wait: The wait that was asked for, in seconds.
    """
    return RetriesExhausted(
        f"Not retrying, asked to wait {wait:.0f} seconds",
        translation_key=err.translation_key,
        translation_args=err.translation_args,
        translation_owner=err.translation_owner,
    )


@dataclass
class _Waiter:
    """A request waiting in line for a slot of a Throttler."""

    priority: RequestPriority
    sequence: int
    turn: asyncio.Event = field(default_factory=asyncio.Event)

    def sort_key(self) -> tuple[int, int]:
        """Return the key that orders the line: highest priority first, then by arrival."""
        return (-self.priority, self.sequence)
