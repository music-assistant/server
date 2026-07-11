"""
Sendspin WebSocket proxy handler for Music Assistant.

This module provides an authenticated WebSocket proxy to the internal Sendspin server,
allowing web clients to connect through the main webserver instead of requiring direct
access to the Sendspin port.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

import aiohttp
from aiohttp import ClientConnectorError, WSMsgType, web

from music_assistant.constants import MASS_LOGGER_NAME
from music_assistant.controllers.webserver.helpers.auth_middleware import (
    get_authenticated_user,
    is_request_from_ingress,
)
from music_assistant.helpers.sendspin import (
    filter_audio_only_sendspin_message,
    get_sendspin_client_id,
)
from music_assistant.helpers.util import format_ip_for_url

if TYPE_CHECKING:
    from music_assistant_models.auth import User

    from music_assistant.controllers.webserver import WebserverController

LOGGER = logging.getLogger(f"{MASS_LOGGER_NAME}.sendspin_proxy")


@dataclass
class _SendspinConnectionContext:
    """Mutable state shared by both forwarding directions."""

    player_id: str | None = None


class SendspinProxyHandler:
    """Handler for proxying WebSocket connections to the internal Sendspin server."""

    def __init__(self, webserver: WebserverController) -> None:
        """
        Initialize the Sendspin proxy handler.

        :param webserver: The webserver controller instance.
        """
        self.webserver = webserver
        self.mass = webserver.mass
        self.logger = LOGGER

    @property
    def internal_sendspin_url(self) -> str:
        """Return the internal sendspin URL for connecting to the internal Sendspin server."""
        # Connect via localhost since the proxy and Sendspin server run in the same process
        # If the server binds to 0.0.0.0 (all interfaces), use localhost for efficiency
        # Otherwise use the actual bind IP in case it's configured to a specific interface
        bind_ip = self.mass.streams.bind_ip
        if bind_ip == "0.0.0.0":
            # Use IPv6 loopback if publish_ip is IPv6 (indicates IPv6-only host)
            publish_ip = str(self.mass.streams.publish_ip)
            connect_ip = "::1" if ":" in publish_ip else "127.0.0.1"
        else:
            connect_ip = bind_ip
        return f"ws://{format_ip_for_url(connect_ip)}:8927/sendspin"

    async def handle_sendspin_proxy(self, request: web.Request) -> web.WebSocketResponse:
        """
        Handle incoming WebSocket connection and proxy to internal Sendspin server.

        Authentication is required as the first message. The client must send:
        {"type": "auth", "token": "<access_token>"}

        After successful authentication, all messages are proxied bidirectionally.

        :param request: The incoming HTTP request to upgrade to WebSocket.
        :return: The WebSocket response.
        """
        wsock = web.WebSocketResponse(heartbeat=25)
        await wsock.prepare(request)

        self.logger.debug("Sendspin proxy connection from %s", request.remote)

        # Check for ingress authentication (HA handles auth via headers)
        sendspin_player_id: str | None = None
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
                user, sendspin_player_id = await self._authenticate(wsock)
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

        # The internal Sendspin server may not be ready yet during startup
        # (it starts in the provider load phase, after the webserver).
        # Retry a few times with backoff to handle this race condition.
        try:
            internal_ws = None
            for attempt in range(5):
                try:
                    internal_ws = await self.mass.http_session.ws_connect(
                        self.internal_sendspin_url
                    )
                    break
                except ClientConnectorError:
                    if attempt < 4:
                        await asyncio.sleep(0.5 * (attempt + 1))
                        continue
                    self.logger.exception("Failed to connect to internal Sendspin server")
                    await wsock.close(code=1011, message=b"Internal server error")
                    return wsock
            if internal_ws is None:
                raise RuntimeError("Retry loop exited without connecting or returning")
        except Exception:
            self.logger.exception("Failed to connect to internal Sendspin server")
            await wsock.close(code=1011, message=b"Internal server error")
            return wsock
        self.logger.debug("Sendspin proxy authenticated and connected for %s", request.remote)

        try:
            await self._proxy_messages(wsock, internal_ws, sendspin_player_id)
        finally:
            if not internal_ws.closed:
                await internal_ws.close()
            if not wsock.closed:
                await wsock.close()

        return wsock

    async def _authenticate(self, wsock: web.WebSocketResponse) -> tuple[User | None, str | None]:
        """
        Wait for and validate authentication message.

        :param wsock: The client WebSocket connection.
        :return: The authenticated user and Sendspin player ID.
        """
        async with asyncio.timeout(10):
            msg = await wsock.receive()

        if msg.type != WSMsgType.TEXT:
            await wsock.close(code=4001, message=b"Expected text message for auth")
            return None, None

        try:
            auth_data = json.loads(msg.data)
        except json.JSONDecodeError:
            await wsock.close(code=4001, message=b"Invalid JSON in auth message")
            return None, None

        if auth_data.get("type") != "auth":
            await wsock.close(code=4001, message=b"First message must be auth")
            return None, None

        token = auth_data.get("token")
        if not token:
            await wsock.close(code=4001, message=b"Token required in auth message")
            return None, None

        user = await self.webserver.auth.authenticate_with_token(token)
        if not user:
            await wsock.close(code=4001, message=b"Invalid or expired token")
            return None, None

        # Set the sendspin player_id on the user's websocket client(s)
        # This allows the player controller to auto-whitelist this (web)player
        # without modifying the user's player_filter list
        client_id = auth_data.get("client_id")
        if client_id:
            self.webserver.set_sendspin_player_for_user(user.user_id, client_id)
            self.logger.debug("Registered sendspin player %s for user %s", client_id, user.username)

        self.logger.debug("Sendspin proxy authenticated user: %s", user.username)
        await wsock.send_str('{"type": "auth_ok"}')
        return user, client_id if isinstance(client_id, str) else None

    async def _proxy_messages(
        self,
        client_ws: web.WebSocketResponse,
        internal_ws: aiohttp.ClientWebSocketResponse,
        player_id: str | None = None,
    ) -> None:
        """
        Proxy messages bidirectionally between client and internal Sendspin server.

        :param client_ws: The client WebSocket connection.
        :param internal_ws: The internal Sendspin server WebSocket connection.
        :param player_id: The authenticated Sendspin player ID, if known.
        """
        context = _SendspinConnectionContext(player_id)
        client_to_internal = asyncio.create_task(
            self._forward_client_to_internal(client_ws, internal_ws, context)
        )
        internal_to_client = asyncio.create_task(
            self._forward_internal_to_client(client_ws, internal_ws, context)
        )

        done, pending = await asyncio.wait(
            [client_to_internal, internal_to_client],
            return_when=asyncio.FIRST_COMPLETED,
        )

        for task in pending:
            task.cancel()
        peer_results = await asyncio.gather(*pending, return_exceptions=True)

        # collect everything first so cleanup failures cannot mask the primary error
        unexpected: list[BaseException] = []
        for task in done:
            with contextlib.suppress(asyncio.CancelledError):
                if exc := task.exception():
                    self._collect_proxy_exception(exc, unexpected)
        for result in peer_results:
            if isinstance(result, BaseException):
                self._collect_proxy_exception(result, unexpected)
        if not unexpected:
            return
        for extra in unexpected[1:]:
            self.logger.warning(
                "Additional Sendspin proxy error while forwarding: %s",
                extra,
            )
        raise unexpected[0]

    def _collect_proxy_exception(
        self,
        exc: BaseException,
        unexpected: list[BaseException],
    ) -> None:
        """Log expected transport disconnects; collect anything else."""
        if isinstance(exc, asyncio.CancelledError):
            return
        if isinstance(
            exc,
            (ConnectionError, aiohttp.ClientError, asyncio.IncompleteReadError, EOFError),
        ):
            self.logger.debug("Sendspin proxy connection closed while forwarding: %s", exc)
            return
        unexpected.append(exc)

    async def _forward_client_to_internal(
        self,
        client_ws: web.WebSocketResponse,
        internal_ws: aiohttp.ClientWebSocketResponse,
        context: _SendspinConnectionContext,
    ) -> None:
        """
        Forward messages from client to internal Sendspin server.

        :param client_ws: The client WebSocket connection.
        :param internal_ws: The internal Sendspin server WebSocket connection.
        :param context: Shared connection state.
        """
        async for msg in client_ws:
            if msg.type == WSMsgType.TEXT:
                if player_id := get_sendspin_client_id(msg.data):
                    context.player_id = player_id
                await internal_ws.send_str(msg.data)
            elif msg.type == WSMsgType.BINARY:
                await internal_ws.send_bytes(msg.data)
            elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR):
                if msg.type == WSMsgType.ERROR:
                    self.logger.debug("Sendspin proxy client transport error: %s", msg.data)
                break

    async def _forward_internal_to_client(
        self,
        client_ws: web.WebSocketResponse,
        internal_ws: aiohttp.ClientWebSocketResponse,
        context: _SendspinConnectionContext,
    ) -> None:
        """
        Forward messages from internal Sendspin server to client.

        :param client_ws: The client WebSocket connection.
        :param internal_ws: The internal Sendspin server WebSocket connection.
        :param context: Shared connection state.
        """
        async for msg in internal_ws:
            if msg.type == WSMsgType.TEXT:
                data = self._filter_outbound_message(context, msg.data)
                if isinstance(data, str):
                    await client_ws.send_str(data)
            elif msg.type == WSMsgType.BINARY:
                data = self._filter_outbound_message(context, msg.data)
                if isinstance(data, bytes):
                    await client_ws.send_bytes(data)
            elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR):
                if msg.type == WSMsgType.ERROR:
                    self.logger.debug("Sendspin proxy internal transport error: %s", msg.data)
                break

    def _filter_outbound_message(
        self, context: _SendspinConnectionContext, message: str | bytes
    ) -> str | bytes | None:
        """Return the outbound message allowed for this connection."""
        if context.player_id is None or not self.mass.players.is_player_audio_only(
            context.player_id
        ):
            return message
        return filter_audio_only_sendspin_message(message)
