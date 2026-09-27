"""Tests for the throttle_with_retries decorator and ThrottlerManager."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Generator, Sequence
from unittest.mock import patch

import pytest
from music_assistant_models.errors import (
    RateLimited,
    ResourceTemporarilyUnavailable,
    RetriesExhausted,
)

from music_assistant.helpers.throttle_retry import (
    ThrottlerManager,
    parse_retry_after,
    throttle_with_retries,
)


class FakeProvider:
    """
    Minimal provider stub for testing the decorator.

    The decorator requires `self.throttler` and `self.logger`.
    """

    def __init__(self) -> None:
        """Initialize."""
        # a fresh throttler per provider: a cooldown armed by one test must not leak
        self.throttler = ThrottlerManager(
            rate_limit=100, period=0.01, retry_attempts=5, initial_backoff=4
        )
        self.logger = logging.getLogger("test.fake_provider")
        self.call_count = 0
        self.on_call: Callable[[int], None] | None = None
        self._side_effects: list[Exception | str] = []

    def set_side_effects(self, effects: Sequence[Exception | str]) -> None:
        """
        Configure what happens on each call.

        :param effects: List of exceptions to raise, or "ok" to return successfully.
        """
        self._side_effects = list(effects)
        self.call_count = 0

    @throttle_with_retries
    async def api_call(self, value: str) -> str:
        """Simulate an API call."""
        self.call_count += 1
        if self.on_call:
            self.on_call(self.call_count)
        if self._side_effects:
            effect = self._side_effects.pop(0)
            if isinstance(effect, Exception):
                raise effect
        return value


@pytest.fixture
def provider() -> FakeProvider:
    """Create a FakeProvider with fast throttler for tests."""
    return FakeProvider()


class FakeClock:
    """Virtual monotonic clock, advanced by the sleeps of the code under test."""

    def __init__(self) -> None:
        """Initialize."""
        self.now = 0.0
        self.sleeps: list[float] = []

    async def sleep(self, seconds: float) -> None:
        """Advance the clock instead of waiting, recording what was slept."""
        self.sleeps.append(seconds)
        self.now += seconds

    def monotonic(self) -> float:
        """Return the current virtual time."""
        return self.now


@pytest.fixture
def fake_clock() -> Generator[FakeClock]:
    """Run the throttler on a virtual clock, so backoffs cost no wall time."""
    clock = FakeClock()
    with (
        patch("music_assistant.helpers.throttle_retry.asyncio.sleep", clock.sleep),
        patch("music_assistant.helpers.throttle_retry.time.monotonic", clock.monotonic),
    ):
        yield clock


class TestBasicBehavior:
    """Basic success/failure behavior."""

    async def test_successful_call(self, provider: FakeProvider) -> None:
        """Successful call passes through cleanly."""
        result = await provider.api_call("hello")
        assert result == "hello"
        assert provider.call_count == 1

    async def test_retries_exhausted(self, provider: FakeProvider, fake_clock: FakeClock) -> None:
        """Exhausting all retries raises RetriesExhausted."""
        provider.set_side_effects([ResourceTemporarilyUnavailable("fail")] * 5)
        with pytest.raises(RetriesExhausted):
            await provider.api_call("test")
        assert provider.call_count == 5

    async def test_recovery_after_failures(
        self, provider: FakeProvider, fake_clock: FakeClock
    ) -> None:
        """Succeeds after transient failures."""
        provider.set_side_effects(
            [
                ResourceTemporarilyUnavailable("fail"),
                ResourceTemporarilyUnavailable("fail"),
                "ok",
            ]
        )
        result = await provider.api_call("recovered")
        assert result == "recovered"
        assert provider.call_count == 3


class TestServerProvidedBackoff:
    """When the server names a recovery time (e.g. 503 Retry-After), respect it."""

    async def test_server_backoff_respected(
        self, provider: FakeProvider, fake_clock: FakeClock
    ) -> None:
        """Server-provided backoff should be honored without doubling."""
        provider.set_side_effects(
            [
                ResourceTemporarilyUnavailable("unavailable", backoff_time=2),
                ResourceTemporarilyUnavailable("unavailable", backoff_time=2),
                ResourceTemporarilyUnavailable("unavailable", backoff_time=2),
                "ok",
            ]
        )
        result = await provider.api_call("ok")
        assert result == "ok"
        assert provider.call_count == 4

        # All three retries sleep ~2s (never less, up to +10% citizen jitter), not 2→4→8
        sleep_times = fake_clock.sleeps
        for t in sleep_times:
            assert 2.0 <= t <= 2.2

    async def test_varying_server_backoff(
        self, provider: FakeProvider, fake_clock: FakeClock
    ) -> None:
        """Each retry should use the server's current value."""
        provider.set_side_effects(
            [
                ResourceTemporarilyUnavailable("unavailable", backoff_time=3),
                ResourceTemporarilyUnavailable("unavailable", backoff_time=5),
                ResourceTemporarilyUnavailable("unavailable", backoff_time=1),
                "ok",
            ]
        )
        result = await provider.api_call("ok")
        assert result == "ok"

        sleep_times = fake_clock.sleeps
        assert 3.0 <= sleep_times[0] <= 3.3
        assert 5.0 <= sleep_times[1] <= 5.5
        assert 1.0 <= sleep_times[2] <= 1.1

    async def test_negative_backoff_falls_back_to_exponential(
        self, provider: FakeProvider, fake_clock: FakeClock
    ) -> None:
        """A malformed negative Retry-After must not yield a non-positive sleep."""
        provider.set_side_effects(
            [ResourceTemporarilyUnavailable("bad header", backoff_time=-5), "ok"]
        )
        await provider.api_call("ok")

        sleep_times = fake_clock.sleeps
        assert 3.0 <= sleep_times[0] <= 5.0


