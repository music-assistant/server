"""Tests for the HTTP logout endpoint."""

from __future__ import annotations

import hashlib
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from music_assistant_models.auth import User, UserRole

from music_assistant.controllers.webserver import controller as controller_module
from music_assistant.controllers.webserver.controller import WebserverController

_USER = User(user_id="user_1", username="listener", role=UserRole.USER)
_TOKEN = "LOCAL_TEST_TOKEN"
_TOKEN_HASH = hashlib.sha256(_TOKEN.encode()).hexdigest()
_TOKEN_ROW = {"token_id": "token-a", "token_hash": _TOKEN_HASH}
_BEARER = {"Authorization": f"Bearer {_TOKEN}"}


async def test_logout_revokes_the_token_and_closes_its_sessions(
    webserver: WebserverController,
) -> None:
    """Logging out deletes the token and disconnects the websocket sessions that use it."""
    response, database, disconnect = await _logout(webserver, _USER, _TOKEN_ROW, _BEARER)

    assert response.status == 200
    assert json.loads(response.text or "") == {"success": True}
    database.get_row.assert_awaited_once_with("auth_tokens", {"token_hash": _TOKEN_HASH})
    database.delete.assert_awaited_once_with("auth_tokens", {"token_id": "token-a"})
    disconnect.assert_called_once_with("token-a")


@pytest.mark.parametrize("headers", [_BEARER, {}], ids=["unknown_token", "no_bearer_header"])
async def test_logout_without_a_matching_token_revokes_nothing(
    webserver: WebserverController, headers: dict[str, str]
) -> None:
    """A logout whose token matches no row still succeeds without deleting or disconnecting."""
    response, database, disconnect = await _logout(webserver, _USER, None, headers)

    assert response.status == 200
    assert json.loads(response.text or "") == {"success": True}
    database.delete.assert_not_awaited()
    disconnect.assert_not_called()


async def test_logout_requires_authentication(webserver: WebserverController) -> None:
    """An unauthenticated logout is refused and leaves tokens and sessions alone."""
    response, database, disconnect = await _logout(webserver, None, _TOKEN_ROW, _BEARER)

    assert response.status == 401
    database.get_row.assert_not_awaited()
    database.delete.assert_not_awaited()
    disconnect.assert_not_called()


async def _logout(
    webserver: WebserverController,
    user: User | None,
    token_row: dict[str, str] | None,
    headers: dict[str, str],
) -> tuple[web.Response, AsyncMock, MagicMock]:
    """
    Post a logout request and return the response with the mocked database and disconnect.

    :param webserver: The controller handling the request.
    :param user: The user the request authenticates as, or None when it is not authenticated.
    :param token_row: The token row the database holds for the token's hash, if any.
    :param headers: The request headers.
    """
    database = AsyncMock()
    database.get_row.return_value = token_row
    request = make_mocked_request("POST", "/auth/logout", headers=headers, app=web.Application())
    with (
        patch.object(controller_module, "get_authenticated_user", new=AsyncMock(return_value=user)),
        patch.object(webserver.auth, "database", new=database),
        patch.object(webserver, "disconnect_websockets_for_token") as disconnect,
    ):
        response = await webserver._handle_auth_logout(request)
    return response, database, disconnect
