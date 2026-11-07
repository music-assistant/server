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
    fault_logged = asyncio.Event()
    logger = logging.getLogger(__name__)

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
            logger,
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
            patch.object(
                logger, "error", side_effect=lambda *_args, **_kwargs: fault_logged.set()
            ) as log_error,
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
                    await asyncio.wait_for(fault_logged.wait(), timeout=1)
                    assert isinstance(log_error.call_args.kwargs["exc_info"][1], ValueError)
                    assert first_message_task is not None
                    with pytest.raises(ValueError, match="deliberate local callback fault"):
                        await first_message_task
            finally:
                finish.set()
                await client.disconnect()
            assert not session.closed
            assert not client.connected


@pytest.mark.integration
async def test_initial_empty_redirect_recovers_with_refreshed_token(
    aiohttp_server: Callable[[web.Application], Awaitable[TestServer]],
) -> None:
    """The redirector's invalid-token response cannot strand a cold-start client."""
    connections: list[list[dict[str, Any]]] = []
    finish = asyncio.Event()
    connected = asyncio.Event()
    refresh_count = 0

    async def refresh() -> SecretStr:
        nonlocal refresh_count
        refresh_count += 1
        return SecretStr("fresh-test-token")

    async def on_state(_state: YnisonState) -> None:
        connected.set()

    app = _make_ynison_app(connections, finish, True, rejected_token="expired-test-token")
    server = await aiohttp_server(app)
    async with aiohttp.ClientSession() as session:
        connect_ws = session.ws_connect

        async def local_ws(url: str, **kwargs: Any) -> aiohttp.ClientWebSocketResponse:
            return cast(
                "aiohttp.ClientWebSocketResponse",
                await connect_ws(url.replace("wss://", "ws://", 1), **kwargs),
            )

        client = YnisonClient(
            SecretStr("expired-test-token"),
            YnisonDeviceInfo("local-device", "Test player"),
            on_state,
            logging.getLogger(__name__),
            http_session=session,
            on_auth_failure=refresh,
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
                await asyncio.wait_for(connected.wait(), timeout=5)
                assert client.connected
                assert refresh_count == 1
                assert len(connections) == 1
            finally:
                finish.set()
                await client.disconnect()
            assert not session.closed


@pytest.mark.integration
async def test_server_go_away_defers_reconnect_for_requested_window(
    aiohttp_server: Callable[[web.Application], Awaitable[TestServer]],
) -> None:
    """A throttled session is not redialled before the server's go-away window ends."""
    registrations = 0
    redialled = asyncio.Event()

    async def redirect(request: web.Request) -> web.WebSocketResponse:
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        await socket.send_json(
            {"host": request.host, "redirect_ticket": "local-ticket", "session_id": 1}
        )
        await socket.close()
        return socket

    async def state_service(request: web.Request) -> web.WebSocketResponse:
        nonlocal registrations
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        await socket.receive_json()
        await socket.receive_json()
        registrations += 1
        if registrations > 1:
            redialled.set()
        await socket.send_json(
            {
                "error": {
                    "details": {
                        "ynison-error-code": "500140000",
                        "ynison-go-away-for-seconds": "1",
                    },
                    "grpc_code": 14,
                    "http_code": 503,
                    "message": "Ynison service is temporary unavailable.",
                }
            }
        )
        await socket.close()
        return socket

    async def on_state(_state: YnisonState) -> None:
        return None

    app = web.Application()
    app.router.add_get("/redirect", redirect)
    app.router.add_get("/state", state_service)
    server = await aiohttp_server(app)
    async with aiohttp.ClientSession() as session:
        connect_ws = session.ws_connect

        async def local_ws(url: str, **kwargs: Any) -> aiohttp.ClientWebSocketResponse:
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
            patch("music_assistant.providers.yandex_ynison.ynison_client.RECONNECT_DELAYS", [0.05]),
            patch.object(session, "ws_connect", side_effect=local_ws),
        ):
            try:
                await client.connect()
                await asyncio.sleep(0.7)
                assert registrations == 1
                await asyncio.wait_for(redialled.wait(), timeout=3)
            finally:
                await client.disconnect()


def _make_ynison_app(
    connections: list[list[dict[str, Any]]],
    finish: asyncio.Event,
    callback_fault: bool,
    *,
    rejected_token: str | None = None,
) -> web.Application:
    """Serve redirector and state-service contracts on one loopback endpoint."""

    async def redirect(request: web.Request) -> web.WebSocketResponse:
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        if rejected_token and request.headers.get("Authorization") == f"OAuth {rejected_token}":
            await socket.send_json({})
        else:
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
