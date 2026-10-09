"""Tests for the JSON-RPC API command endpoint."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

from music_assistant_models.errors import PlayerUnavailableError

from music_assistant.helpers.api import APICommandHandler

if TYPE_CHECKING:
    from music_assistant.controllers.webserver.controller import WebserverController


async def test_unavailable_player_returns_not_found(webserver: WebserverController) -> None:
    """Test a command for an unavailable player is answered with 404 instead of a server error."""

    async def raise_unavailable() -> None:
        raise PlayerUnavailableError("Player p1 is not available")

    webserver.auth = MagicMock(has_users=True)
    webserver.mass.command_handlers["test/raise"] = APICommandHandler.parse(
        "test/raise", raise_unavailable
    )
    request = MagicMock(
        can_read_body=True,
        remote="127.0.0.1",
        read=AsyncMock(return_value=b'{"message_id": "1", "command": "test/raise", "args": {}}'),
    )

    with patch.object(webserver, "_authenticate_api_command", AsyncMock(return_value=None)):
        response = await webserver._handle_jsonrpc_api_command(request)

    assert response.status == 404
    assert response.text == "Player p1 is not available"
