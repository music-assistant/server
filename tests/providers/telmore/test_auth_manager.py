"""Test shared 24-7 authentication using Telmore's login and refresh flows."""

from unittest.mock import AsyncMock, MagicMock, call

import pytest
from aiohttp import ClientSession
from music_assistant_models.errors import LoginFailed

from music_assistant.constants import CONF_PASSWORD, CONF_USERNAME
from music_assistant.providers.music247e.auth_manager import Music247eAccessToken
from music_assistant.providers.telmore.auth_manager import TelmoreAuthManager

ACCESS_TOKEN = "UserId=test-user&ExpiresOn=1700003600&Signature=test=signature"
REFRESHED_TOKEN = "UserId=test-user&ExpiresOn=1700007200&Signature=refreshed"
LOGIN_URL = "https://musik.telmore.dk/login/callback"


@pytest.fixture
def http_session() -> AsyncMock:
    """Return a mock HTTP session."""
    return AsyncMock(spec=ClientSession)


@pytest.fixture
def auth_manager(http_session: AsyncMock, monkeypatch: pytest.MonkeyPatch) -> TelmoreAuthManager:
    """Return a Telmore auth manager with a fixed clock and test credentials."""
    monkeypatch.setattr(
        "music_assistant.providers.music247e.auth_manager.time.time", lambda: 1700000000
    )
    provider = MagicMock()
    provider.mass.http_session = http_session
    provider.get_setup_value.side_effect = {
        CONF_USERNAME: "test-user",
        CONF_PASSWORD: "test-password",
    }.__getitem__
    return TelmoreAuthManager(provider)


@pytest.fixture
def login_responses(http_session: AsyncMock) -> tuple[AsyncMock, AsyncMock, AsyncMock]:
    """Configure a successful delegated login and token page."""
    delegate_response = AsyncMock()
    delegate_response.headers = {"Location": "https://id.telmore.dk/login?session=test-session"}
    login_response = AsyncMock()
    login_response.status = 200
    login_response.json.return_value = {"url": LOGIN_URL}
    token_response = AsyncMock()
    token_response.text.return_value = (
        f'accessToken: "{ACCESS_TOKEN}", refreshToken: "test-refresh"'
    )
    http_session.get.return_value.__aenter__.side_effect = [delegate_response, token_response]
    http_session.post.return_value.__aenter__.return_value = login_response
    return delegate_response, login_response, token_response


@pytest.fixture(params=["http-error", "rejected", "missing-status"])
def rejected_refresh(
    request: pytest.FixtureRequest,
    auth_manager: TelmoreAuthManager,
    http_session: AsyncMock,
    login_responses: tuple[AsyncMock, AsyncMock, AsyncMock],
) -> AsyncMock:
    """Configure a failed refresh followed by credential login."""
    auth_manager._refresh_token = "stale-refresh"
    response = AsyncMock()
    response.ok = request.param != "http-error"
    response.json.return_value = {"status": 4} if request.param == "rejected" else {}
    http_session.post.return_value.__aenter__.side_effect = [response, login_responses[1]]
    return response


@pytest.mark.usefixtures("login_responses")
async def test_login_and_reuse_cached_token(
    auth_manager: TelmoreAuthManager,
    http_session: AsyncMock,
) -> None:
    """Login sends credentials, extracts tokens, and reuses an unexpired access token."""
    token = await auth_manager.auth_token()

    assert isinstance(token, Music247eAccessToken)
    assert str(token) == ACCESS_TOKEN
    assert not token.is_expired()
    assert auth_manager._refresh_token == "test-refresh"
    assert await auth_manager.auth_token() is token
    assert http_session.get.call_args_list == [
        call("https://musik.telmore.dk/api/delegatedlogin", allow_redirects=False),
        call(LOGIN_URL),
    ]
    http_session.post.assert_called_once_with(
        "https://id.telmore.dk/internal-login",
        params={"session": "test-session"},
        json={"session": "test-session", "username": "test-user", "password": "test-password"},
    )


