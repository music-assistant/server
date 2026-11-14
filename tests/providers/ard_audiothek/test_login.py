"""Test the ARD Audiothek account login."""

import json
from contextlib import nullcontext
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import jwt
import pytest
from music_assistant_models.errors import LoginFailed, ResourceTemporarilyUnavailable

from music_assistant.providers.ard_audiothek import auth
from music_assistant.providers.ard_audiothek.auth import (
    PlayHistoryEntry,
    add_subscription,
    get_play_history,
    get_subscribed_podcast_ids,
    poll_device_token,
    refresh_tokens,
    remove_subscription,
    request_device_code,
    set_play_history_entry,
)


@pytest.fixture(autouse=True)
def throttler(monkeypatch: pytest.MonkeyPatch) -> Mock:
    """Replace the shared throttler, so no test waits for it or for a cooldown of another."""
    fake = Mock(acquire=Mock(side_effect=lambda: nullcontext()))
    monkeypatch.setattr(auth, "THROTTLER", fake)
    return fake


def _id_token(**claims: Any) -> str:
    """Return an ID token carrying the given claims."""
    return jwt.encode(claims, "secret-key-for-tests-only-0123456789", algorithm="HS256")


def _response(
    status: int = 200, payload: Any = None, text: str = "", headers: dict[str, str] | None = None
) -> MagicMock:
    """Return an async context manager mocking an aiohttp response."""
    response = Mock(status=status, headers=headers or {})
    response.json = AsyncMock(return_value=payload)
    response.text = AsyncMock(return_value=text or json.dumps(payload))
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=response)
    context.__aexit__ = AsyncMock(return_value=False)
    return context


async def test_request_device_code() -> None:
    """Return the device code as handed out by ARD."""
    device = {
        "user_code": "ABCDE",
        "verification_uri": "https://accounts.ard.de/id/ABCDE?clientId=at",
        "token_uri": "https://accounts.ard.de/device/token/secret",
        "interval": 5,
        "expires_in": 180,
    }
    session = Mock(get=Mock(return_value=_response(payload=device)))

    assert await request_device_code(session) == device


async def test_request_device_code_error_raises_login_failed() -> None:
    """Raise LoginFailed when ARD returns no usable device code."""
    session = Mock(get=Mock(return_value=_response(payload={"error": "nope"})))

    with pytest.raises(LoginFailed):
        await request_device_code(session)


async def test_poll_device_token_waits_for_confirmation() -> None:
    """Keep polling while the login is pending and return the tokens once confirmed."""
    id_token = _id_token(user_id="uid", name="Jane", email="jane@example.com", exp=1000)
    session = Mock(
        get=Mock(
            side_effect=[
                _response(status=425, text="authorization_pending"),
                _response(text=f'{{"idToken": "{id_token}", "refreshToken": "r"}}'),
            ]
        )
    )

    with patch("music_assistant.providers.ard_audiothek.auth.asyncio.sleep", AsyncMock()):
        tokens = await poll_device_token(session, {"token_uri": "https://x/token", "interval": 5})

    assert (tokens.user_id, tokens.display_name, tokens.email) == (
        "uid",
        "Jane",
        "jane@example.com",
    )
    assert (tokens.id_token, tokens.refresh_token, tokens.expires_at) == (id_token, "r", 1000)
    assert session.get.call_count == 2


async def test_poll_device_token_error_raises_login_failed() -> None:
    """Raise LoginFailed when ARD answers the poll with an error."""
    session = Mock(get=Mock(return_value=_response(status=400, text='{"error": "expired"}')))

    with (
        patch("music_assistant.providers.ard_audiothek.auth.asyncio.sleep", AsyncMock()),
        pytest.raises(LoginFailed),
    ):
        await poll_device_token(session, {"token_uri": "https://x/token"})


async def test_refresh_tokens() -> None:
    """Take the new ID and refresh token from the token refresh response."""
    id_token = _id_token(user_id="uid", exp=2000)
    session = Mock(
        post=Mock(return_value=_response(payload={"id_token": id_token, "refresh_token": "new"}))
    )

    tokens = await refresh_tokens(session, "old")

    assert (tokens.id_token, tokens.refresh_token, tokens.user_id) == (id_token, "new", "uid")
    assert session.post.call_args.kwargs["data"]["refresh_token"] == "old"


