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
    MAX_RETRY_AFTER,
    MAX_WAIT_TIME,
    REQUEST_PRIORITY,
    RequestPriority,
    Throttler,
    ThrottlerManager,
    current_priority,
    parse_retry_after,
    request_priority,
    set_request_priority,
    throttle_with_retries,
    with_request_priority,
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


async def _acquire_in_order(
    throttler: Throttler, priorities: Sequence[RequestPriority]
) -> tuple[list[int], list[asyncio.Task[float]]]:
    """
    Queue one waiter per priority, in the given order of arrival.

    Returns the list that collects the index of each waiter once it has its slot,
    together with the waiter tasks.

    :param throttler: The throttler to queue the waiters on.
    :param priorities: The priority of each waiter, in order of arrival.
    """
    served: list[int] = []

    async def waiter(index: int, priority: RequestPriority) -> float:
        delay = await throttler.acquire(priority)
        served.append(index)
        return delay

    tasks: list[asyncio.Task[float]] = []
    for index, priority in enumerate(priorities):
        tasks.append(asyncio.create_task(waiter(index, priority)))
        # let the waiter take its place in line before the next one arrives
        await asyncio.sleep(0)
    return served, tasks


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
        with pytest.raises(RetriesExhausted) as exc_info:
            await provider.api_call("test")
        assert provider.call_count == 5
        # the last failure is kept, so a caller can tell a rate limit from another error
        assert isinstance(exc_info.value.__cause__, ResourceTemporarilyUnavailable)
        assert not isinstance(exc_info.value.__cause__, RateLimited)

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
        """A hostile Retry-After is clamped to MAX_RETRY_AFTER (1 day)."""
        provider.set_side_effects([RateLimited("rate limited", backoff_time=999999), "ok"])
        with pytest.raises(RetriesExhausted):
            await provider.api_call("ok")

        assert provider.throttler.cooldown_remaining == MAX_RETRY_AFTER


class TestSharedCooldown:
    """A rate limit covers the whole account, so it must hold back every caller."""

    def test_cooldown_remaining_reports_an_armed_cooldown(
        self, provider: FakeProvider, fake_clock: FakeClock
    ) -> None:
        """The remaining cooldown is 0 when clear and counts down once one is armed."""
        assert provider.throttler.cooldown_remaining == 0
        provider.throttler.set_cooldown(50)
        assert provider.throttler.cooldown_remaining == 50
        fake_clock.now += 20
        assert provider.throttler.cooldown_remaining == 30
        fake_clock.now += 40
        assert provider.throttler.cooldown_remaining == 0

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
        # a small initial backoff keeps the escalated backoff below the Retry-After
        provider.throttler = ThrottlerManager(
            rate_limit=100, period=0.01, retry_attempts=5, initial_backoff=2
        )
        provider.set_side_effects([RateLimited("rate limited", backoff_time=50)] * 5)
        with pytest.raises(RetriesExhausted):
            await provider.api_call("test")
        exhausted_at = fake_clock.now

        provider.set_side_effects(["ok"])
        assert await provider.api_call("ok") == "ok"
        assert fake_clock.now - exhausted_at == pytest.approx(50)

    async def test_exhausted_retries_gate_without_retry_after(
        self, provider: FakeProvider, fake_clock: FakeClock
    ) -> None:
        """Without a Retry-After the gate closes on the backoff we escalated to."""
        provider.throttler = ThrottlerManager(
            rate_limit=100, period=0.01, retry_attempts=5, initial_backoff=2
        )
        provider.set_side_effects([RateLimited("rate limited")] * 5)
        with pytest.raises(RetriesExhausted):
            await provider.api_call("test")
        exhausted_at = fake_clock.now

        provider.set_side_effects(["ok"])
        assert await provider.api_call("ok") == "ok"
        # initial_backoff 2, doubled by each of the 4 retries
        assert fake_clock.now - exhausted_at == pytest.approx(32)

    async def test_playback_caller_still_backs_off(
        self, provider: FakeProvider, fake_clock: FakeClock
    ) -> None:
        """A playback caller waits out its own backoff before retrying."""
        provider.set_side_effects([RateLimited("rate limited", backoff_time=50), "ok"])
        with request_priority(RequestPriority.HIGH):
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