class TestRateLimited:
    """When rate-limited (429), Retry-After is a floor and we escalate above it."""

    async def test_floor_is_never_undercut(
        self, provider: FakeProvider, fake_clock: FakeClock
    ) -> None:
        """A large Retry-After dominates until exponential backoff catches up."""
        provider.set_side_effects([RateLimited("rate limited", backoff_time=50)] * 3 + ["ok"])
        await provider.api_call("ok")

        # exp_backoff starts at 4 and doubles; 50 stays the floor for these retries
        sleep_times = fake_clock.sleeps
        for t in sleep_times:
            assert 50.0 <= t <= 55.0

    async def test_escalates_above_floor(
        self, provider: FakeProvider, fake_clock: FakeClock
    ) -> None:
        """A small Retry-After is honored as a floor while exponential backoff grows."""
        provider.set_side_effects([RateLimited("rate limited", backoff_time=2)] * 4 + ["ok"])
        await provider.api_call("ok")

        sleep_times = fake_clock.sleeps
        # exp from initial=4 dominates the 2s floor and doubles each retry (up to +10%)
        assert 4.0 <= sleep_times[0] <= 4.4
        assert 8.0 <= sleep_times[1] <= 8.8
        assert 16.0 <= sleep_times[2] <= 17.6

    async def test_absurd_retry_after_capped(
        self, provider: FakeProvider, fake_clock: FakeClock
    ) -> None:
        """A hostile Retry-After is clamped to MAX_RETRY_AFTER (1 hour)."""
        provider.set_side_effects([RateLimited("rate limited", backoff_time=999999), "ok"])
        await provider.api_call("ok")

        sleep_times = fake_clock.sleeps
        assert 3600.0 <= sleep_times[0] <= 3960.0


