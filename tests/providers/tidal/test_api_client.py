"""Test Tidal API Client."""

from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from aiohttp import ClientResponse
from music_assistant_models.errors import (
    LoginFailed,
    MediaNotFoundError,
    ResourceTemporarilyUnavailable,
    RetriesExhausted,
)

from music_assistant.providers.tidal.api_client import MAX_PAGINATION_PAGES, TidalAPIClient
from music_assistant.providers.tidal.constants import OPEN_API_URL


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


async def test_get_jsonapi_raises_on_missing_data(
    api_client: TidalAPIClient, provider_mock: Mock
) -> None:
    """
    Test get_jsonapi raises when the response is not a valid JSON:API document.

    An empty body is surfaced by the response handler as {"success": True}; such a
    response (or any error body) has no top-level "data" and must raise rather than
    become a silently empty result.
    """
    response = AsyncMock(spec=ClientResponse)
    response.status = 200
    response.content_length = 0
    response.json.return_value = {}
    ctx = AsyncMock()
    ctx.__aenter__.return_value = response
    provider_mock.mass.http_session.request = MagicMock(return_value=ctx)

    with pytest.raises(ResourceTemporarilyUnavailable):
        await api_client.get_jsonapi("searchResults/foo")


async def test_get_jsonapi_returns_document_on_valid_response(
    api_client: TidalAPIClient, provider_mock: Mock
) -> None:
    """Test get_jsonapi returns a document when the response carries a data member."""
    response = AsyncMock(spec=ClientResponse)
    response.status = 200
    response.json.return_value = {"data": {"id": "1", "type": "tracks"}}
    ctx = AsyncMock()
    ctx.__aenter__.return_value = response
    provider_mock.mass.http_session.request = MagicMock(return_value=ctx)

    doc = await api_client.get_jsonapi("tracks/1")
    assert doc.data == {"id": "1", "type": "tracks"}


async def test_session_id_scoped_to_unofficial_api(
    api_client: TidalAPIClient, provider_mock: Mock
) -> None:
    """Test sessionId is sent to the unofficial API but not the official one."""
    response = AsyncMock(spec=ClientResponse)
    response.status = 200
    response.json.return_value = {}
    ctx = AsyncMock()
    ctx.__aenter__.return_value = response
    provider_mock.mass.http_session.request = MagicMock(return_value=ctx)

    await api_client.get("test/endpoint")
    assert "sessionId" in provider_mock.mass.http_session.request.call_args[1]["params"]

    await api_client.get("tracks", base_url=OPEN_API_URL)
    assert "sessionId" not in provider_mock.mass.http_session.request.call_args[1]["params"]
    assert "countryCode" in provider_mock.mass.http_session.request.call_args[1]["params"]


async def test_get_401_error(api_client: TidalAPIClient, provider_mock: Mock) -> None:
    """Test GET request with 401 error and a failing token refresh."""
    response = AsyncMock(spec=ClientResponse)
    response.status = 401

    request_ctx = AsyncMock()
    request_ctx.__aenter__.return_value = response
    provider_mock.mass.http_session.request = MagicMock(return_value=request_ctx)
    provider_mock.auth.refresh_token.return_value = False

    with pytest.raises(LoginFailed):
        await api_client.get("test/endpoint")

    provider_mock.auth.refresh_token.assert_called_once()


async def test_get_401_refreshes_token_and_retries(
    api_client: TidalAPIClient, provider_mock: Mock
) -> None:
    """Test that a 401 response forces a token refresh and retries the request once."""
    response_401 = AsyncMock(spec=ClientResponse)
    response_401.status = 401

    response_ok = AsyncMock(spec=ClientResponse)
    response_ok.status = 200
    response_ok.json.return_value = {"data": "test"}

    ctx1 = AsyncMock()
    ctx1.__aenter__.return_value = response_401
    ctx2 = AsyncMock()
    ctx2.__aenter__.return_value = response_ok
    provider_mock.mass.http_session.request = MagicMock(side_effect=[ctx1, ctx2])
    provider_mock.auth.refresh_token.return_value = True

    result = await api_client.get("test/endpoint")

    assert result == {"data": "test"}
    provider_mock.auth.refresh_token.assert_called_once()
    assert provider_mock.mass.http_session.request.call_count == 2


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
    response = AsyncMock(spec=ClientResponse)
    response.status = 429
    response.headers = {"Retry-After": "10"}

    request_ctx = AsyncMock()
    request_ctx.__aenter__.return_value = response
    provider_mock.mass.http_session.request = MagicMock(return_value=request_ctx)

    with pytest.raises(RetriesExhausted):
        await api_client.get("test/endpoint")