class TestLongWaits:
    """A call asked by the server to wait longer than MAX_WAIT_TIME fails instead."""

    async def test_long_retry_after_fails_right_away(
        self, provider: FakeProvider, fake_clock: FakeClock
    ) -> None:
        """A 429 asking for more than MAX_WAIT_TIME fails the call without waiting."""
        provider.set_side_effects([RateLimited("rate limited", backoff_time=3600), "ok"])
        with pytest.raises(RetriesExhausted) as exc_info:
            await provider.api_call("test")
        assert provider.call_count == 1
        assert fake_clock.now == 0
        assert fake_clock.sleeps == []
        assert exc_info.value.translation_key == "rate_limited"
        assert isinstance(exc_info.value.__cause__, RateLimited)

    @pytest.mark.parametrize(
        ("backoff_time", "expected"),
        [(3600, 3600), (7200, 7200), (999999, MAX_RETRY_AFTER)],
    )
    async def test_long_retry_after_closes_the_gate_for_the_full_time(
        self, provider: FakeProvider, fake_clock: FakeClock, backoff_time: int, expected: int
    ) -> None:
        """The gate stays closed for the full time the server asked, up to MAX_RETRY_AFTER."""
        provider.set_side_effects([RateLimited("rate limited", backoff_time=backoff_time)])
        with pytest.raises(RetriesExhausted):
            await provider.api_call("test")
        assert provider.throttler.cooldown_remaining == pytest.approx(expected)

    async def test_call_during_a_long_cooldown_fails_without_reaching_the_api(
        self, provider: FakeProvider, fake_clock: FakeClock
    ) -> None:
        """A call made during a long cooldown fails right away and leaves the cooldown as is."""
        provider.set_side_effects([RateLimited("rate limited", backoff_time=3600)])
        with pytest.raises(RetriesExhausted):
            await provider.api_call("test")
        fake_clock.now += 100

        provider.set_side_effects(["ok"])
        with pytest.raises(RetriesExhausted) as exc_info:
            await provider.api_call("ok")
        assert provider.call_count == 0
        assert fake_clock.now == 100
        assert exc_info.value.translation_key == "rate_limited"
        assert isinstance(exc_info.value.__cause__, RateLimited)
        assert provider.throttler.cooldown_remaining == pytest.approx(3500)

    async def test_call_after_a_long_cooldown_goes_through(
        self, provider: FakeProvider, fake_clock: FakeClock
    ) -> None:
        """Once the cooldown has passed a call reaches the api again."""
        provider.set_side_effects([RateLimited("rate limited", backoff_time=3600)])
        with pytest.raises(RetriesExhausted):
            await provider.api_call("test")
        fake_clock.now += 3600

        provider.set_side_effects(["ok"])
        assert await provider.api_call("ok") == "ok"
        assert provider.call_count == 1

    async def test_call_behind_exhausted_retries_fails_on_a_long_cooldown(
        self, provider: FakeProvider, fake_clock: FakeClock
    ) -> None:
        """A cooldown escalated beyond MAX_WAIT_TIME fails the callers behind it as well."""
        provider.set_side_effects([RateLimited("rate limited")] * 5)
        with pytest.raises(RetriesExhausted):
            await provider.api_call("test")
        # initial_backoff 4, doubled by each of the 4 retries
        assert provider.throttler.cooldown_remaining == pytest.approx(64)
        exhausted_at = fake_clock.now

        provider.set_side_effects(["ok"])
        with pytest.raises(RetriesExhausted):
            await provider.api_call("ok")
        assert provider.call_count == 0
        assert fake_clock.now == exhausted_at

    async def test_wait_of_max_wait_time_is_sat_out(
        self, provider: FakeProvider, fake_clock: FakeClock
    ) -> None:
        """A server wait of exactly MAX_WAIT_TIME is waited out and retried."""
        provider.set_side_effects([RateLimited("rate limited", backoff_time=MAX_WAIT_TIME), "ok"])
        assert await provider.api_call("ok") == "ok"
        assert provider.call_count == 2
        assert fake_clock.now >= MAX_WAIT_TIME

    async def test_wait_above_max_wait_time_gives_up(
        self, provider: FakeProvider, fake_clock: FakeClock
    ) -> None:
        """A server wait of one second more than MAX_WAIT_TIME gives up right away."""
        provider.set_side_effects(
            [RateLimited("rate limited", backoff_time=MAX_WAIT_TIME + 1), "ok"]
        )
        with pytest.raises(RetriesExhausted):
            await provider.api_call("ok")
        assert provider.call_count == 1
        assert fake_clock.now == 0

    async def test_cooldown_of_max_wait_time_is_waited_out(
        self, provider: FakeProvider, fake_clock: FakeClock
    ) -> None:
        """A cooldown of exactly MAX_WAIT_TIME holds a new call back instead of failing it."""
        provider.throttler.set_cooldown(MAX_WAIT_TIME)
        assert await provider.api_call("ok") == "ok"
        assert fake_clock.now == pytest.approx(MAX_WAIT_TIME)

    async def test_playback_caller_ignores_a_long_cooldown(
        self, provider: FakeProvider, fake_clock: FakeClock
    ) -> None:
        """A playback caller still reaches the api during a long cooldown."""
        provider.throttler.set_cooldown(3600)
        with request_priority(RequestPriority.HIGH):
            assert await provider.api_call("ok") == "ok"
        assert provider.call_count == 1
        assert fake_clock.now == 0

    async def test_playback_caller_gives_up_on_a_long_retry_after(
        self, provider: FakeProvider, fake_clock: FakeClock
    ) -> None:
        """A playback caller gives up right away when asked to wait too long."""
        provider.set_side_effects([RateLimited("rate limited", backoff_time=3600), "ok"])
        with request_priority(RequestPriority.HIGH), pytest.raises(RetriesExhausted):
            await provider.api_call("ok")
        assert provider.call_count == 1
        assert fake_clock.now == 0
        assert provider.throttler.cooldown_remaining == pytest.approx(3600)

    async def test_long_unavailable_wait_gives_up_without_a_cooldown(
        self, provider: FakeProvider, fake_clock: FakeClock
    ) -> None:
        """A long wait on a non rate limit error gives up but holds no other caller back."""
        provider.set_side_effects(
            [ResourceTemporarilyUnavailable("unavailable", backoff_time=300), "ok"]
        )
        with pytest.raises(RetriesExhausted) as exc_info:
            await provider.api_call("ok")
        assert provider.call_count == 1
        assert fake_clock.now == 0
        assert exc_info.value.translation_key == "resource_temporarily_unavailable"
        assert provider.throttler.cooldown_remaining == 0

    async def test_backing_off_caller_gives_up_when_a_long_cooldown_is_armed(
        self, provider: FakeProvider, fake_clock: FakeClock
    ) -> None:
        """A retry gives up when another caller arms a long cooldown during our backoff."""

        def another_caller_is_rate_limited(call_count: int) -> None:
            if call_count == 1:
                provider.throttler.set_cooldown(3600)

        provider.on_call = another_caller_is_rate_limited
        provider.set_side_effects([RateLimited("rate limited", backoff_time=5), "ok"])
        with pytest.raises(RetriesExhausted):
            await provider.api_call("ok")
        assert provider.call_count == 1
        # only our own short backoff was slept, not the long cooldown
        assert fake_clock.now < MAX_WAIT_TIME
        assert provider.throttler.cooldown_remaining == pytest.approx(3600 - fake_clock.now)


