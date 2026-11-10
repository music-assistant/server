"""Tests for how the DR LYD provider reads the radio API of DR."""

from __future__ import annotations

import json
from http import HTTPStatus
from types import TracebackType
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from multidict import CIMultiDict
from music_assistant_models.errors import (
    MediaNotFoundError,
    ProviderUnavailableError,
    RetriesExhausted,
)
from yarl import URL

from music_assistant.helpers.throttle_retry import ThrottlerManager
from music_assistant.providers.dr_lyd.constants import (
    API_HEADERS,
    API_RATE_LIMIT,
    API_RATE_PERIOD,
    API_TIMEOUT,
)
from music_assistant.providers.dr_lyd.provider import DRLydProvider

from .conftest import CHANNELS_URL, make_provider

SLEEP = "music_assistant.helpers.throttle_retry.asyncio.sleep"


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


def http_provider(*answers: FakeResponse | Exception) -> tuple[DRLydProvider, MagicMock]:
    """Return a provider whose session gives the answers one after the other, with the session."""
    provider = make_provider()
    get = MagicMock(side_effect=list(answers))
    mass: Any = provider.mass
    mass.http_session.get = get
    return provider, get


async def test_the_document_is_returned() -> None:
    """The JSON at the url is the document."""
    provider, _ = http_provider(FakeResponse(document=[{"slug": "p3"}]))
    assert await provider._get_json(CHANNELS_URL) == [{"slug": "p3"}]


async def test_the_request_carries_the_key_of_the_web_app_and_a_timeout() -> None:
    """The requests ask for JSON with the key that the web app of DR sends."""
    provider, get = http_provider(FakeResponse(document={}))
    await provider._get_json(CHANNELS_URL)
    get.assert_called_once_with(CHANNELS_URL, headers=API_HEADERS, timeout=API_TIMEOUT)
    assert API_HEADERS["Accept"] == "application/json"
    assert API_HEADERS["x-apikey"]


async def test_a_missing_document_is_not_found() -> None:
    """DR answers 404 for what it does not know."""
    provider, _ = http_provider(FakeResponse(HTTPStatus.NOT_FOUND))
    with pytest.raises(MediaNotFoundError, match="nothing at"):
        await provider._get_json(CHANNELS_URL)


async def test_a_rate_limit_is_waited_out_and_the_request_repeated() -> None:
    """A 429 is waited out for the time DR asks, then the request is made again."""
    provider, get = http_provider(
        FakeResponse(HTTPStatus.TOO_MANY_REQUESTS, headers={"Retry-After": "7"}),
        FakeResponse(document=[{"slug": "p3"}]),
    )
    with patch(SLEEP, new_callable=AsyncMock) as sleep:
        assert await provider._get_json(CHANNELS_URL) == [{"slug": "p3"}]
    assert get.call_count == 2
    [call] = sleep.await_args_list
    assert 7 <= call.args[0] < 8


async def test_a_rate_limit_that_stays_gives_up_after_the_attempts() -> None:
    """A 429 that DR keeps answering ends in retries exhausted."""
    provider, get = http_provider(*[FakeResponse(HTTPStatus.TOO_MANY_REQUESTS)] * 5)
    with patch(SLEEP, new_callable=AsyncMock), pytest.raises(RetriesExhausted):
        await provider._get_json(CHANNELS_URL)
    assert get.call_count == provider.throttler.retry_attempts


async def test_a_long_rate_limit_is_not_waited_out_and_holds_back_later_requests() -> None:
    """A 429 that asks for minutes is not waited out, and no request is made until it is over."""
    provider, get = http_provider(
        FakeResponse(HTTPStatus.TOO_MANY_REQUESTS, headers={"Retry-After": "120"})
    )
    with patch(SLEEP, new_callable=AsyncMock) as sleep:
        with pytest.raises(RetriesExhausted):
            await provider._get_json(CHANNELS_URL)
        with pytest.raises(RetriesExhausted):
            await provider._get_json(CHANNELS_URL)
    assert get.call_count == 1
    sleep.assert_not_awaited()


@pytest.mark.parametrize(
    "status", [HTTPStatus.FORBIDDEN, HTTPStatus.INTERNAL_SERVER_ERROR, HTTPStatus.BAD_GATEWAY]
)
async def test_other_errors_of_dr_make_the_provider_unavailable(status: HTTPStatus) -> None:
    """A status that is an error means DR cannot be used now, which names the url."""
    provider, get = http_provider(FakeResponse(status))
    with pytest.raises(ProviderUnavailableError, match=CHANNELS_URL):
        await provider._get_json(CHANNELS_URL)
    assert get.call_count == 1


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
    provider, _ = http_provider(error)
    with pytest.raises(ProviderUnavailableError, match=text):
        await provider._get_json(CHANNELS_URL)


async def test_an_answer_that_is_no_json_makes_the_provider_unavailable() -> None:
    """A body that is not JSON means DR does not work as it should."""
    provider, _ = http_provider(FakeResponse(body="<html>maintenance</html>"))
    with pytest.raises(ProviderUnavailableError, match=CHANNELS_URL):
        await provider._get_json(CHANNELS_URL)


async def test_the_throttler_is_set_up_with_the_rate_of_the_provider() -> None:
    """The provider is a polite client of the API of DR."""
    provider = make_provider()
    await provider.handle_async_init()
    assert isinstance(provider.throttler, ThrottlerManager)
    assert (provider.throttler.throttler.rate_limit, provider.throttler.throttler.period) == (
        API_RATE_LIMIT,
        API_RATE_PERIOD,
    )
