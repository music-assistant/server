"""Sendspin WebSocket proxy handler for Music Assistant.

This module provides an authenticated WebSocket proxy to the internal Sendspin server,
allowing web clients to connect through the main webserver instead of requiring direct
access to the Sendspin port.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import TYPE_CHECKING

from aiohttp import WSMsgType, web

from music_assistant.constants import MASS_LOGGER_NAME
from music_assistant.controllers.webserver.helpers.auth_middleware import (
    get_authenticated_user,
    is_request_from_ingress,
)

if TYPE_CHECKING:
    import aiohttp
    from music_assistant_models.auth import User

    from music_assistant.controllers.webserver import WebserverController

LOGGER = logging.getLogger(f"{MASS_LOGGER_NAME}.sendspin_proxy")
INTERNAL_SENDSPIN_URL = "ws://127.0.0.1:8927/sendspin"


class SendspinProxyHandler:
    """Handler for proxying WebSocket connections to the internal Sendspin server."""

    def __init__(self, webserver: WebserverController) -> None:
        """Initialize the Sendspin proxy handler.

        :param webserver: The webserver controller instance.
        """
        self.webserver = webserver
        self.mass = webserver.mass
        self.logger = LOGGER

    async def handle_sendspin_proxy(self, request: web.Request) -> web.WebSocketResponse:
        """
        Handle incoming WebSocket connection and proxy to internal Sendspin server.

        Authentication is required as the first message. The client must send:
        {"type": "auth", "token": "<access_token>"}

        After successful authentication, all messages are proxied bidirectionally.

        :param request: The incoming HTTP request to upgrade to WebSocket.
        :return: The WebSocket response.
        """
        wsock = web.WebSocketResponse(heartbeat=30)
        await wsock.prepare(request)

        self.logger.debug("Sendspin proxy connection from %s", request.remote)

        # Check for ingress authentication (HA handles auth via headers)
        if is_request_from_ingress(request):
            user = await get_authenticated_user(request)
            if not user:
                self.logger.warning(
                    "Ingress auth failed for sendspin proxy from %s", request.remote
                )
                await wsock.close(code=4001, message=b"Ingress authentication failed")
                return wsock
            self.logger.debug("Sendspin proxy authenticated via ingress: %s", user.username)
        else:
            # Regular auth via first message
            try:
                user = await self._authenticate(wsock)
                if not user:
                    return wsock
            except TimeoutError:
                self.logger.warning("Auth timeout for sendspin proxy from %s", request.remote)
                await wsock.close(code=4001, message=b"Authentication timeout")
                return wsock
            except Exception:
                self.logger.exception("Auth error for sendspin proxy")
                await wsock.close(code=4001, message=b"Authentication error")
                return wsock

        try:
            internal_ws = await self.mass.http_session.ws_connect(INTERNAL_SENDSPIN_URL)
        except Exception:
            self.logger.exception("Failed to connect to internal Sendspin server")
            await wsock.close(code=1011, message=b"Internal server error")
            return wsock

        self.logger.debug("Sendspin proxy authenticated and connected for %s", request.remote)

        try:
            await self._proxy_messages(wsock, internal_ws)
        finally:
            if not internal_ws.closed:
                await internal_ws.close()
            if not wsock.closed:
                await wsock.close()

        return wsock

    async def _authenticate(self, wsock: web.WebSocketResponse) -> User | None:
        """Wait for and validate authentication message.

        :param wsock: The client WebSocket connection.
        :return: The authenticated user, or None if authentication failed.
        """
        async with asyncio.timeout(10):
            msg = await wsock.receive()

        if msg.type != WSMsgType.TEXT:
            await wsock.close(code=4001, message=b"Expected text message for auth")
            return None

        try:
            auth_data = json.loads(msg.data)
        except json.JSONDecodeError:
            await wsock.close(code=4001, message=b"Invalid JSON in auth message")
            return None

        if auth_data.get("type") != "auth":
            await wsock.close(code=4001, message=b"First message must be auth")
            return None

        token = auth_data.get("token")
        if not token:
            await wsock.close(code=4001, message=b"Token required in auth message")
            return None

        user = await self.webserver.auth.authenticate_with_token(token)
        if not user:
            await wsock.close(code=4001, message=b"Invalid or expired token")
            return None

        # Auto-whitelist player for users with player filters
        client_id = auth_data.get("client_id")
        if client_id and user.player_filter and client_id not in user.player_filter:
            self.logger.debug(
                "Auto-whitelisting Sendspin player %s for user %s", client_id, user.username
            )
            new_filter = [*user.player_filter, client_id]
            await self.webserver.auth.update_user_filters(
                user, player_filter=new_filter, provider_filter=None
            )

        self.logger.debug("Sendspin proxy authenticated user: %s", user.username)
        await wsock.send_str('{"type": "auth_ok"}')
        return user

    async def _proxy_messages(
        self,
        client_ws: web.WebSocketResponse,
        internal_ws: aiohttp.ClientWebSocketResponse,
    ) -> None:
        """
        Proxy messages bidirectionally between client and internal Sendspin server.

        :param client_ws: The client WebSocket connection.
        :param internal_ws: The internal Sendspin server WebSocket connection.
        """
        client_to_internal = asyncio.create_task(
            self._forward_client_to_internal(client_ws, internal_ws)
        )
        internal_to_client = asyncio.create_task(
            self._forward_internal_to_client(client_ws, internal_ws)
        )

        _done, pending = await asyncio.wait(
            [client_to_internal, internal_to_client],
            return_when=asyncio.FIRST_COMPLETED,
        )

        for task in pending:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _forward_client_to_internal(
        self,
        client_ws: web.WebSocketResponse,
        internal_ws: aiohttp.ClientWebSocketResponse,
    ) -> None:
        """
        Forward messages from client to internal Sendspin server.

        :param client_ws: The client WebSocket connection.
        :param internal_ws: The internal Sendspin server WebSocket connection.
        """
        async for msg in client_ws:
            if msg.type == WSMsgType.TEXT:
                await internal_ws.send_str(msg.data)
            elif msg.type == WSMsgType.BINARY:
                await internal_ws.send_bytes(msg.data)
            elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR):
                break

    async def _forward_internal_to_client(
        self,
        client_ws: web.WebSocketResponse,
        internal_ws: aiohttp.ClientWebSocketResponse,
    ) -> None:
        """
        Forward messages from internal Sendspin server to client.

        :param client_ws: The client WebSocket connection.
        :param internal_ws: The internal Sendspin server WebSocket connection.
        """
        async for msg in internal_ws:
            if msg.type == WSMsgType.TEXT:
                await client_ws.send_str(msg.data)
            elif msg.type == WSMsgType.BINARY:
                await client_ws.send_bytes(msg.data)
            elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR):
                break