class TestSetRateLimit:
    """Changing the rate limit of a throttler keeps its cooldown."""

    def test_cooldown_survives_a_rate_limit_change(self, fake_clock: FakeClock) -> None:
        """An armed cooldown is unchanged by a new rate limit."""
        throttler = ThrottlerManager(rate_limit=1, period=2)
        throttler.set_cooldown(3600)
        throttler.set_rate_limit(rate_limit=30, period=30)
        assert throttler.cooldown_remaining == pytest.approx(3600)

    async def test_rate_limit_holds_back_a_second_call(self) -> None:
        """A second call within the period waits for a free slot."""
        throttler = ThrottlerManager(rate_limit=1, period=0.2)
        async with throttler.acquire() as delay:
            assert delay == 0
        async with throttler.acquire() as delay:
            assert delay >= 0.2

    async def test_new_rate_limit_applies(self) -> None:
        """A raised rate limit lets a second call within the period through right away."""
        throttler = ThrottlerManager(rate_limit=1, period=100)
        throttler.set_rate_limit(rate_limit=2, period=100)
        async with throttler.acquire() as delay:
            assert delay == 0
        # a call held to the previous limit would wait out the period
        async with asyncio.timeout(1), throttler.acquire() as delay:
            assert delay == 0


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


class TestRequestPriority:
    """The priority of a request follows the context it is made from."""

    def test_default_is_normal(self) -> None:
        """A request made without a priority set counts as a user action."""
        assert current_priority() is RequestPriority.NORMAL

    def test_request_priority_applies_within_the_block(self) -> None:
        """The priority applies within the block and is reset after it."""
        with request_priority(RequestPriority.LOW):
            assert current_priority() is RequestPriority.LOW
            with request_priority(RequestPriority.HIGH):
                assert current_priority() is RequestPriority.HIGH
            assert current_priority() is RequestPriority.LOW
        assert REQUEST_PRIORITY.get() is RequestPriority.NORMAL

    def test_request_priority_resets_after_an_exception(self) -> None:
        """The priority is reset when the block raises."""
        with pytest.raises(ValueError, match="boom"), request_priority(RequestPriority.HIGH):
            raise ValueError("boom")
        assert REQUEST_PRIORITY.get() is RequestPriority.NORMAL

    async def test_with_request_priority_applies_during_the_call_only(self) -> None:
        """The decorated function runs with the priority, its caller keeps its own."""

        @with_request_priority(RequestPriority.HIGH)
        async def playback_call(value: str) -> tuple[str, RequestPriority]:
            return value, current_priority()

        assert await playback_call("a") == ("a", RequestPriority.HIGH)
        assert current_priority() is RequestPriority.NORMAL

    async def test_set_request_priority_overrides_the_inherited_priority(self) -> None:
        """A task entry sets its priority, whatever it inherited from its caller."""

        async def task_entry(priority: RequestPriority) -> RequestPriority:
            set_request_priority(priority)
            return current_priority()

        assert await asyncio.create_task(task_entry(RequestPriority.HIGH)) is RequestPriority.HIGH
        with request_priority(RequestPriority.HIGH):
            low_task = asyncio.create_task(task_entry(RequestPriority.LOW))
            assert current_priority() is RequestPriority.HIGH
        assert await low_task is RequestPriority.LOW