@pytest.mark.parametrize("invalidate", [False, True], ids=["expired", "invalidated"])
async def test_refresh_rotates_tokens(
    auth_manager: TelmoreAuthManager, http_session: AsyncMock, invalidate: bool
) -> None:
    """Expired or invalidated access tokens refresh without repeating credential login."""
    auth_manager._access_token = Music247eAccessToken(
        ACCESS_TOKEN if invalidate else "ExpiresOn=1700000000"
    )
    auth_manager._refresh_token = "test-refresh"
    if invalidate:
        auth_manager.invalidate()
        assert auth_manager._access_token is None

    response = AsyncMock()
    response.ok = True
    response.json.return_value = {
        "status": 0,
        "tokenResult": {"access_token": REFRESHED_TOKEN, "refresh_token": "rotated-refresh"},
    }
    http_session.post.return_value.__aenter__.return_value = response

    token = await auth_manager.auth_token()

    assert str(token) == REFRESHED_TOKEN
    assert auth_manager._refresh_token == "rotated-refresh"
    assert await auth_manager.auth_token() is token
    http_session.get.assert_not_called()
    http_session.post.assert_called_once_with(
        "https://musik.telmore.dk/api/token", json={"refresh_token": "test-refresh"}
    )
    response.json.assert_awaited_once_with(content_type=None)

    auth_manager.invalidate()
    assert str(await auth_manager.auth_token()) == REFRESHED_TOKEN
    assert http_session.post.call_count == 2
    http_session.post.assert_called_with(
        "https://musik.telmore.dk/api/token", json={"refresh_token": "rotated-refresh"}
    )


async def test_rejected_refresh_falls_back_to_login(
    auth_manager: TelmoreAuthManager,
    http_session: AsyncMock,
    rejected_refresh: AsyncMock,
) -> None:
    """HTTP and backend refresh failures fall back to a fresh credential login."""
    assert str(await auth_manager.auth_token()) == ACCESS_TOKEN

    assert auth_manager._refresh_token == "test-refresh"
    assert http_session.get.call_args_list == [
        call("https://musik.telmore.dk/api/delegatedlogin", allow_redirects=False),
        call(LOGIN_URL),
    ]
    assert http_session.post.call_args_list == [
        call("https://musik.telmore.dk/api/token", json={"refresh_token": "stale-refresh"}),
        call(
            "https://id.telmore.dk/internal-login",
            params={"session": "test-session"},
            json={
                "session": "test-session",
                "username": "test-user",
                "password": "test-password",
            },
        ),
    ]
    if rejected_refresh.ok:
        rejected_refresh.json.assert_awaited_once_with(content_type=None)
    else:
        rejected_refresh.json.assert_not_awaited()


@pytest.mark.usefixtures("rejected_refresh")
async def test_rejected_refresh_is_cleared_when_login_fails(
    auth_manager: TelmoreAuthManager,
    http_session: AsyncMock,
    login_responses: tuple[AsyncMock, AsyncMock, AsyncMock],
) -> None:
    """A failed fallback login does not retain a rejected refresh token."""
    login_responses[1].status = 401

    with pytest.raises(LoginFailed, match="internal-login returned HTTP 401"):
        await auth_manager.auth_token()

    assert auth_manager._access_token is None
    assert auth_manager._refresh_token is None
    assert http_session.post.call_count == 2


# Note that the cases below do not reflect the responses returned by the actual Telmore API;
# rather they simulate various failure scenarios for testing purposes, to stimulate the
# different branches of the authentication logic.


@pytest.mark.parametrize(
    ("failure", "message", "get_count", "post_count"),
    [
        ("missing-location", "no session", 1, 0),
        ("missing-session", "no session", 1, 0),
        ("rejected-login", "internal-login returned HTTP 401", 1, 1),
        ("missing-url", "no redirect URL", 1, 1),
        ("missing-access-token", "access/refresh token not found", 2, 1),
        ("missing-refresh-token", "access/refresh token not found", 2, 1),
    ],
)
async def test_login_failure(
    auth_manager: TelmoreAuthManager,
    http_session: AsyncMock,
    login_responses: tuple[AsyncMock, AsyncMock, AsyncMock],
    failure: str,
    message: str,
    get_count: int,
    post_count: int,
) -> None:
    """Incomplete or rejected login responses raise LoginFailed without caching tokens."""
    delegate_response, login_response, token_response = login_responses
    if failure == "missing-location":
        delegate_response.headers = {}
    elif failure == "missing-session":
        delegate_response.headers = {"Location": "https://id.telmore.dk/login"}
    elif failure == "rejected-login":
        login_response.status = 401
    elif failure == "missing-url":
        login_response.json.return_value = {}
    elif failure == "missing-access-token":
        token_response.text.return_value = 'refreshToken: "test-refresh"'
    else:
        token_response.text.return_value = f'accessToken: "{ACCESS_TOKEN}"'

    with pytest.raises(LoginFailed, match=message):
        await auth_manager.auth_token()

    assert auth_manager._access_token is None
    assert auth_manager._refresh_token is None
    assert http_session.get.call_count == get_count
    assert http_session.post.call_count == post_count