async def test_write_jsonapi(api_client: TidalAPIClient, provider_mock: Mock) -> None:
    """Test write_jsonapi sends the JSON:API content type and serialized body."""
    response = AsyncMock(spec=ClientResponse)
    response.status = 204
    ctx = AsyncMock()
    ctx.__aenter__.return_value = response
    provider_mock.mass.http_session.request = MagicMock(return_value=ctx)

    await api_client.write_jsonapi(
        "POST",
        "userCollectionTracks/me/relationships/items",
        {"data": [{"type": "tracks", "id": "1"}]},
    )

    call = provider_mock.mass.http_session.request.call_args
    assert call[0][0] == "POST"
    assert call[1]["headers"]["Content-Type"] == "application/vnd.api+json"
    # the body is sent as a serialized JSON string, not aiohttp's json= kwarg
    assert '"type": "tracks"' in call[1]["data"]
    # a per-request Idempotency-Key is sent so a throttler-driven retry dedups server-side
    assert call[1]["headers"].get("Idempotency-Key")


async def test_paginate_jsonapi_follows_cursor(
    api_client: TidalAPIClient, provider_mock: Mock
) -> None:
    """Test paginate_jsonapi follows links.next and stops when the cursor runs out."""
    page1 = AsyncMock(spec=ClientResponse)
    page1.status = 200
    page1.json.return_value = {
        "data": [{"type": "tracks", "id": "1"}],
        "links": {"next": "/x?countryCode=AT&page[cursor]=NEXT%3D123&other=1"},
    }
    page2 = AsyncMock(spec=ClientResponse)
    page2.status = 200
    page2.json.return_value = {"data": [{"type": "tracks", "id": "2"}]}

    ctx1 = AsyncMock()
    ctx1.__aenter__.return_value = page1
    ctx2 = AsyncMock()
    ctx2.__aenter__.return_value = page2
    provider_mock.mass.http_session.request = MagicMock(side_effect=[ctx1, ctx2])

    docs = [doc async for doc in api_client.paginate_jsonapi("tracks")]

    assert len(docs) == 2
    assert [d.data_list[0]["id"] for d in docs] == ["1", "2"]
    assert provider_mock.mass.http_session.request.call_count == 2
    # the second request carried the (url-decoded) cursor from page 1's next link
    second_params = provider_mock.mass.http_session.request.call_args_list[1][1]["params"]
    assert second_params["page[cursor]"] == "NEXT=123"


async def test_paginate_jsonapi_caps_pages(api_client: TidalAPIClient, provider_mock: Mock) -> None:
    """Test paginate_jsonapi stops at max_pages and warns when more pages remain."""
    response = AsyncMock(spec=ClientResponse)
    response.status = 200
    # every page advertises a next cursor, so the cap is what stops iteration
    response.json.return_value = {"data": [], "links": {"next": "/x?page[cursor]=C"}}
    ctx = AsyncMock()
    ctx.__aenter__.return_value = response
    provider_mock.mass.http_session.request = MagicMock(return_value=ctx)

    docs = [doc async for doc in api_client.paginate_jsonapi("x", max_pages=2)]

    assert len(docs) == 2
    provider_mock.logger.warning.assert_called_once()


def test_pagination_ceiling_covers_large_libraries() -> None:
    """Test the default page cap is high enough to walk a full library without truncating."""
    # The endpoints expose no page-size control, so this cap is the only guard
    # against truncating real collections; it must stay generous.
    assert MAX_PAGINATION_PAGES >= 1000