class TestThrottlerOrder:
    """Waiters are served by priority, and by arrival within the same priority."""

    async def test_higher_priority_is_served_first(self) -> None:
        """Waiters that queued as low, normal and high are served high, normal, low."""
        throttler = Throttler(rate_limit=1, period=0.1)
        await throttler.acquire(RequestPriority.NORMAL)
        served, tasks = await _acquire_in_order(
            throttler, [RequestPriority.LOW, RequestPriority.NORMAL, RequestPriority.HIGH]
        )
        async with asyncio.timeout(5):
            await asyncio.gather(*tasks)
        assert served == [2, 1, 0]

    async def test_same_priority_is_served_in_order_of_arrival(self) -> None:
        """Waiters of the same priority are served in the order they arrived."""
        throttler = Throttler(rate_limit=1, period=0.1)
        await throttler.acquire(RequestPriority.NORMAL)
        served, tasks = await _acquire_in_order(throttler, [RequestPriority.NORMAL] * 3)
        async with asyncio.timeout(5):
            await asyncio.gather(*tasks)
        assert served == [0, 1, 2]

    async def test_later_caller_does_not_overtake_a_queued_one(self) -> None:
        """A caller queues behind a waiter of the same priority, even when a slot is free."""
        throttler = Throttler(rate_limit=1, period=0.2)
        await throttler.acquire(RequestPriority.NORMAL)
        served, tasks = await _acquire_in_order(throttler, [RequestPriority.NORMAL])
        # a slot is free now, but the first waiter is still asleep until the window moves on
        throttler.rate_limit = 2
        late_served, late_tasks = await _acquire_in_order(throttler, [RequestPriority.NORMAL])
        await asyncio.sleep(0.05)
        assert not late_tasks[0].done()
        async with asyncio.timeout(5):
            await asyncio.gather(*tasks)
            await asyncio.gather(*late_tasks)
        assert served == [0]
        assert late_served == [0]

    async def test_cancelled_waiter_does_not_block_the_next_one(self) -> None:
        """A waiter that is cancelled leaves the line and the waiter behind it is served."""
        throttler = Throttler(rate_limit=1, period=0.1)
        await throttler.acquire(RequestPriority.NORMAL)
        served, tasks = await _acquire_in_order(throttler, [RequestPriority.NORMAL] * 2)
        tasks[0].cancel()
        async with asyncio.timeout(5):
            await tasks[1]
        assert served == [1]
        assert tasks[0].cancelled()