class TestSharedCooldown:
    """A rate limit covers the whole account, so it must hold back every caller."""

    async def test_cooldown_gates_new_callers(
        self, provider: FakeProvider, fake_clock: FakeClock
    ) -> None:
        """A call started during a cooldown waits it out before it reaches the api."""
        provider.throttler.set_cooldown(50)
        assert await provider.api_call("ok") == "ok"
        assert fake_clock.now == pytest.approx(50)

    async def test_exhausted_retries_keep_the_gate_closed(
        self, provider: FakeProvider, fake_clock: FakeClock
    ) -> None:
        """The 429 that exhausts our retries still holds back the callers behind us."""
        provider.set_side_effects([RateLimited("rate limited", backoff_time=200)] * 5)
        with pytest.raises(RetriesExhausted):
            await provider.api_call("test")
        exhausted_at = fake_clock.now

        provider.set_side_effects(["ok"])
        assert await provider.api_call("ok") == "ok"
        assert fake_clock.now - exhausted_at == pytest.approx(200)

    async def test_exhausted_retries_gate_without_retry_after(
        self, provider: FakeProvider, fake_clock: FakeClock
    ) -> None:
        """Without a Retry-After the gate closes on the backoff we escalated to."""
        provider.set_side_effects([RateLimited("rate limited")] * 5)
        with pytest.raises(RetriesExhausted):
            await provider.api_call("test")
        exhausted_at = fake_clock.now

        provider.set_side_effects(["ok"])
        assert await provider.api_call("ok") == "ok"
        # initial_backoff 4, doubled by each of the 4 retries
        assert fake_clock.now - exhausted_at == pytest.approx(64)

    async def test_bypassing_caller_still_backs_off(
        self, provider: FakeProvider, fake_clock: FakeClock
    ) -> None:
        """A caller that bypasses the gate waits out its own backoff before retrying."""
        provider.set_side_effects([RateLimited("rate limited", backoff_time=50), "ok"])
        async with provider.throttler.bypass():
            assert await provider.api_call("ok") == "ok"
        assert fake_clock.now >= 50

    async def test_retry_waits_out_a_cooldown_armed_while_backing_off(
        self, provider: FakeProvider, fake_clock: FakeClock
    ) -> None:
        """A retry holds when another caller arms a longer cooldown during our backoff."""

        def another_caller_is_rate_limited(call_count: int) -> None:
            if call_count == 1:
                provider.throttler.set_cooldown(60)

        provider.on_call = another_caller_is_rate_limited
        provider.set_side_effects([RateLimited("rate limited", backoff_time=5), "ok"])
        assert await provider.api_call("ok") == "ok"
        # our own short backoff must not jump the longer cooldown of the other caller
        assert fake_clock.now == pytest.approx(60)

    async def test_cooldown_armed_while_queued_is_observed(self) -> None:
        """A caller queued for a free slot rechecks the gate before it calls the api."""
        throttler = ThrottlerManager(rate_limit=1, period=0.2)
        async with throttler.acquire():
            pass  # the only slot of this period is now taken

        async def arm_cooldown() -> None:
            await asyncio.sleep(0.05)
            throttler.set_cooldown(0.3)

        armer = asyncio.create_task(arm_cooldown())
        start_time = time.monotonic()
        async with throttler.acquire():
            elapsed = time.monotonic() - start_time
        await armer
        assert elapsed >= 0.3


class TestExponentialBackoffWithJitter:
    """When no server backoff is provided, use exponential backoff with jitter."""

    async def test_exponential_backoff_increases(
        self, provider: FakeProvider, fake_clock: FakeClock
    ) -> None:
        """Backoff should roughly double each retry (with jitter)."""
        provider.set_side_effects(
            [
                ResourceTemporarilyUnavailable("error"),
                ResourceTemporarilyUnavailable("error"),
                ResourceTemporarilyUnavailable("error"),
                ResourceTemporarilyUnavailable("error"),
                "ok",
            ]
        )
        result = await provider.api_call("ok")
        assert result == "ok"

        sleep_times = fake_clock.sleeps
        assert len(sleep_times) == 4

        # With initial_backoff=4 and jitter ±25%:
        # Attempt 0: base=4, range [3.0, 5.0]
        # Attempt 1: base=8, range [6.0, 10.0]
        # Attempt 2: base=16, range [12.0, 20.0]
        # Attempt 3: base=32, range [24.0, 40.0]
        assert 3.0 <= sleep_times[0] <= 5.0
        assert 6.0 <= sleep_times[1] <= 10.0
        assert 12.0 <= sleep_times[2] <= 20.0
        assert 24.0 <= sleep_times[3] <= 40.0

    async def test_backoff_capped_at_max(self, fake_clock: FakeClock) -> None:
        """Exponential backoff should not exceed MAX_BACKOFF (120s)."""
        provider = FakeProvider()
        # Override with a very high initial_backoff
        provider.throttler = ThrottlerManager(
            rate_limit=100, period=0.01, retry_attempts=5, initial_backoff=100
        )
        provider.set_side_effects(
            [
                ResourceTemporarilyUnavailable("error"),
                ResourceTemporarilyUnavailable("error"),
                ResourceTemporarilyUnavailable("error"),
                ResourceTemporarilyUnavailable("error"),
                "ok",
            ]
        )
        await provider.api_call("ok")

        sleep_times = fake_clock.sleeps
        # Jitter is applied before capping, so no value should exceed MAX_BACKOFF (120)
        for t in sleep_times:
            assert t <= 120.0


