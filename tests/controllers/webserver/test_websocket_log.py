"""Tests for the websocket API message log."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp import WSMessage, WSMsgType, web
from aiohttp.test_utils import make_mocked_request
from music_assistant_models.api import CommandMessage

from music_assistant.constants import VERBOSE_LOG_LEVEL
from music_assistant.controllers.webserver.websocket_client import WebsocketClientHandler
from music_assistant.helpers.api import APICommandHandler

if TYPE_CHECKING:
    import pytest

    from music_assistant.controllers.webserver.controller import WebserverController

# made-up values, none of them is a real credential
FAKE_PASSWORD = "made-up-password"
FAKE_JWT = "eyJmYWtl.eyJmYWtlLXBheWxvYWQ.ZmFrZS1zaWduYXR1cmU"


async def _login() -> dict[str, Any]:
    """Test command that answers with an access token."""
    return {"success": True, "access_token": FAKE_JWT}


async def _failing_save() -> None:
    """Test command that fails unexpectedly."""
    raise RuntimeError("Unexpected failure")


def _create_client(webserver: WebserverController) -> WebsocketClientHandler:
    """Create a websocket client handler for a mocked request."""
    webserver.auth = MagicMock(has_users=True)
    return WebsocketClientHandler(
        webserver, make_mocked_request("GET", "/ws", app=web.Application())
    )


async def _handle_frame(webserver: WebserverController, frame: str, replies: int) -> None:
    """
    Run a websocket connection that receives one text frame, until it sent its replies.

    :param webserver: WebserverController the connection belongs to.
    :param frame: The text frame the client sends.
    :param replies: Number of messages the server sends before the client disconnects.
    """
    client = _create_client(webserver)
    replied = asyncio.Event()
    sent: list[str] = []

    async def send_str(data: str) -> None:
        sent.append(data)
        if len(sent) >= replies:
            replied.set()

    frames = iter([WSMessage(WSMsgType.TEXT, frame, None)])

    async def receive() -> WSMessage:
        if (message := next(frames, None)) is not None:
            return message
        await asyncio.wait_for(replied.wait(), 5)
        return WSMessage(WSMsgType.CLOSE, None, None)

    with (
        patch.object(webserver.mass, "dashboard", MagicMock(), create=True),
        patch.object(client.wsock, "prepare", AsyncMock()),
        patch.object(client.wsock, "close", AsyncMock()),
        patch.object(client.wsock, "receive", side_effect=receive),
        patch.object(client.wsock, "send_str", side_effect=send_str),
    ):
        await client.handle_client()


async def test_verbose_log_hides_secrets_of_commands_and_results(
    webserver: WebserverController, caplog: pytest.LogCaptureFixture
) -> None:
    """The verbose log shows the messages in both directions without their secrets."""
    webserver.mass.command_handlers["test/login"] = APICommandHandler.parse(
        "test/login", _login, authenticated=False
    )
    frame = CommandMessage(
        message_id="1",
        command="test/login",
        args={"username": "someone", "password": FAKE_PASSWORD},
    ).to_json()

    with caplog.at_level(VERBOSE_LOG_LEVEL, logger=webserver.logger.name):
        # the server info and the command result
        await _handle_frame(webserver, frame, replies=2)

    assert FAKE_PASSWORD not in caplog.text
    assert FAKE_JWT not in caplog.text
    assert '"command":"test/login"' in caplog.text
    assert '"username":"someone","password":"<redacted>"' in caplog.text
    assert '"access_token":"<redacted>"' in caplog.text


async def test_failed_command_log_hides_secrets(
    webserver: WebserverController, caplog: pytest.LogCaptureFixture
) -> None:
    """The debug log of a failed command shows the command without its secrets."""
    client = _create_client(webserver)
    handler = APICommandHandler.parse("test/save", _failing_save, authenticated=False)
    msg = CommandMessage(
        message_id="1",
        command="test/save",
        args={"provider_domain": "demo", "values": {"password": FAKE_PASSWORD}},
    )

    with (
        caplog.at_level(logging.DEBUG, logger=webserver.logger.name),
        patch.object(client, "_send_message", AsyncMock()),
    ):
        await client._run_handler(handler, msg)

    assert FAKE_PASSWORD not in caplog.text
    assert "command='test/save'" in caplog.text
    assert "'values': {'password': '<redacted>'}" in caplog.text


async def test_invalid_json_warning_leaves_out_the_message(
    webserver: WebserverController, caplog: pytest.LogCaptureFixture
) -> None:
    """A connection that sends invalid JSON is closed without logging what it sent."""
    with caplog.at_level(logging.WARNING, logger=webserver.logger.name):
        await _handle_frame(webserver, f'{{"password": "{FAKE_PASSWORD}"', replies=1)

    assert FAKE_PASSWORD not in caplog.text
    assert "Disconnected: Received invalid JSON" in caplog.text