class TestThrottlerPriorities:
    """Low priority requests use half of the rate limit and are paced, the others are not."""

    async def test_low_priority_leaves_headroom(self, fake_clock: FakeClock) -> None:
        """Low priority gets at most half of the window while normal requests still pass."""
        throttler = Throttler(rate_limit=4, period=100)
        assert await throttler.acquire(RequestPriority.LOW) == 0
        assert await throttler.acquire(RequestPriority.LOW) == pytest.approx(50)
        assert await throttler.acquire(RequestPriority.NORMAL) == 0
        assert await throttler.acquire(RequestPriority.NORMAL) == 0
        # the pacer alone would allow it at 100, the window holds it until two slots expire
        assert await throttler.acquire(RequestPriority.LOW) == pytest.approx(100)

    async def test_low_priority_yields_to_normal_requests(self, fake_clock: FakeClock) -> None:
        """Once normal requests hold half of the window, a low priority request waits."""
        throttler = Throttler(rate_limit=4, period=100)
        await throttler.acquire(RequestPriority.NORMAL)
        await throttler.acquire(RequestPriority.NORMAL)
        assert await throttler.acquire(RequestPriority.LOW) == pytest.approx(100)
        assert fake_clock.sleeps == [pytest.approx(100)]

    async def test_low_priority_is_paced(self, fake_clock: FakeClock) -> None:
        """Two low priority requests in a row are spread over the low priority share."""
        throttler = Throttler(rate_limit=30, period=30)
        assert await throttler.acquire(RequestPriority.LOW) == 0
        assert await throttler.acquire(RequestPriority.LOW) == pytest.approx(2)
        assert fake_clock.now == pytest.approx(2)

    async def test_normal_burst_is_not_paced(self, fake_clock: FakeClock) -> None:
        """A burst of normal requests up to the rate limit passes without delay."""
        throttler = Throttler(rate_limit=30, period=30)
        for _ in range(30):
            assert await throttler.acquire(RequestPriority.NORMAL) == 0
        assert fake_clock.sleeps == []

    async def test_low_priority_gets_half_of_a_rate_limit_of_one(
        self, fake_clock: FakeClock
    ) -> None:
        """At a rate limit of one, low priority takes every other period and leaves the rest."""
        throttler = Throttler(rate_limit=1, period=10)
        assert await throttler.acquire(RequestPriority.LOW) == 0
        assert await throttler.acquire(RequestPriority.LOW) == pytest.approx(20)
        # the period in between is free for a user action
        assert await throttler.acquire(RequestPriority.NORMAL) == pytest.approx(10)

    async def test_every_granted_slot_counts(self, fake_clock: FakeClock) -> None:
        """A high priority request takes a slot of the window like any other request."""
        throttler = Throttler(rate_limit=2, period=100)
        await throttler.acquire(RequestPriority.HIGH)
        assert await throttler.acquire(RequestPriority.NORMAL) == 0
        assert await throttler.acquire(RequestPriority.NORMAL) == pytest.approx(100)

    async def test_priority_defaults_to_the_current_context(self, fake_clock: FakeClock) -> None:
        """Without an explicit priority the throttler uses the priority of the context."""
        throttler = Throttler(rate_limit=30, period=30)
        with request_priority(RequestPriority.LOW):
            assert await throttler.acquire() == 0
            # the second low priority request meets the pacer
            assert await throttler.acquire() == pytest.approx(2)
        # a user action is not paced
        assert await throttler.acquire() == 0


