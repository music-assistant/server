"""Tests for what a failing Podcast Index call reports back."""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import MagicMock, patch

import aiohttp
import pytest
from music_assistant_models.errors import (
    InvalidDataError,
    LoginFailed,
    ProviderUnavailableError,
)

from music_assistant.providers.podcast_index.constants import MAX_ERROR_DETAIL_LENGTH
from music_assistant.providers.podcast_index.helpers import make_api_request
from music_assistant.providers.podcast_index.provider import PodcastIndexProvider

API_KEY = "key"
API_SECRET = "secret"


class _FakeResponse:
    def __init__(self, status: int, body: str = "", payload: Any = None) -> None:
        self.status = status
        self._body = body
        self._payload = payload

    async def text(self) -> str:
        return self._body

    async def json(self) -> Any:
        if self._payload is None:
            raise aiohttp.ContentTypeError(MagicMock(), ())
        return self._payload


class _FakeRequestContext:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response

    async def __aenter__(self) -> _FakeResponse:
        return self._response

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


def _mass(response: _FakeResponse) -> MagicMock:
    """Return a Music Assistant stub whose http session answers with the given response."""
    mass = MagicMock()
    mass.http_session.get = MagicMock(return_value=_FakeRequestContext(response))
    return mass


async def _request(response: _FakeResponse, logger: logging.Logger | None = None) -> Any:
    return await make_api_request(
        _mass(response), API_KEY, API_SECRET, "stats/current", logger=logger
    )


async def test_rejected_credentials_quote_the_api() -> None:
    """A refused key reports what Podcast Index said about it, not just the status."""
    response = _FakeResponse(401, body="Invalid authorization header")

    with pytest.raises(LoginFailed, match="Invalid authorization header") as err:
        await _request(response)

    assert "401" in str(err.value)


async def test_a_failure_without_a_body_still_reports_the_status() -> None:
    """An error that says nothing must not leave a dangling separator behind."""
    response = _FakeResponse(500, body="   ")

    with pytest.raises(ProviderUnavailableError) as err:
        await _request(response)

    assert str(err.value) == "API request failed (HTTP 500)"


async def test_a_long_error_is_cut_to_a_readable_length() -> None:
    """A page of markup is quoted only as far as it stays readable."""
    response = _FakeResponse(403, body="x" * (MAX_ERROR_DETAIL_LENGTH * 2))

    with pytest.raises(ProviderUnavailableError) as err:
        await _request(response)

    assert str(err.value).endswith("...")
    assert len(str(err.value)) < MAX_ERROR_DETAIL_LENGTH * 2


async def test_a_refusal_carrying_a_reason_reports_it() -> None:
    """An answer that reports failure in its payload surfaces that description."""
    response = _FakeResponse(200, payload={"status": "false", "description": "no such feed"})

    with pytest.raises(InvalidDataError, match="no such feed"):
        await _request(response)


async def test_a_failure_is_logged_for_support(caplog: pytest.LogCaptureFixture) -> None:
    """A failing call records the endpoint, the status and the reason at debug level."""
    logger = logging.getLogger("test.podcast_index")
    response = _FakeResponse(401, body="Invalid authorization header")

    with caplog.at_level(logging.DEBUG, logger=logger.name), pytest.raises(LoginFailed):
        await _request(response, logger=logger)

    assert "stats/current" in caplog.text
    assert "401" in caplog.text
    assert "Invalid authorization header" in caplog.text


async def test_credentials_are_never_logged(caplog: pytest.LogCaptureFixture) -> None:
    """The key and secret must stay out of anything a user is asked to share."""
    logger = logging.getLogger("test.podcast_index")
    # the live API names the header rather than the value, but the body is not ours to trust
    response = _FakeResponse(401, body=f"key {API_KEY} with secret {API_SECRET} was refused")

    with caplog.at_level(logging.DEBUG, logger=logger.name), pytest.raises(LoginFailed) as err:
        await _request(response, logger=logger)

    assert API_KEY not in caplog.text
    assert API_SECRET not in caplog.text
    assert API_KEY not in str(err.value)
    assert API_SECRET not in str(err.value)


async def test_a_successful_call_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    """A call that worked records that it did, so a working setup is recognisable."""
    logger = logging.getLogger("test.podcast_index")
    response = _FakeResponse(200, payload={"status": "true", "count": 3})

    with caplog.at_level(logging.DEBUG, logger=logger.name):
        data = await _request(response, logger=logger)

    assert data["count"] == 3
    assert "stats/current" in caplog.text


async def test_a_single_item_call_is_not_reported_as_empty(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Endpoints returning one item carry no count, which is not the same as returning none."""
    logger = logging.getLogger("test.podcast_index")
    response = _FakeResponse(200, payload={"status": "true", "episode": {"id": 1}})

    with caplog.at_level(logging.DEBUG, logger=logger.name):
        await _request(response, logger=logger)

    assert "no items" not in caplog.text
    assert "stats/current succeeded" in caplog.text


def _browse_provider() -> PodcastIndexProvider:
    """Create a provider whose API calls can be stubbed out."""
    provider = object.__new__(PodcastIndexProvider)
    provider.mass = MagicMock()
    provider.logger = MagicMock()
    return provider


@pytest.mark.parametrize(
    ("browse", "args"),
    [
        (PodcastIndexProvider._browse_trending, ()),
        (PodcastIndexProvider._browse_category_podcasts, ("comedy",)),
    ],
)
async def test_browsing_reports_rejected_credentials(browse: Any, args: tuple[Any, ...]) -> None:
    """A rejected key must surface, not leave the shelf looking empty."""
    provider = _browse_provider()
    with (
        patch.object(PodcastIndexProvider, "_api_request", side_effect=LoginFailed("key refused")),
        patch.object(
            PodcastIndexProvider, "_fetch_podcasts", side_effect=LoginFailed("key refused")
        ),
        pytest.raises(LoginFailed),
    ):
        await browse.__wrapped__(provider, *args)
