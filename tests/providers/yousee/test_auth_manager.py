"""Test YouSee Musik's login and refresh flows."""

from unittest.mock import AsyncMock, MagicMock, call

import pytest
from aiohttp import ClientSession
from music_assistant_models.errors import LoginFailed

from music_assistant.constants import CONF_PASSWORD, CONF_USERNAME
from music_assistant.providers.music247e.auth_manager import Music247eAccessToken
from music_assistant.providers.yousee.auth_manager import YouSeeAuthManager

ACCESS_TOKEN = "UserId=test-user&ExpiresOn=1700003600&Signature=test=signature"
REFRESHED_TOKEN = "UserId=test-user&ExpiresOn=1700007200&Signature=refreshed"
LOGIN_ACTION = "/as/test-flow/resume/as/authorization.ping"
LOGIN_FORM = {
    "pf.username": "test-user",
    "pf.pass": "test-password",
    "pf.ok": "clicked",
    "pf.adapterId": "MusicUsernamePasswordAdapter",
}
COOKIES = {"session": "test-cookie"}


@pytest.fixture
def http_session() -> AsyncMock:
    """Return a mock HTTP session."""
    return AsyncMock(spec=ClientSession)


@pytest.fixture
def auth_manager(http_session: AsyncMock, monkeypatch: pytest.MonkeyPatch) -> YouSeeAuthManager:
    """Return a YouSee auth manager with a fixed clock and test credentials."""
    monkeypatch.setattr(
        "music_assistant.providers.music247e.auth_manager.time.time", lambda: 1700000000
    )
    provider = MagicMock()
    provider.mass.http_session = http_session
    provider.get_setup_value.side_effect = {
        CONF_USERNAME: "test-user",
        CONF_PASSWORD: "test-password",
    }.__getitem__
    return YouSeeAuthManager(provider)


@pytest.fixture
def login_responses(http_session: AsyncMock) -> tuple[AsyncMock, AsyncMock]:
    """Configure a successful delegated login form and token page."""
    delegate_response = AsyncMock()
    delegate_response.text.return_value = f'<form method="post" action="{LOGIN_ACTION}">'
    delegate_response.cookies = COOKIES
    login_response = AsyncMock()
    login_response.text.return_value = (
        f'localStorage.setItem("accesstoken", "{ACCESS_TOKEN}");'
        'localStorage.setItem("refreshtoken", "test-refresh");'
    )
    http_session.get.return_value.__aenter__.return_value = delegate_response
    http_session.post.return_value.__aenter__.return_value = login_response
    return delegate_response, login_response


@pytest.fixture(params=["http-error", "rejected", "missing-status"])
def rejected_refresh(
    request: pytest.FixtureRequest,
    auth_manager: YouSeeAuthManager,
    http_session: AsyncMock,
    login_responses: tuple[AsyncMock, AsyncMock],
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
    auth_manager: YouSeeAuthManager,
    http_session: AsyncMock,
) -> None:
    """Login posts the delegated form, extracts tokens, and reuses an unexpired access token."""
    token = await auth_manager.auth_token()

    assert isinstance(token, Music247eAccessToken)
    assert str(token) == ACCESS_TOKEN
    assert not token.is_expired()
    assert auth_manager._refresh_token == "test-refresh"
    assert await auth_manager.auth_token() is token
    http_session.get.assert_called_once_with("https://musik.yousee.dk/api/delegatedlogin")
    http_session.post.assert_called_once_with(
        f"https://login.yousee.dk{LOGIN_ACTION}", data=LOGIN_FORM, cookies=COOKIES
    )


@pytest.mark.parametrize("invalidate", [False, True], ids=["expired", "invalidated"])
async def test_refresh_rotates_tokens(
    auth_manager: YouSeeAuthManager, http_session: AsyncMock, invalidate: bool
) -> None:
    """Expired or invalidated access tokens refresh with form data without a credential login."""
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
        "https://musik.yousee.dk/api/token", data={"refresh_token": "test-refresh"}
    )
    response.json.assert_awaited_once_with(content_type=None)

    auth_manager.invalidate()
    assert str(await auth_manager.auth_token()) == REFRESHED_TOKEN
    assert http_session.post.call_count == 2
    http_session.post.assert_called_with(
        "https://musik.yousee.dk/api/token", data={"refresh_token": "rotated-refresh"}
    )


async def test_rejected_refresh_falls_back_to_login(
    auth_manager: YouSeeAuthManager,
    http_session: AsyncMock,
    rejected_refresh: AsyncMock,
) -> None:
    """HTTP and backend refresh failures fall back to a fresh credential login."""
    assert str(await auth_manager.auth_token()) == ACCESS_TOKEN

    assert auth_manager._refresh_token == "test-refresh"
    http_session.get.assert_called_once_with("https://musik.yousee.dk/api/delegatedlogin")
    assert http_session.post.call_args_list == [
        call("https://musik.yousee.dk/api/token", data={"refresh_token": "stale-refresh"}),
        call(f"https://login.yousee.dk{LOGIN_ACTION}", data=LOGIN_FORM, cookies=COOKIES),
    ]
    if rejected_refresh.ok:
        rejected_refresh.json.assert_awaited_once_with(content_type=None)
    else:
        rejected_refresh.json.assert_not_awaited()


@pytest.mark.usefixtures("rejected_refresh")
async def test_rejected_refresh_is_cleared_when_login_fails(
    auth_manager: YouSeeAuthManager,
    http_session: AsyncMock,
    login_responses: tuple[AsyncMock, AsyncMock],
) -> None:
    """A failed fallback login does not retain a rejected refresh token."""
    login_responses[1].text.return_value = "<html>Wrong password</html>"

    with pytest.raises(LoginFailed, match="access/refresh token not found"):
        await auth_manager.auth_token()

    assert auth_manager._access_token is None
    assert auth_manager._refresh_token is None
    assert http_session.post.call_count == 2


# Note that the cases below do not reflect the responses returned by the actual YouSee API;
# rather they simulate various failure scenarios for testing purposes, to stimulate the
# different branches of the authentication logic.


@pytest.mark.parametrize(
    ("failure", "message", "post_count"),
    [
        ("missing-action", "no login form", 0),
        ("missing-access-token", "access/refresh token not found", 1),
        ("missing-refresh-token", "access/refresh token not found", 1),
    ],
)
async def test_login_failure(
    auth_manager: YouSeeAuthManager,
    http_session: AsyncMock,
    login_responses: tuple[AsyncMock, AsyncMock],
    failure: str,
    message: str,
    post_count: int,
) -> None:
    """Incomplete login responses raise LoginFailed without caching tokens."""
    delegate_response, login_response = login_responses
    if failure == "missing-action":
        delegate_response.text.return_value = "<form method='post'>"
    elif failure == "missing-access-token":
        login_response.text.return_value = 'localStorage.setItem("refreshtoken", "test-refresh");'
    else:
        login_response.text.return_value = f'localStorage.setItem("accesstoken", "{ACCESS_TOKEN}");'

    with pytest.raises(LoginFailed, match=message):
        await auth_manager.auth_token()

    assert auth_manager._access_token is None
    assert auth_manager._refresh_token is None
    http_session.get.assert_called_once()
    assert http_session.post.call_count == post_count
