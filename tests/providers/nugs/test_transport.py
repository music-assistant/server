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

from music_assistant.providers.nugs import NugsProvider


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
