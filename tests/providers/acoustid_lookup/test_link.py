"""Tests for the AcoustID account link calls."""

from __future__ import annotations

import datetime
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from music_assistant_models.errors import (
    InvalidDataError,
    RateLimited,
    ResourceTemporarilyUnavailable,
)

from music_assistant.helpers.throttle_retry import MAX_RETRY_AFTER
from music_assistant.providers.acoustid_lookup import link as acoustid_link
from music_assistant.providers.acoustid_lookup.link import (
    ACOUSTID_LINK_STATUS_URL,
    ACOUSTID_REQUEST_LINK_URL,
    LinkState,
    link_status,
    request_link,
)
from music_assistant.providers.acoustid_lookup.provider import AcoustidLookupProvider

NOW = datetime.datetime(2026, 12, 1, 12, 0, tzinfo=datetime.UTC)
LINK_REPLY = {
    "status": "ok",
    "user": {
        "apikey": "UzBdV3X80J",
        "link": {
            "code": "WT14-9E3S",
            "url": "https://acoustid.org/link?code=WT14-9E3S",
            "expires": "2026-12-01T12:15:00+00:00",
        },
    },
}


@pytest.fixture(autouse=True)
def _fixed_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the application key and the current time."""
    monkeypatch.setattr(acoustid_link, "app_var", lambda _key: "APPKEY")
    monkeypatch.setattr(acoustid_link, "utc", lambda: NOW)


def _http_session(
    payload: Any = None,
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
    error: Exception | None = None,
) -> MagicMock:
    """Return an HTTP session answering every request with the given reply or error."""
    response = MagicMock()
    response.status = status
    response.headers = headers or {}
    response.json = AsyncMock(return_value=payload)
    request = MagicMock()
    request.__aenter__ = AsyncMock(return_value=response, side_effect=error)
    request.__aexit__ = AsyncMock(return_value=False)
    http_session = MagicMock()
    http_session.request = MagicMock(return_value=request)
    return http_session


async def test_request_link_returns_the_key_and_the_link() -> None:
    """A link reply yields the user key, the code, the page to open and the time left."""
    http_session = _http_session(LINK_REPLY)

    link = await request_link(http_session, "Living room server")

    assert link.user_key == "UzBdV3X80J"
    assert link.code == "WT14-9E3S"
    assert link.url == "https://acoustid.org/link?code=WT14-9E3S"
    assert link.expires_in == 900


async def test_request_link_sends_the_app_key_as_header_and_the_device_name() -> None:
    """The application key travels as a header, the device name is capped, no return URL."""
    http_session = _http_session(LINK_REPLY)

    await request_link(http_session, "x" * 150)

    call = http_session.request.call_args
    assert call.args == ("POST", ACOUSTID_REQUEST_LINK_URL)
    assert call.kwargs["headers"] == {"Acoustid-App-Key": "APPKEY"}
    assert call.kwargs["data"] == {"device": "x" * 100}
    assert "APPKEY" not in call.args[1]


async def test_an_expired_link_has_no_time_left() -> None:
    """A link whose expiry already passed reports no time left rather than a negative one."""
    reply = json.loads(json.dumps(LINK_REPLY))
    reply["user"]["link"]["expires"] = "2026-12-01T11:00:00+00:00"

    link = await request_link(_http_session(reply), "server")

    assert link.expires_in == 0


@pytest.mark.parametrize(
    "reply",
    [
        pytest.param({"status": "ok"}, id="no_user"),
        pytest.param({"status": "ok", "user": {"apikey": "KEY"}}, id="no_link"),
        pytest.param(
            {"status": "ok", "user": {"apikey": "", "link": LINK_REPLY["user"]["link"]}},  # type: ignore[index]
            id="empty_key",
        ),
        pytest.param(
            {
                "status": "ok",
                "user": {
                    "apikey": "KEY",
                    "link": {"code": "C", "url": "https://acoustid.org/link", "expires": "soon"},
                },
            },
            id="bad_expiry",
        ),
        pytest.param(
            {
                "status": "ok",
                "user": {
                    "apikey": "KEY",
                    "link": {
                        "code": "C",
                        "url": "https://acoustid.org/link",
                        "expires": "2026-12-01T12:15:00",
                    },
                },
            },
            id="expiry_without_timezone",
        ),
    ],
)
async def test_request_link_rejects_an_unusable_reply(reply: dict[str, Any]) -> None:
    """A reply that misses part of the link is reported as invalid data."""
    with pytest.raises(InvalidDataError):
        await request_link(_http_session(reply), "server")


@pytest.mark.parametrize("state", list(LinkState))
async def test_link_status_reports_each_state(state: LinkState) -> None:
    """Every state AcoustID documents is recognised."""
    http_session = _http_session({"status": "ok", "key": {"state": state.value, "expires": "x"}})

    assert await link_status(http_session, "USERKEY") is state


async def test_link_status_sends_both_keys_as_headers() -> None:
    """Both keys travel as headers, so neither ends up in the request URL."""
    http_session = _http_session({"status": "ok", "key": {"state": "approved"}})

    await link_status(http_session, "USERKEY")

    call = http_session.request.call_args
    assert call.args == ("GET", ACOUSTID_LINK_STATUS_URL)
    assert call.kwargs["headers"] == {
        "Acoustid-App-Key": "APPKEY",
        "Acoustid-User-Key": "USERKEY",
    }
    assert call.kwargs["data"] is None


@pytest.mark.parametrize(
    "reply",
    [
        pytest.param({"status": "ok"}, id="no_key"),
        pytest.param({"status": "ok", "key": {"state": "paused"}}, id="unheard_of_state"),
        pytest.param({"status": "ok", "key": {}}, id="no_state"),
    ],
)
async def test_link_status_rejects_an_unexpected_state(reply: dict[str, Any]) -> None:
    """A state AcoustID does not document is never mistaken for a withdrawn key."""
    with pytest.raises(InvalidDataError):
        await link_status(_http_session(reply), "USERKEY")


@pytest.mark.parametrize(
    ("session_kwargs", "expected", "backoff_time"),
    [
        pytest.param(
            {"status": 429, "headers": {"Retry-After": "30"}}, RateLimited, 30, id="http_429"
        ),
        pytest.param(
            {
                "status": 400,
                "payload": {"status": "error", "error": {"code": 14, "message": "slow down"}},
            },
            RateLimited,
            # AcoustID named no wait, so the default one applies
            5,
            id="too_many_requests_error",
        ),
        pytest.param({"status": 503}, ResourceTemporarilyUnavailable, 0, id="server_error"),
        pytest.param(
            {"error": aiohttp.ClientConnectionError("dropped")},
            ResourceTemporarilyUnavailable,
            0,
            id="connection_dropped",
        ),
        pytest.param({"error": TimeoutError()}, ResourceTemporarilyUnavailable, 0, id="timeout"),
        pytest.param(
            {
                "status": 400,
                "payload": {"status": "error", "error": {"code": 4, "message": "invalid API key"}},
            },
            InvalidDataError,
            None,
            id="refused",
        ),
        pytest.param({"payload": ["unexpected"]}, InvalidDataError, None, id="not_an_object"),
    ],
)
async def test_failed_calls_raise_typed_errors(
    session_kwargs: dict[str, Any], expected: type[Exception], backoff_time: int | None
) -> None:
    """Each way a call can fail surfaces as the matching error."""
    payload = session_kwargs.pop("payload", None)

    with pytest.raises(expected) as raised:
        await link_status(_http_session(payload, **session_kwargs), "USERKEY")

    assert type(raised.value) is expected
    if backoff_time is not None:
        assert getattr(raised.value, "backoff_time", None) == backoff_time


@pytest.mark.parametrize(
    ("session_kwargs", "wait"),
    [
        pytest.param({"status": 429, "headers": {"Retry-After": "30"}}, 30, id="wait_named"),
        pytest.param({"status": 429}, 5, id="no_wait_named"),
        pytest.param(
            {"status": 429, "headers": {"Retry-After": "999999999"}},
            MAX_RETRY_AFTER,
            id="absurd_wait_capped",
        ),
    ],
)
async def test_nothing_is_sent_while_acoustid_asked_to_wait(
    session_kwargs: dict[str, Any], wait: int
) -> None:
    """After a rate limit reply every request is refused unsent until the wait is over."""
    with pytest.raises(RateLimited):
        await request_link(_http_session(**session_kwargs), "Living room")
    http_session = _http_session({"status": "ok", "key": {"state": "approved", "expires": "x"}})

    with pytest.raises(RateLimited) as raised:
        await link_status(http_session, "USERKEY")

    http_session.request.assert_not_called()
    assert 0 < raised.value.backoff_time <= wait
    # once the wait is over the request goes out again
    acoustid_link.THROTTLER._cooldown_until = 0.0
    assert await link_status(http_session, "USERKEY") is LinkState.APPROVED
    http_session.request.assert_called_once()


def test_lookups_share_the_wait_with_the_account_requests() -> None:
    """A wait AcoustID asks for on a lookup holds the account requests back too."""
    assert AcoustidLookupProvider.throttler is acoustid_link.THROTTLER


async def test_request_link_leaves_out_what_acoustid_rejects_in_a_device_name() -> None:
    """Characters AcoustID refuses, such as the joiner inside an emoji, are not sent."""
    http_session = _http_session(LINK_REPLY)

    await request_link(http_session, "Living\u200d room\x07" + "x" * 200)

    device = http_session.request.call_args.kwargs["data"]["device"]
    assert device == ("Living room" + "x" * 200)[:100]


async def test_a_reply_that_is_not_json_is_invalid_data() -> None:
    """A reply that can not be parsed is reported as invalid data."""
    http_session = _http_session()
    response = http_session.request.return_value.__aenter__.return_value
    response.json = AsyncMock(side_effect=json.JSONDecodeError("bad", "", 0))

    with pytest.raises(InvalidDataError):
        await link_status(http_session, "USERKEY")