@pytest.mark.parametrize(
    "message",
    ["TOKEN_EXPIRED", "INVALID_REFRESH_TOKEN : Invalid refresh token", "USER_DISABLED"],
)
async def test_refresh_tokens_error_raises_login_failed(message: str) -> None:
    """Raise LoginFailed with the reason when the refresh token is rejected."""
    payload = {"error": {"code": 400, "message": message}}
    session = Mock(post=Mock(return_value=_response(status=400, payload=payload)))

    with pytest.raises(LoginFailed, match=message.split(" ", 1)[0]):
        await refresh_tokens(session, "old")


@pytest.mark.parametrize(
    ("status", "text"),
    [
        (503, '{"error": {"message": "UNAVAILABLE"}}'),
        (502, "<html>"),
        (400, '{"error": {"code": 400, "message": "QUOTA_EXCEEDED"}}'),
    ],
    ids=["unavailable", "html", "quota"],
)
async def test_refresh_tokens_server_error_is_temporary(
    throttler: Mock, status: int, text: str
) -> None:
    """A server error while refreshing is temporary and does not reject the account."""
    response = _response(status=status, text=text, headers={"Retry-After": "120"})
    session = Mock(post=Mock(return_value=response))

    with pytest.raises(ResourceTemporarilyUnavailable) as err:
        await refresh_tokens(session, "old")

    assert err.value.backoff_time == 120
    throttler.set_cooldown.assert_called_once_with(120)


async def test_poll_device_token_backs_off_on_server_errors() -> None:
    """A server error while polling is no reason to give up, but the next poll waits longer."""
    id_token = _id_token(user_id="uid", exp=1000)
    session = Mock(
        get=Mock(
            side_effect=[
                _response(status=429, text="slow down", headers={"Retry-After": "30"}),
                _response(status=503, text="<html>"),
                _response(text=f'{{"idToken": "{id_token}", "refreshToken": "r"}}'),
            ]
        )
    )
    sleep = AsyncMock()

    with patch("music_assistant.providers.ard_audiothek.auth.asyncio.sleep", sleep):
        await poll_device_token(session, {"token_uri": "https://x/token", "interval": 5})

    assert [call.args[0] for call in sleep.await_args_list] == [5, 30, 20]


async def test_firestore_server_error_is_temporary(throttler: Mock) -> None:
    """A Firestore server error is temporary and holds back requests as long as asked."""
    response = _response(status=429, text="<html>", headers={"Retry-After": "90"})
    session = Mock(request=Mock(return_value=response))

    with pytest.raises(ResourceTemporarilyUnavailable) as err:
        await get_subscribed_podcast_ids(session, "token", "uid")

    assert err.value.backoff_time == 90
    throttler.set_cooldown.assert_called_once_with(90)


async def test_requests_are_throttled(throttler: Mock) -> None:
    """The token refresh and the Firestore requests go through the shared throttler."""
    id_token = _id_token(user_id="uid", exp=2000)
    session = Mock(
        post=Mock(return_value=_response(payload={"id_token": id_token})),
        request=Mock(return_value=_response(payload={})),
    )

    await refresh_tokens(session, "old")
    assert throttler.acquire.call_count == 1
    await remove_subscription(session, "token", "uid", "urn:ard:show:2af809299201a921")
    assert throttler.acquire.call_count == 2


async def test_get_subscribed_podcast_ids_follows_pages() -> None:
    """Collect the podcast ids from all pages of the account's subscription list."""
    base = "projects/ardmt-prod/databases/(default)/documents/audiothek/uid/lists/groupings/items"
    pages = [
        {"documents": [{"name": f"{base}/111"}, {"name": f"{base}/222"}], "nextPageToken": "n"},
        {"documents": [{"name": f"{base}/333"}]},
    ]
    session = Mock(request=Mock(side_effect=[_response(payload=page) for page in pages]))

    assert await get_subscribed_podcast_ids(session, "token", "uid") == ["111", "222", "333"]
    assert (
        session.request.call_args_list[0].args[1].endswith("/audiothek/uid/lists/groupings/items")
    )
    assert session.request.call_args_list[0].kwargs["headers"]["Authorization"] == "Bearer token"
    assert session.request.call_args_list[1].kwargs["params"]["pageToken"] == "n"


async def test_get_subscribed_podcast_ids_without_subscriptions() -> None:
    """Return no ids for an account without subscriptions (Firestore omits the documents)."""
    session = Mock(request=Mock(return_value=_response(payload={})))

    assert await get_subscribed_podcast_ids(session, "token", "uid") == []


