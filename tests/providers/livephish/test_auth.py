"""Test Live Phish login: token issue, rejection, and in-memory reuse."""

from __future__ import annotations

from time import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.errors import LoginFailed

from music_assistant.constants import CONF_PASSWORD, CONF_USERNAME
from music_assistant.providers.livephish import LivePhishProvider
from music_assistant.providers.livephish.constants import (
    ACCOUNT_URL,
    CLIENT_ID,
    GRANT_TYPE,
    REDIRECT_URI,
    TOKEN_URL,
)

USERNAME = "listener@example.com"
PASSWORD = "secret"
ACCESS_TOKEN = "access-token"
AUTH_CODE = "auth-code"
ACCOUNT = {"userId": "user-1"}
LOGIN_URL = "https://id.livephish.com/account/login?returnUrl=%2Fconnect%2Fauthorize%2Fcallback"
LOGIN_HTML = (
    '<input name="Input.Email">'
    '<input name="__RequestVerificationToken" type="hidden" value="verify-token">'
)


def _set_credentials(provider: LivePhishProvider) -> None:
    """Point setup values at a username and password."""
    provider.get_setup_value = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda key, default=None: {
            CONF_USERNAME: USERNAME,
            CONF_PASSWORD: PASSWORD,
        }.get(key, default)
    )


def _http_context(
    status: int,
    payload: Any = None,
    *,
    url: str = LOGIN_URL,
    text: str = "",
    headers: dict[str, str] | None = None,
) -> AsyncMock:
    """Return an async context manager yielding a response with the given status and body."""
    response = AsyncMock()
    response.status = status
    response.url = url
    response.headers = headers or {}
    response.json = AsyncMock(return_value=payload)
    response.text = AsyncMock(return_value=text)
    response.raise_for_status = MagicMock()
    context = AsyncMock()
    context.__aenter__ = AsyncMock(return_value=response)
    context.__aexit__ = AsyncMock(return_value=False)
    return context


def _stub_sessions(
    provider: LivePhishProvider,
    *,
    login_status: int,
    login_body: str = "",
    login_headers: dict[str, str] | None = None,
    token_status: int = 200,
    token_payload: dict[str, Any] | None = None,
    account_status: int = 200,
) -> tuple[MagicMock, MagicMock]:
    """Stub the identity login session and the token/account session."""
    login_session = MagicMock()
    login_session.get = MagicMock(return_value=_http_context(200, text=LOGIN_HTML))
    login_session.post = MagicMock(
        return_value=_http_context(
            login_status, text=login_body, headers=login_headers, url=LOGIN_URL
        )
    )
    login_session.close = AsyncMock()
    token_session = MagicMock()
    token_session.post = MagicMock(
        return_value=_http_context(
            token_status,
            token_payload or {"access_token": ACCESS_TOKEN, "expires_in": 3600},
        )
    )
    token_session.get = MagicMock(return_value=_http_context(account_status, ACCOUNT))
    mass: Any = provider.mass
    mass.http_session = token_session
    return login_session, token_session


@pytest.mark.asyncio
async def test_login_stores_token_after_account_accepts_it(provider: LivePhishProvider) -> None:
    """An authorization code exchanged for a token is kept after the account call accepts it."""
    _set_credentials(provider)
    login_session, token_session = _stub_sessions(
        provider,
        login_status=302,
        login_headers={"Location": f"{REDIRECT_URI}?code={AUTH_CODE}"},
    )

    with patch(
        "music_assistant.providers.livephish.create_clientsession", return_value=login_session
    ):
        assert await provider.login() == ACCESS_TOKEN

    assert provider._auth_token == ACCESS_TOKEN
    assert provider._token_expiry > time()
    login_session.close.assert_awaited_once()
    form = login_session.post.call_args.kwargs["data"]
    assert form["Input.Email"] == USERNAME
    assert form["Input.Password"] == PASSWORD
    assert form["__RequestVerificationToken"] == "verify-token"
    assert token_session.post.call_args.args[0] == TOKEN_URL
    token_form = token_session.post.call_args.kwargs["data"]
    assert token_form["grant_type"] == GRANT_TYPE
    assert token_form["code"] == AUTH_CODE
    assert token_form["client_id"] == CLIENT_ID
    assert token_form["redirect_uri"] == REDIRECT_URI
    assert token_form["code_verifier"]
    assert token_session.get.call_args.args[0] == ACCOUNT_URL
    assert (
        token_session.get.call_args.kwargs["headers"]["Authorization"] == f"Bearer {ACCESS_TOKEN}"
    )


@pytest.mark.asyncio
async def test_login_rejects_invalid_password_without_storing_a_token(
    provider: LivePhishProvider,
) -> None:
    """The identity login form's rejection is a failed login and does not request a token."""
    _set_credentials(provider)
    login_session, token_session = _stub_sessions(
        provider,
        login_status=200,
        login_body="Invalid username or password.",
    )

    with (
        patch(
            "music_assistant.providers.livephish.create_clientsession", return_value=login_session
        ),
        pytest.raises(LoginFailed, match="Invalid Live Phish username or password"),
    ):
        await provider.login()

    assert provider._auth_token is None
    token_session.post.assert_not_called()
    token_session.get.assert_not_called()
    login_session.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_login_reuses_unexpired_token(provider: LivePhishProvider) -> None:
    """A bearer that has not expired is returned without another token or account request."""
    provider._auth_token = ACCESS_TOKEN
    provider._token_expiry = time() + 3600
    _login_session, token_session = _stub_sessions(provider, login_status=302)
    session_factory = MagicMock()

    with patch("music_assistant.providers.livephish.create_clientsession", session_factory):
        assert await provider.login() == ACCESS_TOKEN

    session_factory.assert_not_called()
    token_session.post.assert_not_called()
    token_session.get.assert_not_called()


@pytest.mark.asyncio
async def test_login_discards_token_when_account_returns_401(provider: LivePhishProvider) -> None:
    """A token the account endpoint rejects is not kept for reuse."""
    _set_credentials(provider)
    login_session, token_session = _stub_sessions(
        provider,
        login_status=302,
        login_headers={"Location": f"{REDIRECT_URI}?code={AUTH_CODE}"},
        account_status=401,
    )

    with (
        patch(
            "music_assistant.providers.livephish.create_clientsession", return_value=login_session
        ),
        pytest.raises(LoginFailed, match="authentication expired or was rejected"),
    ):
        await provider.login()

    assert provider._auth_token is None
    assert provider._token_expiry == 0
    token_session.get.assert_called_once()


@pytest.mark.asyncio
async def test_init_drops_cached_account_and_logs_in(provider: LivePhishProvider) -> None:
    """Loading the provider drops cached account details, then logs in."""
    cache: Any = provider.mass.cache
    cache.delete = AsyncMock()
    provider.login = AsyncMock()  # type: ignore[method-assign]

    await provider.handle_async_init()

    cache.delete.assert_awaited_once_with("_get_account", provider=provider.instance_id)
    provider.login.assert_awaited_once()
