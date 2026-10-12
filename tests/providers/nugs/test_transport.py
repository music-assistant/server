"""Transport error handling for authenticated and legacy Nugs APIs."""

from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import ClientConnectionError, ClientPayloadError
from music_assistant_models.errors import (
    InvalidDataError,
    LoginFailed,
    MediaNotFoundError,
    RateLimited,
    ResourceTemporarilyUnavailable,
)

from music_assistant.helpers.throttle_retry import ThrottlerManager
from music_assistant.providers.nugs import NugsProvider
from tests.common import use_real_create_task


def _response(provider: NugsProvider, status: int = 200) -> Mock:
    """Set up an HTTP response without exercising throttling delays."""
    response = Mock(status=status, headers={"Retry-After": "7"})
    response.json = AsyncMock(return_value={"items": []})
    response.text = AsyncMock(return_value='{"responseAvailabilityCode":0,"Response":{}}')
    context = AsyncMock()
    context.__aenter__.return_value = response
    cast("Mock", provider.mass.http_session.get).return_value = context
    cast("Any", provider).login = AsyncMock(return_value="test-token")
    return response


async def _request(provider: NugsProvider, transport: str) -> Any:
    """Invoke the transport beneath caching and retry decorators."""
    if transport == "authenticated":
        method = cast("Any", NugsProvider._get_data).__wrapped__
        return await method(provider, "catalog", "search", query="Billy")
    method = cast("Any", NugsProvider._get_search_data).__wrapped__.__wrapped__
    return await method(provider, "Billy")


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["authenticated", "legacy"])
@pytest.mark.parametrize(
    ("status", "error"),
    [
        (400, InvalidDataError),
        (401, LoginFailed),
        (403, InvalidDataError),
        (404, MediaNotFoundError),
        (429, RateLimited),
        (500, ResourceTemporarilyUnavailable),
        (502, ResourceTemporarilyUnavailable),
        (503, ResourceTemporarilyUnavailable),
        (504, ResourceTemporarilyUnavailable),
    ],
)
async def test_http_errors(
    provider: NugsProvider, transport: str, status: int, error: type[Exception]
) -> None:
    """Both APIs expose typed failures, preserving a rate limit's retry delay."""
    response = _response(provider, status)
    with pytest.raises(error) as exc:
        await _request(provider, transport)
    if isinstance(exc.value, RateLimited):
        assert exc.value.backoff_time == 7
    response.json.assert_not_awaited()
    response.text.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["authenticated", "legacy"])
@pytest.mark.parametrize("error", [ClientConnectionError, ClientPayloadError, TimeoutError])
async def test_connection_errors(
    provider: NugsProvider, transport: str, error: type[Exception]
) -> None:
    """Interrupted requests become errors recognized by the shared retry helper."""
    _response(provider)
    cast("Mock", provider.mass.http_session.get).return_value.__aenter__.side_effect = error()
    with pytest.raises(ResourceTemporarilyUnavailable):
        await _request(provider, transport)


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["authenticated", "legacy"])
async def test_malformed_json(provider: NugsProvider, transport: str) -> None:
    """Malformed responses are permanent data errors, not retryable failures."""
    response = _response(provider)
    response.json.side_effect = ValueError("invalid json")
    response.text.return_value = "not json"
    with pytest.raises(InvalidDataError):
        await _request(provider, transport)


@pytest.mark.asyncio
async def test_authenticated_request(provider: NugsProvider) -> None:
    """Catalog requests retain authentication and filtering parameters."""
    _response(provider)
    assert await _request(provider, "authenticated") == {"items": []}
    request = cast("Mock", provider.mass.http_session.get).call_args
    assert request.args == ("https://catalog.nugs.net/api/v1/search",)
    assert request.kwargs["headers"] == {"Authorization": "Bearer test-token"}
    assert request.kwargs["params"] == {"query": "Billy"}


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["authenticated", "legacy"])
async def test_temporary_failure_retries_through_decorated_transport(
    provider: NugsProvider, transport: str
) -> None:
    """A temporary HTTP failure retries once and returns the following successful response."""
    failed = _response(provider, 500)
    failed_context = cast("Mock", provider.mass.http_session.get).return_value
    succeeded = _response(provider)
    succeeded_context = cast("Mock", provider.mass.http_session.get).return_value
    request = cast("Mock", provider.mass.http_session.get)
    request.side_effect = [failed_context, succeeded_context]
    provider.throttler = ThrottlerManager(rate_limit=1000, retry_attempts=2, initial_backoff=0)

    if transport == "authenticated":
        result = await provider._get_data("catalog", "search", query="Billy")
        assert result == {"items": []}
        succeeded.json.assert_awaited_once()
    else:
        provider.mass.cache.get_with_freshness = AsyncMock(  # type: ignore[method-assign]
            return_value=(None, False, False)
        )
        provider.mass.cache.set = AsyncMock()  # type: ignore[method-assign]
        use_real_create_task(provider.mass)
        result = await provider._get_search_data("Billy")
        assert result == {}
        succeeded.text.assert_awaited_once()
    assert request.call_count == 2
    failed.json.assert_not_awaited()
    failed.text.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_search_uses_its_own_throttler(provider: NugsProvider) -> None:
    """Legacy searches pass through the dedicated search gate as well as the shared one."""
    _response(provider)
    gate = AsyncMock()
    provider.search_throttler = Mock(acquire=Mock(return_value=gate))

    await _request(provider, "legacy")
    provider.search_throttler.acquire.assert_called_once_with()
    gate.__aenter__.assert_awaited_once()

    await _request(provider, "authenticated")
    provider.search_throttler.acquire.assert_called_once_with()


@pytest.mark.asyncio
async def test_rejected_token_is_cleared(provider: NugsProvider) -> None:
    """A 401 drops the stored token so the next request logs in again."""
    _response(provider, 401)
    provider._auth_token = "revoked"
    with pytest.raises(LoginFailed):
        await _request(provider, "authenticated")
    assert provider._auth_token is None
