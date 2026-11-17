"""Tests for the request handling of a Spotify Web API session."""

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.errors import (
    MediaNotFoundError,
    RateLimited,
    ResourceTemporarilyUnavailable,
    RetriesExhausted,
)
from orjson import JSONDecodeError

from music_assistant.helpers.throttle_retry import ThrottlerManager
from music_assistant.providers.spotify.session import API_URL, SpotifySession

QUOTA_BODY = {"error": {"status": 429, "message": "Quota exceeded", "reason": "QUOTA_EXCEEDED"}}


def _make_session(personal: bool = False) -> SpotifySession:
    """
    Return a session with a mocked http layer and a throttler that never holds back.

    :param personal: Return the session of a custom Client ID, which the shared client stands
        in for, instead of the shared client.
    """
    mass = MagicMock()
    mass.metadata.locale = "en_US"
    return SpotifySession(
        mass,
        MagicMock(),
        "dev" if personal else "global",
        "custom Client ID" if personal else "shared client",
        ThrottlerManager(rate_limit=100, period=1),
        get_auth=AsyncMock(return_value={"access_token": "token"}),
        on_unauthorized=MagicMock(),
        has_fallback=personal,
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
    :param payload: JSON body of the response, an exception when the body is not JSON.
    :param headers: Headers of the response.
    """
    response = MagicMock()
    response.status = status
    response.reason = "reason"
    response.headers = headers or {}
    response.json = (
        AsyncMock(side_effect=payload)
        if isinstance(payload, Exception)
        else AsyncMock(return_value=payload)
    )
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


@pytest.mark.parametrize(
    "payload",
    [
        None,
        JSONDecodeError("Expected value", "Too many requests", 0),
        TimeoutError(),
        "Too many requests",
        {"error": {"status": 429, "message": "API rate limit exceeded"}},
    ],
)
async def test_rate_limit_without_a_reason_is_retried_as_before(payload: Any) -> None:
    """A 429 whose body is missing, not JSON or without a reason is retried after the wait."""
    session = _make_session()
    _stub_response(session, status=429, payload=payload, headers={"Retry-After": "42"})

    with pytest.raises(RateLimited) as err:
        async with session._request("GET", "me"):
            pass
    assert err.value.backoff_time == 42
    assert str(err.value) == "Spotify rate limited the shared client"
    assert 41 < session.throttler.cooldown_remaining <= 42
    assert session.get_diagnostics()["last_rate_limit_reason"] is None


async def test_rate_limit_names_the_client_and_reason() -> None:
    """The failed attempt is logged with the client and the reason Spotify gives."""
    session = _make_session()
    session.throttler.retry_attempts = 1
    _stub_response(
        session,
        status=429,
        payload={"error": {"reason": "SOME_REASON"}},
        headers={"Retry-After": "2"},
    )

    with pytest.raises(RetriesExhausted):
        await session.get("me")
    logged = [call.args for call in session.logger.info.call_args_list]  # type: ignore[attr-defined]
    assert logged[0][:3] == ("Attempt %s/%s failed: %s", 1, 1)
    assert str(logged[0][3]) == "Spotify rate limited the shared client (SOME_REASON)"
    diagnostics = session.get_diagnostics()
    assert diagnostics["last_rate_limit_reason"] == "SOME_REASON"
    assert diagnostics["last_rate_limit_at"] is not None


@pytest.mark.parametrize(
    ("headers", "cooldown", "how_long"),
    [
        ({"Retry-After": "3600"}, 3600, "Spotify asks to wait 3600 seconds"),
        ({}, 5, "Spotify did not say for how long"),
    ],
)
async def test_quota_exceeded_is_not_retried_in_place(
    headers: dict[str, str], cooldown: int, how_long: str
) -> None:
    """A used up quota arms the cooldown, gives up at once and logs one warning per block."""
    session = _make_session(personal=True)
    request = _stub_response(session, status=429, payload=QUOTA_BODY, headers=headers)

    with patch("music_assistant.helpers.throttle_retry.asyncio.sleep", AsyncMock()) as sleep:
        with pytest.raises(RetriesExhausted) as err:
            await session.get("me")
        assert err.value.translation_key == RateLimited.translation_key
        assert request.call_count == 1
        sleep.assert_not_awaited()
        assert cooldown - 1 < session.throttler.cooldown_remaining <= cooldown
        # the block goes on: no second warning
        session.throttler._cooldown_until = 0
        with pytest.raises(RetriesExhausted):
            await session.get("me")
    session.logger.warning.assert_called_once_with(  # type: ignore[attr-defined]
        "The %s has used up its Spotify quota, %s", "custom Client ID", how_long
    )
    assert session.get_diagnostics()["last_rate_limit_reason"] == "QUOTA_EXCEEDED"


async def test_quota_warning_returns_after_an_answer() -> None:
    """A quota block that follows an answered request is warned about again."""
    session = _make_session(personal=True)
    _stub_response(session, status=429, payload=QUOTA_BODY)
    with pytest.raises(RetriesExhausted):
        await session.get("me")
    _stub_response(session, payload={})
    session.throttler._cooldown_until = 0
    await session.get("me")
    _stub_response(session, status=429, payload=QUOTA_BODY)
    session.throttler._cooldown_until = 0
    with pytest.raises(RetriesExhausted):
        await session.get("me")
    assert session.logger.warning.call_count == 2  # type: ignore[attr-defined]


async def test_quota_block_without_a_wait_is_asked_about_less_and_less() -> None:
    """Each further quota answer that names no wait doubles the time until the next request."""
    session = _make_session(personal=True)
    _stub_response(session, status=429, payload=QUOTA_BODY)
    cooldowns = []
    for _ in range(8):
        with pytest.raises(RetriesExhausted):
            await session.get("me")
        cooldowns.append(round(session.throttler.cooldown_remaining))
        session.throttler._cooldown_until = 0
    assert cooldowns == [5, 10, 20, 40, 80, 120, 120, 120]
    # an answered request ends the block
    _stub_response(session, payload={})
    await session.get("me")
    _stub_response(session, status=429, payload=QUOTA_BODY)
    with pytest.raises(RetriesExhausted):
        await session.get("me")
    assert round(session.throttler.cooldown_remaining) == 5


async def test_quota_exceeded_on_the_shared_client_is_a_rate_limit_like_any_other() -> None:
    """Without another client to stand in, a used up quota is retried after the wait."""
    session = _make_session()
    _stub_response(session, status=429, payload=QUOTA_BODY, headers={"Retry-After": "42"})

    for _ in range(2):
        with pytest.raises(RateLimited) as err:
            async with session._request("GET", "me"):
                pass
        assert err.value.backoff_time == 42
    assert 41 < session.throttler.cooldown_remaining <= 42
    session.logger.warning.assert_called_once_with(  # type: ignore[attr-defined]
        "The %s has used up its Spotify quota, %s",
        "shared client",
        "Spotify asks to wait 42 seconds",
    )


@pytest.mark.parametrize("personal", [True, False])
async def test_the_gate_closes_before_the_body_of_a_limit_is_read(personal: bool) -> None:
    """A client that was told to wait holds further requests back while the body comes."""
    session = _make_session(personal=personal)
    body_asked = asyncio.Event()
    body_sent = asyncio.Event()

    async def _slow_body(**_kwargs: Any) -> dict[str, Any]:
        body_asked.set()
        await body_sent.wait()
        return {}

    request = _stub_response(session, status=429, headers={"Retry-After": "3600"})
    request.return_value.__aenter__.return_value.json = _slow_body

    async def _user_action() -> None:
        async with session._request("GET", "me"):
            pass

    task = asyncio.create_task(_user_action())
    await asyncio.wait_for(body_asked.wait(), 1)
    assert session.throttler.cooldown_remaining > 3590
    body_sent.set()
    # the personal client gives up, the shared client leaves the retry to its caller
    with pytest.raises(RetriesExhausted if personal else RateLimited):
        await task


async def test_a_body_that_never_comes_is_given_up_on() -> None:
    """A limit whose body stalls is handled from its headers once the wait for it is over."""
    session = _make_session()

    async def _no_body(**_kwargs: Any) -> dict[str, Any]:
        await asyncio.sleep(3600)
        return {}

    request = _stub_response(session, status=429, headers={"Retry-After": "42"})
    request.return_value.__aenter__.return_value.json = _no_body

    async def _attempt() -> None:
        async with session._request("GET", "me"):
            pass

    with (
        patch("music_assistant.providers.spotify.session.RATE_LIMIT_BODY_TIMEOUT", 0.01),
        pytest.raises(RateLimited) as err,
    ):
        await asyncio.wait_for(_attempt(), 1)
    assert err.value.backoff_time == 42
    assert session.get_diagnostics()["last_rate_limit_reason"] is None


async def test_a_limit_that_moves_the_request_on_is_logged_with_its_reason() -> None:
    """A request that leaves a limited client logs the client and the reason Spotify gave."""
    session = _make_session(personal=True)
    _stub_response(
        session,
        status=429,
        payload={"error": {"reason": "SOME_REASON"}},
        headers={"Retry-After": "8"},
    )

    with pytest.raises(RetriesExhausted):
        async with session._request("GET", "me"):
            pass
    session.logger.info.assert_called_once_with(  # type: ignore[attr-defined]
        "Spotify rate limited the custom Client ID (SOME_REASON)"
    )
    session.logger.warning.assert_not_called()  # type: ignore[attr-defined]


async def test_a_used_up_quota_is_logged_once_per_answer() -> None:
    """The first quota answer of a block warns, the ones after it are logged as a limit."""
    session = _make_session(personal=True)
    _stub_response(session, status=429, payload=QUOTA_BODY)
    for _ in range(2):
        with pytest.raises(RetriesExhausted):
            async with session._request("GET", "me"):
                pass
        session.throttler._cooldown_until = 0
    session.logger.warning.assert_called_once()  # type: ignore[attr-defined]
    session.logger.info.assert_called_once_with(  # type: ignore[attr-defined]
        "Spotify rate limited the custom Client ID (QUOTA_EXCEEDED)"
    )


async def test_a_request_that_stood_in_line_is_not_sent_to_a_client_told_to_wait() -> None:
    """A user action that was waiting for its turn gives up once the client is held back."""
    session = _make_session(personal=True)
    request = _stub_response(session, payload={})
    session.throttler.set_cooldown(3600)

    with pytest.raises(RetriesExhausted) as err:
        async with session._request("GET", "me"):
            pass
    assert err.value.translation_key == RateLimited.translation_key
    request.assert_not_called()


async def test_the_shared_client_sends_a_request_that_stood_in_line() -> None:
    """Without another client to stand in, a request past the gate is sent as before."""
    session = _make_session()
    request = _stub_response(session, payload={})
    session.throttler.set_cooldown(3600)

    async with session._request("GET", "me"):
        pass
    request.assert_called_once()
