"""Tests for the POST endpoints the AriaCast receiver exposes on the LAN."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock

import aiohttp
from aiohttp import web

from music_assistant.providers.ariacast_receiver import AriaCastReceiver

if TYPE_CHECKING:
    import pytest

SENDER = "192.168.1.10"
SECRET = "/opt/music-assistant/internal.py, line 42: boom"


def _receiver(cmd_play: AsyncMock | None = None) -> SimpleNamespace:
    """Build a bare receiver namespace for driving the HTTP handlers."""
    return SimpleNamespace(
        mass=MagicMock(),
        logger=MagicMock(),
        _apply_meta=AsyncMock(),
        _cmd_play=cmd_play or AsyncMock(),
        _cmd_pause=AsyncMock(),
        _forward_action=AsyncMock(),
    )


def _request(body: Any = None, error: Exception | None = None) -> SimpleNamespace:
    """Build a bare request with the sender as its peer."""
    json_mock = AsyncMock(side_effect=error) if error else AsyncMock(return_value=body or {})
    return SimpleNamespace(remote=SENDER, json=json_mock)


async def _metadata(receiver: SimpleNamespace, request: SimpleNamespace) -> web.Response:
    """Run the POST /metadata handler on the bare receiver."""
    return await AriaCastReceiver._http_metadata(
        cast("AriaCastReceiver", receiver), cast("web.Request", request)
    )


async def _command(receiver: SimpleNamespace, request: SimpleNamespace) -> web.Response:
    """Run the POST /api/command handler on the bare receiver."""
    return await AriaCastReceiver._http_command(
        cast("AriaCastReceiver", receiver), cast("web.Request", request)
    )


async def test_metadata_handler_does_not_echo_the_exception() -> None:
    """A failing metadata request tells the caller nothing about our internals."""
    receiver = _receiver()
    request = _request(error=ValueError(SECRET))

    response = await _metadata(receiver, request)

    assert response.status == 400
    assert SECRET not in (response.text or "")
    assert receiver.logger.debug.call_args.kwargs["exc_info"] is True


async def test_command_handler_does_not_echo_the_exception() -> None:
    """A failing command request tells the caller nothing about our internals."""
    receiver = _receiver(cmd_play=AsyncMock(side_effect=RuntimeError(SECRET)))
    request = _request(body={"action": "play"})

    response = await _command(receiver, request)

    assert response.status == 400
    assert SECRET not in (response.text or "")
    assert receiver.logger.debug.call_args.kwargs["exc_info"] is True


async def test_metadata_handler_forwards_the_peer_address() -> None:
    """The peer that posted the metadata is threaded through to the merge."""
    receiver = _receiver()
    request = _request(body={"data": {"title": "Test Track"}})

    response = await _metadata(receiver, request)

    assert response.status == 200
    assert receiver._apply_meta.await_args.args == ({"title": "Test Track"}, SENDER)


async def test_ws_metadata_handler_forwards_the_peer_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The peer on the metadata WebSocket is threaded through to the merge too."""
    receiver = _receiver()
    receiver._meta_sockets = set()
    receiver._meta_dict = MagicMock(return_value={})
    update = SimpleNamespace(
        type=aiohttp.WSMsgType.TEXT,
        data=json.dumps({"type": "update", "data": {"title": "Test Track"}}),
    )
    ws = MagicMock()
    ws.prepare = AsyncMock()
    ws.send_json = AsyncMock()
    ws.__aiter__.return_value = [update]
    monkeypatch.setattr(web, "WebSocketResponse", lambda: ws)

    await AriaCastReceiver._ws_metadata(
        cast("AriaCastReceiver", receiver), cast("web.Request", _request())
    )

    assert receiver._apply_meta.await_args.args == ({"title": "Test Track"}, SENDER)
