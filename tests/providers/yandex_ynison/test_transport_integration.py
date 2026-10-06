"""Ynison handshakes and recovery against real local WebSocket endpoints."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import patch

import aiohttp
import pytest
from aiohttp import web
from ya_passport_auth import SecretStr

from music_assistant.providers.yandex_ynison.ynison_client import (
    YnisonClient,
    YnisonDeviceInfo,
    YnisonState,
)

if TYPE_CHECKING:
    from aiohttp.test_utils import TestServer


@pytest.mark.integration
@pytest.mark.parametrize("callback_fault", [False, True])
async def test_real_websocket_recovery_registers_fresh_state(
    aiohttp_server: Callable[[web.Application], Awaitable[TestServer]],
    callback_fault: bool,
) -> None:
    """A dropped socket or callback error reconnects without replaying old player state."""
    connections: list[list[dict[str, Any]]] = []
    received_tracks: list[str | None] = []
    recovered = asyncio.Event()
    finish = asyncio.Event()

    async def on_state(state: YnisonState) -> None:
        received_tracks.append(state.current_track_id)
        if callback_fault and len(received_tracks) == 1:
            raise ValueError("deliberate local callback fault")
        if state.current_track_id == "recovered":
            recovered.set()

    app = _make_ynison_app(connections, finish, callback_fault)
    server = await aiohttp_server(app)
    async with aiohttp.ClientSession() as session:
        connect_ws = session.ws_connect

        async def local_ws(url: str, **kwargs: Any) -> aiohttp.ClientWebSocketResponse:
            # The production state URL requires TLS; this test binds only loopback.
            return cast(
                "aiohttp.ClientWebSocketResponse",
                await connect_ws(url.replace("wss://", "ws://", 1), **kwargs),
            )

        client = YnisonClient(
            SecretStr("local-test-token"),
            YnisonDeviceInfo("local-device", "Test player"),
            on_state,
            logging.getLogger(__name__),
            http_session=session,
        )
        with (
            patch(
                "music_assistant.providers.yandex_ynison.ynison_client.YNISON_REDIRECT_URL",
                str(server.make_url("/redirect")),
            ),
            patch(
                "music_assistant.providers.yandex_ynison.ynison_client.YNISON_STATE_PATH", "/state"
            ),
            patch("music_assistant.providers.yandex_ynison.ynison_client.RECONNECT_DELAYS", [0.5]),
            patch.object(session, "ws_connect", side_effect=local_ws),
        ):
            try:
                await client.connect()
                first_message_task = client._message_task
                await asyncio.wait_for(recovered.wait(), timeout=5)
                assert received_tracks == ["first", "recovered"]
                assert client.connected
                assert client.state.current_track_id == "recovered"
                assert len(connections) == 2
                for registration in connections:
                    full_state = registration[0]["update_full_state"]
                    assert full_state["is_currently_active"] is False
                    assert full_state["player_state"]["status"]["paused"] is True
                    assert full_state["player_state"]["player_queue"]["playable_list"] == []
                    assert "update_session_params" in registration[1]
                if callback_fault:
                    assert first_message_task is not None
                    with pytest.raises(ValueError, match="deliberate local callback fault"):
                        await first_message_task
            finally:
                finish.set()
                await client.disconnect()
            assert not session.closed
            assert not client.connected


def _make_ynison_app(
    connections: list[list[dict[str, Any]]], finish: asyncio.Event, callback_fault: bool
) -> web.Application:
    """Serve redirector and state-service contracts on one loopback endpoint."""

    async def redirect(request: web.Request) -> web.WebSocketResponse:
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        await socket.send_json(
            {"host": request.host, "redirect_ticket": "local-ticket", "session_id": 1}
        )
        await socket.close()
        return socket

    async def state_service(request: web.Request) -> web.WebSocketResponse:
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        connections.append([await socket.receive_json(), await socket.receive_json()])
        track = "first" if len(connections) == 1 else "recovered"
        await socket.send_json(
            {
                "player_state": {
                    "status": {"paused": False, "progress_ms": "0", "duration_ms": "1000"},
                    "player_queue": {
                        "current_playable_index": 0,
                        "playable_list": [{"playable_id": track}],
                    },
                }
            }
        )
        if len(connections) == 1:
            if callback_fault:
                async for _message in socket:
                    pass
            else:
                await socket.close()
        else:
            await finish.wait()
            await socket.close()
        return socket

    app = web.Application()
    app.router.add_get("/redirect", redirect)
    app.router.add_get("/state", state_service)
    return app