class TestMixedBackoff:
    """Test switching between server-provided and exponential backoff."""

    async def test_server_then_exponential(
        self, provider: FakeProvider, fake_clock: FakeClock
    ) -> None:
        """After server-provided backoff, exponential continues from its own counter."""
        provider.set_side_effects(
            [
                # First: server says wait 2s
                ResourceTemporarilyUnavailable("rate limited", backoff_time=2),
                # Then: no server guidance, fall back to exponential
                ResourceTemporarilyUnavailable("error"),
                ResourceTemporarilyUnavailable("error"),
                "ok",
            ]
        )
        result = await provider.api_call("ok")
        assert result == "ok"

        sleep_times = fake_clock.sleeps
        # Retry 1: server says 2 (honored, up to +10% jitter)
        assert 2.0 <= sleep_times[0] <= 2.2
        # Retry 2: exponential starts at initial=4 (unchanged by server retry), jitter ±25%
        assert 3.0 <= sleep_times[1] <= 5.0
        # Retry 3: doubled to 8, jitter ±25%
        assert 6.0 <= sleep_times[2] <= 10.0

    async def test_exponential_then_server(
        self, provider: FakeProvider, fake_clock: FakeClock
    ) -> None:
        """Server-provided backoff overrides even after exponential was growing."""
        provider.set_side_effects(
            [
                ResourceTemporarilyUnavailable("error"),  # no server guidance
                ResourceTemporarilyUnavailable("error"),  # no server guidance
                ResourceTemporarilyUnavailable("unavailable", backoff_time=1),  # server says 1
                "ok",
            ]
        )
        result = await provider.api_call("ok")
        assert result == "ok"

        sleep_times = fake_clock.sleeps
        # Retries 1-2: exponential (4 jittered, 8 jittered)
        assert 3.0 <= sleep_times[0] <= 5.0
        assert 6.0 <= sleep_times[1] <= 10.0
        # Retry 3: server says 1, honored (up to +10% jitter)
        assert 1.0 <= sleep_times[2] <= 1.1


class TestParseRetryAfter:
    """Tests for RFC 9110 Retry-After header parsing."""

    def test_none_returns_zero(self) -> None:
        """Missing header returns 0."""
        assert parse_retry_after(None) == 0

    def test_integer_string(self) -> None:
        """Standard delay-seconds format."""
        assert parse_retry_after("120") == 120
        assert parse_retry_after("0") == 0
        assert parse_retry_after("1") == 1

    def test_negative_clamped_to_zero(self) -> None:
        """Negative values (non-conforming) are clamped to 0."""
        assert parse_retry_after("-5") == 0

    def test_http_date(self) -> None:
        """RFC 9110 HTTP-date format returns seconds until that time."""
        import datetime  # noqa: PLC0415

        # Create a date 60 seconds in the future
        future = datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(seconds=60)
        date_str = future.strftime("%a, %d %b %Y %H:%M:%S GMT")
        result = parse_retry_after(date_str)
        # Allow ±2 seconds tolerance for test execution time
        assert 58 <= result <= 62

    def test_http_date_in_past(self) -> None:
        """HTTP-date in the past returns 0 (clamped)."""
        assert parse_retry_after("Mon, 01 Jan 2024 00:00:00 GMT") == 0

    def test_garbage_returns_zero(self) -> None:
        """Unparsable values return 0."""
        assert parse_retry_after("not-a-number") == 0
        assert parse_retry_after("") == 0
        assert parse_retry_after("1.5") == 0  # floats are not valid per RFC 9110
