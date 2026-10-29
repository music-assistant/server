"""Tests for throttling every request the Pandora provider sends."""

from __future__ import annotations

import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, Self, cast
from unittest.mock import AsyncMock, Mock

from music_assistant.helpers.throttle_retry import ThrottlerManager
from music_assistant.providers.pandora import provider as provider_module
from music_assistant.providers.pandora.constants import (
    API_RATE_LIMIT,
    API_RATE_PERIOD,
    PLAYBACK_RESUMED_ENDPOINT,
    STATIONS_ENDPOINT,
)
from music_assistant.providers.pandora.provider import PandoraProvider

from .test_provider import _provider

if TYPE_CHECKING:
    import pytest


class _CountingThrottler(ThrottlerManager):
    """A throttler that records how many slots it granted."""

    def __init__(self, rate_limit: int = 100, period: float = 1) -> None:
        super().__init__(rate_limit=rate_limit, period=period)
        self.grants = 0

    @asynccontextmanager
    async def acquire(self, honored_until: float = 0.0) -> AsyncGenerator[float]:
        async with super().acquire(honored_until) as delay:
            self.grants += 1
            yield delay


class _Response:
    """Stand-in for an aiohttp response, used as its own context manager."""

    def __init__(self, status: int, payload: dict[str, Any]) -> None:
        self.status = status
        self._payload = payload

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def json(self) -> dict[str, Any]:
        return self._payload


class _Session:
    """Stand-in for the aiohttp session that answers each request with the next response."""

    def __init__(self, responses: list[_Response]) -> None:
        self._responses = responses
        self.urls: list[str] = []

    def request(self, method: str, url: str, **kwargs: Any) -> _Response:
        self.urls.append(url)
        return self._responses.pop(0)

    def post(self, url: str, **kwargs: Any) -> _Response:
        return self.request("POST", url, **kwargs)


def _real_provider(responses: list[_Response], throttler: ThrottlerManager) -> PandoraProvider:
    """Build a logged-in provider whose real `_api_request` talks to a canned session."""
    provider = _provider()
    # drop the fixture's stub so the real request path runs
    del provider._api_request
    provider._csrf_token = "csrf"
    provider._auth_token = "auth"
    provider.http_session = _Session(responses)  # type: ignore[assignment]
    provider.throttler = throttler
    return provider


def test_rate_covers_a_request_with_its_recovery_and_retry() -> None:
    """A request answered 401, the CSRF fetch and login after it, and the retry all go at once."""
    assert API_RATE_LIMIT >= 4


async def test_every_api_request_takes_a_slot() -> None:
    """Each call to Pandora goes through the throttler."""
    throttler = _CountingThrottler()
    provider = _real_provider([_Response(200, {}), _Response(200, {})], throttler)
    await provider._api_request("POST", STATIONS_ENDPOINT, data={})
    await provider._api_request("POST", STATIONS_ENDPOINT, data={})
    assert throttler.grants == 2


async def test_a_burst_is_spread_over_the_rate_limit() -> None:
    """Back-to-back requests past the limit wait for a free slot instead of going out at once."""
    period = 0.05
    provider = _real_provider(
        [_Response(200, {}) for _ in range(3)], ThrottlerManager(rate_limit=1, period=period)
    )
    start = time.monotonic()
    for _ in range(3):
        await provider._api_request("POST", STATIONS_ENDPOINT, data={})
    assert time.monotonic() - start >= 2 * period


async def test_stream_takeover_and_retry_each_take_a_slot() -> None:
    """The takeover a STREAM_VIOLATION triggers, and the retry after it, are throttled too."""
    throttler = _CountingThrottler()
    provider = _real_provider(
        [
            _Response(429, {"errorString": "STREAM_VIOLATION"}),
            _Response(200, {}),  # playbackResumed
            _Response(200, {"ok": True}),  # the retried request
        ],
        throttler,
    )
    session = cast("_Session", provider.http_session)
    result = await provider._api_request("POST", STATIONS_ENDPOINT, data={})
    assert result == {"ok": True}
    assert session.urls == [STATIONS_ENDPOINT, PLAYBACK_RESUMED_ENDPOINT, STATIONS_ENDPOINT]
    assert throttler.grants == 3


async def test_login_takes_a_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    """The login and the CSRF fetch before it are requests to Pandora like any other."""
    throttler = _CountingThrottler()
    provider = _real_provider([_Response(200, {"authToken": "token"})], throttler)
    monkeypatch.setattr(provider_module, "get_csrf_token", AsyncMock(return_value="csrf"))
    await provider._authenticate("user", "secret")
    assert throttler.grants == 2
    assert provider._auth_token == "token"


async def test_a_relogin_and_retry_go_out_without_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    """At the provider's rate, the longest recovery (401, CSRF, login, retry) is not held back."""
    throttler = _CountingThrottler(rate_limit=API_RATE_LIMIT, period=API_RATE_PERIOD)
    provider = _real_provider(
        [
            _Response(401, {}),
            _Response(200, {"authToken": "new"}),  # the login
            _Response(200, {"ok": True}),  # the retried request
        ],
        throttler,
    )
    monkeypatch.setattr(provider_module, "get_csrf_token", AsyncMock(return_value="csrf"))
    monkeypatch.setattr(provider, "get_setup_value", Mock(return_value="credential"))
    start = time.monotonic()
    result = await provider._api_request("POST", STATIONS_ENDPOINT, data={})
    assert result == {"ok": True}
    assert throttler.grants == 4
    assert time.monotonic() - start < API_RATE_PERIOD / 2