async def test_get_subscribed_podcast_ids_rejected_token_raises_login_failed() -> None:
    """Raise LoginFailed when Firestore rejects the account token."""
    payload = {"error": {"code": 401, "message": "unauthenticated"}}
    session = Mock(request=Mock(return_value=_response(status=401, payload=payload)))

    with pytest.raises(LoginFailed):
        await get_subscribed_podcast_ids(session, "token", "uid")


async def test_get_play_history() -> None:
    """Read playtime and completion per episode from the account's play history."""
    base = "projects/ardmt-prod/databases/(default)/documents/audiothek/uid/lists/playhistory/items"
    page = {
        "documents": [
            {
                "name": f"{base}/111",
                "fields": {
                    "playtime": {"integerValue": "95"},
                    "completed": {"booleanValue": False},
                    "updatedOn": {"timestampValue": "2026-11-13T20:05:39.123456789Z"},
                },
            },
            {
                "name": f"{base}/222",
                "fields": {
                    "playtime": {"doubleValue": 1799.5},
                    "completed": {"booleanValue": True},
                },
            },
            {"name": f"{base}/333", "fields": {}},
        ]
    }
    session = Mock(request=Mock(return_value=_response(payload=page)))

    assert await get_play_history(session, "token", "uid") == {
        "111": PlayHistoryEntry(False, 95.0, datetime(2026, 11, 13, 20, 5, 39, 123456, UTC)),
        "222": PlayHistoryEntry(True, 1799.5, None),
        "333": PlayHistoryEntry(False, 0.0, None),
    }
    assert session.request.call_args.args[1].endswith("/audiothek/uid/lists/playhistory/items")


async def test_set_play_history_entry() -> None:
    """Replace the episode's play history document the way the ARD Sounds apps write it."""
    session = Mock(request=Mock(return_value=_response(payload={"name": "doc"})))

    await set_play_history_entry(session, "token", "uid", "111", 95, completed=True)

    url = session.request.call_args.args[1]
    fields = session.request.call_args.kwargs["json"]["fields"]
    assert url.endswith("/audiothek/uid/lists/playhistory/items/111")
    assert session.request.call_args.kwargs["headers"]["Authorization"] == "Bearer token"
    assert fields["id"] == {"stringValue": "111"}
    assert fields["type"] == {"stringValue": "aod"}
    assert fields["playtime"] == {"integerValue": "95"}
    assert fields["completed"] == {"booleanValue": True}
    assert fields["updatedOn"]["timestampValue"].endswith("Z")


async def test_set_play_history_entry_error_raises() -> None:
    """Raise when Firestore refuses to store the play history."""
    payload = {"error": {"code": 400, "message": "INVALID_ARGUMENT"}}
    session = Mock(request=Mock(return_value=_response(status=400, payload=payload)))

    with pytest.raises(RuntimeError, match="INVALID_ARGUMENT"):
        await set_play_history_entry(session, "token", "uid", "111", 95, completed=False)


async def test_add_subscription() -> None:
    """Subscribe by writing the podcast into the account's list the way ARD Sounds does."""
    session = Mock(request=Mock(return_value=_response(payload={"name": "doc"})))

    await add_subscription(session, "token", "uid", "urn:ard:show:2af809299201a921")

    url = session.request.call_args.args[1]
    fields = session.request.call_args.kwargs["json"]["fields"]
    assert url.endswith("/audiothek/uid/lists/groupings/items/urn:ard:show:2af809299201a921")
    assert fields["id"] == {"stringValue": "urn:ard:show:2af809299201a921"}
    assert fields["type"] == {"stringValue": "grouping"}
    assert fields["updatedOn"]["timestampValue"].endswith("Z")


async def test_remove_subscription() -> None:
    """Unsubscribe by deleting the podcast from the account's list."""
    session = Mock(request=Mock(return_value=_response(payload={})))

    await remove_subscription(session, "token", "uid", "urn:ard:show:2af809299201a921")

    url = session.request.call_args.args[1]
    assert url.endswith("/audiothek/uid/lists/groupings/items/urn:ard:show:2af809299201a921")
    assert session.request.call_args.kwargs["headers"]["Authorization"] == "Bearer token"


async def test_remove_subscription_rejected_token_raises_login_failed() -> None:
    """Raise LoginFailed when Firestore rejects the account token."""
    payload = {"error": {"code": 403, "message": "PERMISSION_DENIED"}}
    session = Mock(request=Mock(return_value=_response(status=403, payload=payload)))

    with pytest.raises(LoginFailed):
        await remove_subscription(session, "token", "uid", "urn:ard:show:2af809299201a921")
