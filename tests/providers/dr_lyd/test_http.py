"""Tests for how the DR LYD provider reads the radio API of DR."""

from __future__ import annotations

import json
from http import HTTPStatus
from types import TracebackType
from typing import Any, Self
from unittest.mock import MagicMock

import aiohttp
import pytest
from multidict import CIMultiDict
from music_assistant_models.errors import (
    MediaNotFoundError,
    ProviderUnavailableError,
    RateLimited,
)
from yarl import URL

from music_assistant.helpers.throttle_retry import Throttler
from music_assistant.providers.dr_lyd.constants import (
    API_HEADERS,
    API_RATE_LIMIT,
    API_RATE_PERIOD,
    API_TIMEOUT,
)
from music_assistant.providers.dr_lyd.provider import DRLydProvider

from .conftest import CHANNELS_URL, make_provider


class FakeResponse:
    """Stands in for the response of the radio API of DR."""

    def __init__(
        self,
        status: int = HTTPStatus.OK,
        document: Any = None,
        headers: dict[str, str] | None = None,
        body: str | None = None,
    ) -> None:
        """Initialize with the status, the document or the body, and the headers."""
        self.status = status
        self.headers: CIMultiDict[str] = CIMultiDict(headers or {})
        self._body = json.dumps(document) if body is None else body

    async def __aenter__(self) -> Self:
        """Enter the response."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Leave the response."""

    def raise_for_status(self) -> None:
        """Raise for a status that is an error."""
        if self.status >= HTTPStatus.BAD_REQUEST:
            raise aiohttp.ClientResponseError(
                MagicMock(real_url=URL(CHANNELS_URL)), (), status=self.status, message="error"
            )

    async def json(self) -> Any:
        """Return the body as JSON."""
        return json.loads(self._body)


class CountingThrottler:
    """Counts the requests that were let through."""

    def __init__(self) -> None:
        """Initialize without requests."""
        self.acquired = 0

    async def __aenter__(self) -> float:
        """Let a request through."""
        self.acquired += 1
        return 0.0

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Nothing to do."""


def http_provider(response: Any = None, error: Exception | None = None) -> DRLydProvider:
    """Return a provider whose session answers with the response, or raises the error."""
    provider = make_provider()
    mass: Any = provider.mass
    mass.http_session.get = MagicMock(return_value=response, side_effect=error)
    return provider


async def test_the_document_is_returned() -> None:
    """The JSON at the url is the document."""
    provider = http_provider(FakeResponse(document=[{"slug": "p3"}]))
    assert await provider._get_json(CHANNELS_URL) == [{"slug": "p3"}]


async def test_the_request_carries_the_key_of_the_web_app_and_a_timeout() -> None:
    """The requests ask for JSON with the key that the web app of DR sends."""
    provider = http_provider(FakeResponse(document={}))
    await provider._get_json(CHANNELS_URL)
    provider.mass.http_session.get.assert_called_once_with(  # type: ignore[attr-defined]
        CHANNELS_URL, headers=API_HEADERS, timeout=API_TIMEOUT
    )
    assert API_HEADERS["Accept"] == "application/json"
    assert API_HEADERS["x-apikey"]


async def test_a_missing_document_is_not_found() -> None:
    """DR answers 404 for what it does not know."""
    provider = http_provider(FakeResponse(HTTPStatus.NOT_FOUND))
    with pytest.raises(MediaNotFoundError, match="nothing at"):
        await provider._get_json(CHANNELS_URL)


@pytest.mark.parametrize(("headers", "backoff"), [({"Retry-After": "7"}, 7), ({}, 0)])
async def test_a_rate_limit_is_reported_with_the_time_to_wait(
    headers: dict[str, str], backoff: int
) -> None:
    """A 429 is a rate limit, which says how long DR asks to wait."""
    provider = http_provider(FakeResponse(HTTPStatus.TOO_MANY_REQUESTS, headers=headers))
    with pytest.raises(RateLimited) as error:
        await provider._get_json(CHANNELS_URL)
    assert error.value.backoff_time == backoff


@pytest.mark.parametrize(
    "status", [HTTPStatus.FORBIDDEN, HTTPStatus.INTERNAL_SERVER_ERROR, HTTPStatus.BAD_GATEWAY]
)
async def test_other_errors_of_dr_make_the_provider_unavailable(status: HTTPStatus) -> None:
    """A status that is an error means DR cannot be used now, which names the url."""
    provider = http_provider(FakeResponse(status))
    with pytest.raises(ProviderUnavailableError, match=CHANNELS_URL):
        await provider._get_json(CHANNELS_URL)


@pytest.mark.parametrize(
    ("error", "text"),
    [
        (aiohttp.ClientConnectionError("Cannot connect"), "Cannot connect"),
        (TimeoutError(), "TimeoutError"),
    ],
)
async def test_a_connection_that_fails_makes_the_provider_unavailable(
    error: Exception, text: str
) -> None:
    """No connection, or a timeout, means DR cannot be used now."""
    provider = http_provider(error=error)
    with pytest.raises(ProviderUnavailableError, match=text):
        await provider._get_json(CHANNELS_URL)


async def test_an_answer_that_is_no_json_makes_the_provider_unavailable() -> None:
    """A body that is not JSON means DR does not work as it should."""
    provider = http_provider(FakeResponse(body="<html>maintenance</html>"))
    with pytest.raises(ProviderUnavailableError, match=CHANNELS_URL):
        await provider._get_json(CHANNELS_URL)


async def test_every_request_goes_through_the_throttler() -> None:
    """A request takes a slot of the throttler, also one that fails."""
    provider = http_provider(FakeResponse(document={}))
    throttler = CountingThrottler()
    provider._throttler = throttler  # type: ignore[assignment]
    await provider._get_json(CHANNELS_URL)
    await provider._get_json(CHANNELS_URL)
    provider.mass.http_session.get.return_value = FakeResponse(HTTPStatus.NOT_FOUND)  # type: ignore[attr-defined]
    with pytest.raises(MediaNotFoundError):
        await provider._get_json(CHANNELS_URL)
    assert throttler.acquired == 3


async def test_the_throttler_is_set_up_with_the_rate_of_the_provider() -> None:
    """The provider is a polite client of the API of DR."""
    provider = make_provider()
    await provider.handle_async_init()
    assert isinstance(provider._throttler, Throttler)
    assert (provider._throttler.rate_limit, provider._throttler.period) == (
        API_RATE_LIMIT,
        API_RATE_PERIOD,
    )