class TestThrottlerVirtualClock:
    """A single waiting caller gets its slot after one sleep on the virtual clock."""

    async def test_normal_caller_behind_a_full_window(self, fake_clock: FakeClock) -> None:
        """A normal caller behind a full window sleeps once, until the oldest slot expires."""
        fake_clock.now = 0.7
        throttler = Throttler(rate_limit=2, period=0.1)
        await throttler.acquire(RequestPriority.NORMAL)
        await throttler.acquire(RequestPriority.NORMAL)
        assert await throttler.acquire(RequestPriority.NORMAL) == pytest.approx(0.1)
        assert fake_clock.sleeps == [pytest.approx(0.1)]

    async def test_low_caller_held_by_the_pacer(self, fake_clock: FakeClock) -> None:
        """A low priority caller held by the pacer sleeps once, for the pacing interval."""
        fake_clock.now = 0.7
        throttler = Throttler(rate_limit=30, period=0.3)
        await throttler.acquire(RequestPriority.LOW)
        assert await throttler.acquire(RequestPriority.LOW) == pytest.approx(0.02)
        assert fake_clock.sleeps == [pytest.approx(0.02)]


class TestThrottlerManagerPriority:
    """Playback takes a slot without regard to a cooldown."""

    @pytest.mark.parametrize("cooldown", [5, 3600])
    async def test_high_priority_passes_a_cooldown(
        self, fake_clock: FakeClock, cooldown: int
    ) -> None:
        """A high priority request neither waits out nor fails on an armed cooldown."""
        throttler = ThrottlerManager(rate_limit=1, period=10)
        throttler.set_cooldown(cooldown)
        with request_priority(RequestPriority.HIGH):
            async with throttler.acquire() as delay:
                assert delay == 0
        assert fake_clock.now == 0
        assert throttler.cooldown_remaining == cooldown

    async def test_high_priority_waits_for_a_free_slot(self, fake_clock: FakeClock) -> None:
        """A high priority request still waits for a free slot when the window is full."""
        throttler = ThrottlerManager(rate_limit=1, period=10)
        throttler.set_cooldown(3600)
        with request_priority(RequestPriority.HIGH):
            async with throttler.acquire():
                pass
            async with throttler.acquire() as delay:
                assert delay == pytest.approx(10)

    async def test_set_rate_limit_changes_the_low_limit(self, fake_clock: FakeClock) -> None:
        """A raised rate limit raises the share of low priority requests along with it."""
        throttler = ThrottlerManager(rate_limit=2, period=100)
        throttler.set_rate_limit(rate_limit=4, period=100)
        with request_priority(RequestPriority.LOW):
            async with throttler.acquire():
                pass
            # low limit 2 of 4: only the pacer holds the second one, not the window
            async with throttler.acquire() as delay:
                assert delay == pytest.approx(50)


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
