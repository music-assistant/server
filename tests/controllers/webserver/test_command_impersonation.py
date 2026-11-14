"""Tests for the user argument of impersonation-enabled commands on the JSON-RPC API."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.streams import StreamReader
from aiohttp.test_utils import make_mocked_request
from music_assistant_models.auth import Scope, UserRole

from music_assistant.controllers.webserver import controller as webserver_controller
from music_assistant.controllers.webserver.helpers.auth_middleware import (
    get_current_user,
    set_impersonated_user,
)
from music_assistant.helpers.api import APICommandHandler
from music_assistant.helpers.json import json_dumps

if TYPE_CHECKING:
    from music_assistant.controllers.webserver.controller import WebserverController


async def _acting_user_command() -> str | None:
    """Test command target that tells which user it ran as."""
    user = get_current_user()
    return user.user_id if user else None


def _api_request(body: dict[str, object]) -> web.Request:
    payload = StreamReader(MagicMock(), 2**16, loop=asyncio.get_running_loop())
    payload.feed_data(json_dumps(body).encode())
    payload.feed_eof()
    return make_mocked_request("POST", "/api", payload=payload, app=web.Application())


@pytest.mark.parametrize(
    ("required_scope", "status"),
    [
        pytest.param(Scope.LIBRARY_READ, 200, id="target_holds_the_scope"),
        pytest.param(Scope.LIBRARY_WRITE, 403, id="target_lacks_the_scope"),
    ],
)
async def test_jsonrpc_command_runs_as_the_impersonated_user(
    webserver: WebserverController, required_scope: Scope, status: int
) -> None:
    """The service caller acts as the guest, but only within the guest's own scopes."""
    mass = webserver.mass
    mass.music = MagicMock()
    mass.music.database.execute = AsyncMock()
    mass.music.database.commit = AsyncMock()
    mass.translations.ensure_locale_loaded = AsyncMock()  # type: ignore[method-assign]
    await webserver.auth.setup()
    service = await webserver.auth.create_user(username="ha_service", role=UserRole.SERVICE)
    guest = await webserver.auth.create_user(username="guest", role=UserRole.GUEST)
    mass.command_handlers["test/protected"] = APICommandHandler.parse(
        "test/protected",
        _acting_user_command,
        required_scope=required_scope,
        allow_impersonation=True,
    )
    request = _api_request(
        {"message_id": "1", "command": "test/protected", "args": {"user": "guest"}}
    )

    try:
        with patch.object(
            webserver_controller, "get_authenticated_user", AsyncMock(return_value=service)
        ):
            response = await webserver._handle_jsonrpc_api_command(request)
    finally:
        set_impersonated_user(None)
        await webserver.auth.close()

    assert response.status == status
    assert response.text is not None
    if status == 200:
        assert guest.user_id in response.text
    else:
        assert "impersonated user lacks" in response.text
