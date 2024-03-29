"""Connect o a remote Music Assistant Server using the default Websocket API."""

from __future__ import annotations

import logging
import pprint
from typing import Any

from aiohttp import ClientSession, ClientWebSocketResponse, WSMsgType, client_exceptions

from music_assistant.client.exceptions import (
    CannotConnect,
    ConnectionClosed,
    ConnectionFailed,
    InvalidMessage,
    InvalidState,
    NotConnected,
)
from music_assistant.common.helpers.json import json_dumps, json_loads

LOGGER = logging.getLogger(f"{__package__}.connection")


def get_websocket_url(url: str) -> str:
    """Extract Websocket URL from (base) Music Assistant URL."""
    if not url or "://" not in url:
        msg = f"{url} is not a valid url"
        raise RuntimeError(msg)
    ws_url = url.replace("http", "ws")
    if not ws_url.endswith("/ws"):
        ws_url += "/ws"
    return ws_url.replace("//ws", "/ws")


class WebsocketsConnection:
    """Websockets connection to a Music Assistant Server."""

    def __init__(self, server_url: str, aiohttp_session: ClientSession | None) -> None:
        """Initialize."""
        self.ws_server_url = get_websocket_url(server_url)
        self._aiohttp_session_provided = aiohttp_session is not None
        self._aiohttp_session = aiohttp_session or ClientSession()
        self._ws_client: ClientWebSocketResponse | None = None

    @property
    def connected(self) -> bool:
        """Return if we're currently connected."""
        return self._ws_client is not None and not self._ws_client.closed

    async def connect(self) -> dict[str, Any]:
        """Connect to the websocket server and return the first message (server info)."""
        if self._aiohttp_session is None:
            self._aiohttp_session = ClientSession()
        if self._ws_client is not None:
            msg = "Already connected"
            raise InvalidState(msg)

        LOGGER.debug("Trying to connect")
        try:
            self._ws_client = await self._aiohttp_session.ws_connect(
                self.ws_server_url,
                heartbeat=55,
                compress=15,
                max_msg_size=0,
            )
            # receive first server info message
            return await self.receive_message()
        except (
            client_exceptions.WSServerHandshakeError,
            client_exceptions.ClientError,
        ) as err:
            raise CannotConnect(err) from err

    async def disconnect(self) -> None:
        """Disconnect the client."""
        LOGGER.debug("Closing client connection")
        if self._ws_client is not None and not self._ws_client.closed:
            await self._ws_client.close()
        self._ws_client = None
        if self._aiohttp_session and not self._aiohttp_session_provided:
            await self._aiohttp_session.close()
            self._aiohttp_session = None

    async def receive_message(self) -> dict[str, Any]:
        """Receive the next message from the server (or raise on error)."""
        assert self._ws_client
        ws_msg = await self._ws_client.receive()

        if ws_msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.CLOSING):
            msg = "Connection was closed."
            raise ConnectionClosed(msg)

        if ws_msg.type == WSMsgType.ERROR:
            raise ConnectionFailed

        if ws_msg.type != WSMsgType.TEXT:
            msg = f"Received non-Text message: {ws_msg.type}"
            raise InvalidMessage(msg)

        try:
            msg = json_loads(ws_msg.data)
        except TypeError as err:
            msg = f"Received unsupported JSON: {err}"
            raise InvalidMessage(msg) from err
        except ValueError as err:
            msg = "Received invalid JSON."
            raise InvalidMessage(msg) from err

        if LOGGER.isEnabledFor(logging.DEBUG):
            LOGGER.debug("Received message:\n%s\n", pprint.pformat(ws_msg))

        return msg

    async def send_message(self, message: dict[str, Any]) -> None:
        """
        Send a message to the server.

        Raises NotConnected if client not connected.
        """
        if not self.connected:
            raise NotConnected

        if LOGGER.isEnabledFor(logging.DEBUG):
            LOGGER.debug("Publishing message:\n%s\n", pprint.pformat(message))

        assert self._ws_client
        assert isinstance(message, dict)

        await self._ws_client.send_json(message, dumps=json_dumps)

    def __repr__(self) -> str:
        """Return the representation."""
        prefix = "" if self.connected else "not "
        return f"{type(self).__name__}(ws_server_url={self.ws_server_url!r}, {prefix}connected)"
