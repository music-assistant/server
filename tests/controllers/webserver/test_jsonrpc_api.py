"""Tests for the JSON-RPC API command endpoint."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

from music_assistant_models.errors import PlayerUnavailableError

from music_assistant.constants import VERBOSE_LOG_LEVEL
from music_assistant.helpers.api import APICommandHandler

if TYPE_CHECKING:
    import pytest

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


async def test_verbose_log_hides_secrets_of_commands(
    webserver: WebserverController, caplog: pytest.LogCaptureFixture
) -> None:
    """The verbose log shows a received command without its secrets."""

    async def login() -> bool:
        return True

    webserver.auth = MagicMock(has_users=True)
    webserver.mass.command_handlers["test/login"] = APICommandHandler.parse(
        "test/login", login, authenticated=False
    )
    request = MagicMock(
        can_read_body=True,
        remote="127.0.0.1",
        headers={},
        read=AsyncMock(
            return_value=b'{"message_id": "1", "command": "test/login", '
            b'"args": {"username": "someone", "password": "made-up-password"}}'
        ),
    )

    with (
        caplog.at_level(VERBOSE_LOG_LEVEL, logger=webserver.logger.name),
        patch.object(webserver.mass.translations, "ensure_locale_loaded", AsyncMock(), create=True),
    ):
        response = await webserver._handle_jsonrpc_api_command(request)

    assert response.status == 200
    assert "made-up-password" not in caplog.text
    assert '"command":"test/login"' in caplog.text
    assert '"username":"someone","password":"<redacted>"' in caplog.text


async def test_invalid_json_is_rejected_without_logging_it(
    webserver: WebserverController, caplog: pytest.LogCaptureFixture
) -> None:
    """A request with invalid JSON is rejected without logging what it sent."""
    webserver.auth = MagicMock(has_users=True)
    request = MagicMock(
        can_read_body=True,
        remote="127.0.0.1",
        read=AsyncMock(return_value=b'{"password": "made-up-password"'),
    )

    with caplog.at_level(logging.ERROR, logger=webserver.logger.name):
        response = await webserver._handle_jsonrpc_api_command(request)

    assert response.status == 400
    assert response.text == "Invalid JSON"
    assert "made-up-password" not in caplog.text
