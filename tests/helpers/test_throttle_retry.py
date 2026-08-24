"""Tests for the throttle_with_retries decorator and ThrottlerManager."""

from __future__ import annotations

import logging
from collections.abc import Generator, Sequence
from unittest.mock import AsyncMock, patch

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

    throttler = ThrottlerManager(rate_limit=100, period=0.01, retry_attempts=5, initial_backoff=4)

    def __init__(self) -> None:
        """Initialize."""
        self.logger = logging.getLogger("test.fake_provider")
        self.call_count = 0
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
        if self._side_effects:
            effect = self._side_effects.pop(0)
            if isinstance(effect, Exception):
                raise effect
        return value


@pytest.fixture
def provider() -> FakeProvider:
    """Create a FakeProvider with fast throttler for tests."""
    return FakeProvider()


@pytest.fixture
def mock_sleep() -> Generator[AsyncMock]:
    """Patch asyncio.sleep to capture sleep times without actually sleeping."""
    with patch(
        "music_assistant.helpers.throttle_retry.asyncio.sleep", new_callable=AsyncMock
    ) as mock:
        yield mock


class TestBasicBehavior:
    """Basic success/failure behavior."""

    async def test_successful_call(self, provider: FakeProvider) -> None:
        """Successful call passes through cleanly."""
        result = await provider.api_call("hello")
        assert result == "hello"
        assert provider.call_count == 1

    async def test_retries_exhausted(self, provider: FakeProvider, mock_sleep: AsyncMock) -> None:
        """Exhausting all retries raises RetriesExhausted."""
        provider.set_side_effects([ResourceTemporarilyUnavailable("fail")] * 5)
        with pytest.raises(RetriesExhausted):
            await provider.api_call("test")
        assert provider.call_count == 5

    async def test_recovery_after_failures(
        self, provider: FakeProvider, mock_sleep: AsyncMock
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
        self, provider: FakeProvider, mock_sleep: AsyncMock
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
        sleep_times = [call.args[0] for call in mock_sleep.call_args_list]
        for t in sleep_times:
            assert 2.0 <= t <= 2.2

    async def test_varying_server_backoff(
        self, provider: FakeProvider, mock_sleep: AsyncMock
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

        sleep_times = [call.args[0] for call in mock_sleep.call_args_list]
        assert 3.0 <= sleep_times[0] <= 3.3
        assert 5.0 <= sleep_times[1] <= 5.5
        assert 1.0 <= sleep_times[2] <= 1.1

    async def test_negative_backoff_falls_back_to_exponential(
        self, provider: FakeProvider, mock_sleep: AsyncMock
    ) -> None:
        """A malformed negative Retry-After must not yield a non-positive sleep."""
        provider.set_side_effects(
            [ResourceTemporarilyUnavailable("bad header", backoff_time=-5), "ok"]
        )
        await provider.api_call("ok")

        sleep_times = [call.args[0] for call in mock_sleep.call_args_list]
        assert 3.0 <= sleep_times[0] <= 5.0


class TestRateLimited:
    """When rate-limited (429), Retry-After is a floor and we escalate above it."""

    async def test_floor_is_never_undercut(
        self, provider: FakeProvider, mock_sleep: AsyncMock
    ) -> None:
        """A large Retry-After dominates until exponential backoff catches up."""
        provider.set_side_effects([RateLimited("rate limited", backoff_time=50)] * 3 + ["ok"])
        await provider.api_call("ok")

        # exp_backoff starts at 4 and doubles; 50 stays the floor for these retries
        sleep_times = [call.args[0] for call in mock_sleep.call_args_list]
        for t in sleep_times:
            assert 50.0 <= t <= 55.0

    async def test_escalates_above_floor(
        self, provider: FakeProvider, mock_sleep: AsyncMock
    ) -> None:
        """A small Retry-After is honored as a floor while exponential backoff grows."""
        provider.set_side_effects([RateLimited("rate limited", backoff_time=2)] * 4 + ["ok"])
        await provider.api_call("ok")

        sleep_times = [call.args[0] for call in mock_sleep.call_args_list]
        # exp from initial=4 dominates the 2s floor and doubles each retry (up to +10%)
        assert 4.0 <= sleep_times[0] <= 4.4
        assert 8.0 <= sleep_times[1] <= 8.8
        assert 16.0 <= sleep_times[2] <= 17.6

    async def test_absurd_retry_after_capped(
        self, provider: FakeProvider, mock_sleep: AsyncMock
    ) -> None:
        """A hostile Retry-After is clamped to MAX_RETRY_AFTER (1 hour)."""
        provider.set_side_effects([RateLimited("rate limited", backoff_time=999999), "ok"])
        await provider.api_call("ok")

        sleep_times = [call.args[0] for call in mock_sleep.call_args_list]
        assert 3600.0 <= sleep_times[0] <= 3960.0


class TestExponentialBackoffWithJitter:
    """When no server backoff is provided, use exponential backoff with jitter."""

    async def test_exponential_backoff_increases(
        self, provider: FakeProvider, mock_sleep: AsyncMock
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

        sleep_times = [call.args[0] for call in mock_sleep.call_args_list]
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

    async def test_backoff_capped_at_max(self, mock_sleep: AsyncMock) -> None:
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

        sleep_times = [call.args[0] for call in mock_sleep.call_args_list]
        # Jitter is applied before capping, so no value should exceed MAX_BACKOFF (120)
        for t in sleep_times:
            assert t <= 120.0


class TestMixedBackoff:
    """Test switching between server-provided and exponential backoff."""

    async def test_server_then_exponential(
        self, provider: FakeProvider, mock_sleep: AsyncMock
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

        sleep_times = [call.args[0] for call in mock_sleep.call_args_list]
        # Retry 1: server says 2 (honored, up to +10% jitter)
        assert 2.0 <= sleep_times[0] <= 2.2
        # Retry 2: exponential starts at initial=4 (unchanged by server retry), jitter ±25%
        assert 3.0 <= sleep_times[1] <= 5.0
        # Retry 3: doubled to 8, jitter ±25%
        assert 6.0 <= sleep_times[2] <= 10.0

    async def test_exponential_then_server(
        self, provider: FakeProvider, mock_sleep: AsyncMock
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

        sleep_times = [call.args[0] for call in mock_sleep.call_args_list]
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
