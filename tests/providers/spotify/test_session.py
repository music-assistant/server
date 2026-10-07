"""Tests for the request handling of a Spotify Web API session."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.errors import (
    MediaNotFoundError,
    RateLimited,
    ResourceTemporarilyUnavailable,
)

from music_assistant.helpers.throttle_retry import ThrottlerManager
from music_assistant.providers.spotify.session import API_URL, SpotifySession


def _make_session() -> SpotifySession:
    """Return a session with a mocked http layer and a throttler that never holds back."""
    mass = MagicMock()
    mass.metadata.locale = "en_US"
    return SpotifySession(
        mass,
        MagicMock(),
        "global",
        ThrottlerManager(rate_limit=100, period=1),
        get_auth=AsyncMock(return_value={"access_token": "token"}),
        on_unauthorized=MagicMock(),
    )


def _stub_response(
    session: SpotifySession,
    status: int = 200,
    payload: Any = None,
    headers: dict[str, str] | None = None,
) -> MagicMock:
    """
    Answer every request of the session with a canned response, return the request mock.

    :param session: The session whose requests to answer.
    :param status: HTTP status of the response.
    :param payload: JSON body of the response.
    :param headers: Headers of the response.
    """
    response = MagicMock()
    response.status = status
    response.reason = "reason"
    response.headers = headers or {}
    response.json = AsyncMock(return_value=payload)
    response.text = AsyncMock(return_value="")
    request = MagicMock(
        return_value=MagicMock(
            __aenter__=AsyncMock(return_value=response), __aexit__=AsyncMock(return_value=None)
        )
    )
    session.mass.http_session.request = request  # type: ignore[method-assign]
    return request


async def test_rate_limit_carries_the_retry_after() -> None:
    """A 429 raises RateLimited with the wait Spotify asked for."""
    session = _make_session()
    _stub_response(session, status=429, headers={"Retry-After": "42"})

    with pytest.raises(RateLimited) as err:
        async with session._request("GET", "me"):
            pass
    assert err.value.backoff_time == 42


async def test_unauthorized_drops_the_token() -> None:
    """A 401 reports the rejected token and asks for a retry."""
    session = _make_session()
    _stub_response(session, status=401)

    with pytest.raises(ResourceTemporarilyUnavailable) as err:
        async with session._request("GET", "me"):
            pass
    assert err.value.backoff_time == 1
    session._on_unauthorized.assert_called_once()  # type: ignore[attr-defined]


async def test_server_error_asks_for_a_later_retry() -> None:
    """A 502 raises ResourceTemporarilyUnavailable with a 30 second backoff."""
    session = _make_session()
    _stub_response(session, status=502)

    with pytest.raises(ResourceTemporarilyUnavailable) as err:
        async with session._request("GET", "me"):
            pass
    assert err.value.backoff_time == 30


async def test_get_not_found_raises_media_not_found() -> None:
    """A GET answered with 404 raises MediaNotFoundError."""
    session = _make_session()
    _stub_response(session, status=404, payload={"error": {"message": "missing"}})

    with pytest.raises(MediaNotFoundError, match="tracks/abc not found"):
        await session.get("tracks/abc")


async def test_get_stores_the_etag() -> None:
    """A GET returns the body with the ETag of the response, and asks in the user's language."""
    session = _make_session()
    request = _stub_response(session, payload={"id": "abc"}, headers={"ETag": '"v1"'})

    result = await session.get("tracks/abc", limit=1)
    assert result == {"id": "abc", "etag": '"v1"'}
    args, kwargs = request.call_args
    assert args == ("GET", f"{API_URL}/tracks/abc")
    assert kwargs["params"] == {"limit": 1, "market": "from_token", "country": "from_token"}
    assert kwargs["headers"]["Authorization"] == "Bearer token"
    assert kwargs["headers"]["Accept-Language"].startswith("en-US")


async def test_get_uses_the_given_token() -> None:
    """A GET with a token of its own does not ask the session for one."""
    session = _make_session()
    request = _stub_response(session, payload={})

    await session.get("me", auth_info={"access_token": "other"})
    assert request.call_args.kwargs["headers"]["Authorization"] == "Bearer other"
    session._get_auth.assert_not_called()  # type: ignore[attr-defined]


async def test_post_without_result_returns_empty() -> None:
    """A POST that does not want the result returns an empty dict without reading the body."""
    session = _make_session()
    request = _stub_response(session, payload={"id": "abc"})

    assert await session.post("me/tracks", {"ids": ["abc"]}, want_result=False) == {}
    assert request.call_args.args[0] == "POST"
    assert request.call_args.kwargs["json"] == {"ids": ["abc"]}
