"""Test Tidal API Client."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from aiohttp import ClientResponse
from music_assistant_models.errors import (
    LoginFailed,
    MediaNotFoundError,
    RetriesExhausted,
)

from music_assistant.providers.tidal.api_client import TidalAPIClient


@pytest.fixture
def provider_mock() -> Mock:
    """Return a mock provider."""
    provider = Mock()
    provider.auth = AsyncMock()
    provider.auth.ensure_valid_token.return_value = True
    provider.auth.access_token = "token"
    provider.auth.session_id = "session"
    provider.auth.country_code = "US"
    provider.mass = Mock()
    provider.mass.http_session = AsyncMock()
    provider.mass.metadata.locale = "en_US"
    provider.logger = Mock()
    return provider


@pytest.fixture
def api_client(provider_mock: Mock) -> TidalAPIClient:
    """Return a TidalAPIClient instance."""
    return TidalAPIClient(provider_mock)


async def test_get_success(api_client: TidalAPIClient, provider_mock: Mock) -> None:
    """Test successful GET request."""
    response = AsyncMock(spec=ClientResponse)
    response.status = 200
    response.json.return_value = {"data": "test"}

    # Create a mock that acts as an async context manager
    request_ctx = AsyncMock()
    request_ctx.__aenter__.return_value = response

    # The request method itself should be a MagicMock (not AsyncMock)
    # that returns the context manager
    provider_mock.mass.http_session.request = MagicMock(return_value=request_ctx)

    result = await api_client.get("test/endpoint")
    assert result == {"data": "test"}


async def test_get_401_error(api_client: TidalAPIClient, provider_mock: Mock) -> None:
    """Test GET request with 401 error."""
    response = AsyncMock(spec=ClientResponse)
    response.status = 401

    request_ctx = AsyncMock()
    request_ctx.__aenter__.return_value = response
    provider_mock.mass.http_session.request = MagicMock(return_value=request_ctx)

    with pytest.raises(LoginFailed):
        await api_client.get("test/endpoint")


async def test_get_404_error(api_client: TidalAPIClient, provider_mock: Mock) -> None:
    """Test GET request with 404 error."""
    response = AsyncMock(spec=ClientResponse)
    response.status = 404
    response.url = "http://test/endpoint"

    request_ctx = AsyncMock()
    request_ctx.__aenter__.return_value = response
    provider_mock.mass.http_session.request = MagicMock(return_value=request_ctx)

    with pytest.raises(MediaNotFoundError):
        await api_client.get("test/endpoint")


async def test_get_429_error(api_client: TidalAPIClient, provider_mock: Mock) -> None:
    """Test GET request with 429 error."""
    with patch("asyncio.sleep"):
        response = AsyncMock(spec=ClientResponse)
    response.status = 429
    response.headers = {"Retry-After": "10"}

    request_ctx = AsyncMock()
    request_ctx.__aenter__.return_value = response
    provider_mock.mass.http_session.request = MagicMock(return_value=request_ctx)

    with pytest.raises(RetriesExhausted):
        await api_client.get("test/endpoint")


async def test_post_success(api_client: TidalAPIClient, provider_mock: Mock) -> None:
    """Test successful POST request."""
    response = AsyncMock(spec=ClientResponse)
    response.status = 200
    response.json.return_value = {"success": True}

    request_ctx = AsyncMock()
    request_ctx.__aenter__.return_value = response
    provider_mock.mass.http_session.request = MagicMock(return_value=request_ctx)

    result = await api_client.post("test/endpoint", data={"key": "value"})
    assert result == {"success": True}


async def test_paginate(api_client: TidalAPIClient, provider_mock: Mock) -> None:
    """Test pagination."""
    # Mock first page response
    response1 = AsyncMock(spec=ClientResponse)
    response1.status = 200
    response1.json.return_value = {"items": [{"id": 1}, {"id": 2}], "totalNumberOfItems": 4}

    # Mock second page response
    response2 = AsyncMock(spec=ClientResponse)
    response2.status = 200
    response2.json.return_value = {"items": [{"id": 3}, {"id": 4}], "totalNumberOfItems": 4}

    # Mock empty response to stop iteration
    response3 = AsyncMock(spec=ClientResponse)
    response3.status = 200
    response3.json.return_value = {"items": []}

    ctx1 = AsyncMock()
    ctx1.__aenter__.return_value = response1

    ctx2 = AsyncMock()
    ctx2.__aenter__.return_value = response2

    ctx3 = AsyncMock()
    ctx3.__aenter__.return_value = response3

    provider_mock.mass.http_session.request = MagicMock(side_effect=[ctx1, ctx2, ctx3])

    items: list[dict[str, Any]] = []
    async for item in api_client.paginate("test/endpoint", limit=2):
        items.append(item)

    assert len(items) == 4
    assert items[0]["id"] == 1
    assert items[3]["id"] == 4


async def test_delete_success(api_client: TidalAPIClient, provider_mock: Mock) -> None:
    """Test successful DELETE request."""
    response = AsyncMock(spec=ClientResponse)
    response.status = 204

    request_ctx = AsyncMock()
    request_ctx.__aenter__.return_value = response
    provider_mock.mass.http_session.request = MagicMock(return_value=request_ctx)

    await api_client.delete("test/endpoint/123")

    # Verify DELETE was called
    provider_mock.mass.http_session.request.assert_called_once()
    call_args = provider_mock.mass.http_session.request.call_args
    assert call_args[0][0] == "DELETE"


async def test_delete_with_headers(api_client: TidalAPIClient, provider_mock: Mock) -> None:
    """Test DELETE request with custom headers."""
    response = AsyncMock(spec=ClientResponse)
    response.status = 204

    request_ctx = AsyncMock()
    request_ctx.__aenter__.return_value = response
    provider_mock.mass.http_session.request = MagicMock(return_value=request_ctx)

    await api_client.delete("test/endpoint/123", headers={"If-Match": "etag123"})

    # Verify headers were passed
    call_args = provider_mock.mass.http_session.request.call_args
    assert "If-Match" in call_args[1]["headers"]
    assert call_args[1]["headers"]["If-Match"] == "etag123"


async def test_put_success(api_client: TidalAPIClient, provider_mock: Mock) -> None:
    """Test successful PUT request."""
    response = AsyncMock(spec=ClientResponse)
    response.status = 200
    response.json.return_value = {"updated": True}

    request_ctx = AsyncMock()
    request_ctx.__aenter__.return_value = response
    provider_mock.mass.http_session.request = MagicMock(return_value=request_ctx)

    result = await api_client.put("test/endpoint", data={"key": "value"})
    assert result == {"updated": True}

    # Verify PUT was called
    call_args = provider_mock.mass.http_session.request.call_args
    assert call_args[0][0] == "PUT"


async def test_put_with_form_data(api_client: TidalAPIClient, provider_mock: Mock) -> None:
    """Test PUT request with form data."""
    response = AsyncMock(spec=ClientResponse)
    response.status = 200
    response.json.return_value = {"success": True}

    request_ctx = AsyncMock()
    request_ctx.__aenter__.return_value = response
    provider_mock.mass.http_session.request = MagicMock(return_value=request_ctx)

    result = await api_client.put("test/endpoint", data={"key": "value"}, as_form=True)
    assert result == {"success": True}

    # Verify form data was used
    call_args = provider_mock.mass.http_session.request.call_args
    assert "data" in call_args[1]
