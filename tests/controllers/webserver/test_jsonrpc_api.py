"""Tests for the JSON-RPC API command endpoint."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from music_assistant.constants import VERBOSE_LOG_LEVEL
from music_assistant.controllers.webserver.controller import WebserverController
from music_assistant.helpers.api import APICommandHandler

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


@pytest.fixture
def webserver(mass_minimal: MusicAssistant) -> WebserverController:
    """Return a WebserverController with stubbed serialization dependencies."""
    mass_minimal.metadata = SimpleNamespace(  # type: ignore[assignment]
        compute_image_id=lambda provider, path: f"{provider}--{path}"
    )
    mass_minimal.translations = SimpleNamespace(  # type: ignore[assignment]
        get_translation=lambda _key, **_kwargs: None
    )
    webserver = WebserverController(mass_minimal)
    mass_minimal.webserver = webserver
    return webserver


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
