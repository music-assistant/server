"""
Yandex Glagol — local WebSocket client for Yandex Station.

Adapted from AlexxIT/YandexStation (MIT license).
Stripped of Home Assistant dependencies; uses pure aiohttp + asyncio.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import logging
import time
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from aiohttp import (
    ClientConnectorError,
    ClientWebSocketResponse,
    ServerTimeoutError,
    WSMsgType,
)

from .constants import (
    WS_COMMAND_TIMEOUT,
    WS_HEARTBEAT,
    WS_RECONNECT_BASE_DELAY,
    WS_RECONNECT_MAX_MULTIPLIER,
)

if TYPE_CHECKING:
    from ya_passport_auth import PassportClient, SecretStr

    from .session import YandexSession

PROV_LOGGER_BASE = "music_assistant.Yandex Station"
_LOGGER = logging.getLogger(f"{PROV_LOGGER_BASE}.glagol")


class YandexGlagol:
    """Local WebSocket client for Yandex Station via Glagol protocol."""

    def __init__(
        self, session: YandexSession, client: PassportClient, device: dict[str, Any]
    ) -> None:
        """Initialize Glagol client for a device."""
        self.session = session
        self._client = client
        self.device = device
        self._waiters: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._connect_task: asyncio.Task[None] | None = None
        self.device_token: SecretStr | None = None
        self.url: str | None = None
        self.ws: ClientWebSocketResponse | None = None
        self.update_handler: Callable[[dict[str, Any] | None], None] | None = None

    @property
    def name(self) -> str:
        """Return device display name."""
        return str(self.device.get("name", "Unknown"))

    @property
    def device_id(self) -> str:
        """Return Quasar device ID."""
        return str(self.device["quasar_info"]["device_id"])

    @property
    def platform(self) -> str:
        """Return device platform identifier."""
        return str(self.device["quasar_info"]["platform"])

    @property
    def connected(self) -> bool:
        """Return True if WebSocket is open."""
        return self.ws is not None and not self.ws.closed

    async def get_device_token(self) -> SecretStr:
        """Fetch a device token for WebSocket authentication."""
        _LOGGER.debug("[%s] Refreshing device token", self.name)
        await self.session.ensure_music_token()
        if not self.session.music_token:
            msg = "No music token available to fetch device token"
            raise RuntimeError(msg)
        return await self._client.get_glagol_device_token(
            self.session.music_token,
            device_id=self.device_id,
            platform=self.platform,
        )

    async def start(self) -> None:
        """Start or restart the WebSocket connection."""
        host = self.device.get("host")
        port = self.device.get("port")
        if not host or not port:
            _LOGGER.warning("[%s] No host/port for device, cannot connect", self.name)
            return

        # Yandex Station presents a self-signed device certificate, so TLS cert
        # verification is disabled on the Glagol WebSocket (``ssl=False`` in
        # ``_connect``). To limit the trust boundary, refuse to connect to
        # anything outside the private/link-local/loopback address space — a
        # spoofed mDNS record pointing at a public IP could otherwise receive
        # the device ``conversationToken`` with no way to authenticate the peer.
        try:
            ip = ipaddress.ip_address(str(host))
        except ValueError:
            _LOGGER.warning("[%s] Refusing non-IP Glagol host: %s", self.name, host)
            return
        if not (ip.is_private or ip.is_link_local or ip.is_loopback):
            _LOGGER.warning(
                "[%s] Refusing Glagol connection to non-local IP %s (TLS unverified)",
                self.name,
                host,
            )
            return

        new_url = f"wss://{host}:{port}"

        if not self.url:
            # First connection
            self.url = new_url
            self._connect_task = asyncio.create_task(self._connect(0))
        elif new_url != self.url:
            # Endpoint changed (IP or port)
            _LOGGER.debug("[%s] WebSocket endpoint changed, reconnecting", self.name)
            self.url = new_url
            if self.ws:
                await self.ws.close()

    async def stop(self) -> None:
        """Stop the WebSocket connection."""
        _LOGGER.debug("[%s] Stopping local connection", self.name)
        self.url = None
        if self.ws:
            await self.ws.close()
        if self._connect_task and not self._connect_task.done():
            self._connect_task.cancel()
            self._connect_task = None

    async def send_tts(self, text: str) -> dict[str, Any] | None:
        """Speak text using Alice's TTS via repeat_phrase scenario."""
        return await self.send(
            {
                "command": "serverAction",
                "serverActionEventPayload": {
                    "type": "server_action",
                    "name": "update_form",
                    "payload": {
                        "form_update": {
                            "name": "personal_assistant.scenarios.quasar.iot.repeat_phrase",
                            "slots": [
                                {
                                    "type": "string",
                                    "name": "phrase_to_repeat",
                                    "value": text,
                                }
                            ],
                        },
                        "resubmit": True,
                    },
                },
            }
        )

    async def send_text(self, text: str) -> dict[str, Any] | None:
        """Send text as if user spoke it (voice command simulation)."""
        return await self.send({"command": "sendText", "text": text})

    async def send(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        """Send a command and wait for response."""
        if not self.ws or self.ws.closed:
            _LOGGER.warning("[%s] Cannot send: not connected", self.name)
            return {"error": "not_connected"}

        # Log only the command name and the names of accompanying keys —
        # never their values.  `externalCommandBypass.data` carries
        # base64-encoded JSON with `streamUrl`, and MA stream URLs embed
        # session IDs / tokens that should not end up in logs.
        cmd = payload.get("command", "<unknown>")
        extras = sorted(k for k in payload if k != "command")
        _LOGGER.debug("[%s] => local | %s (extras: %s)", self.name, cmd, extras)

        if not self.device_token:
            _LOGGER.warning("[%s] Cannot send: not connected (missing device token)", self.name)
            return {"error": "not_connected"}

        request_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._waiters[request_id] = future

        try:
            await self.ws.send_json(
                {
                    "conversationToken": self.device_token.get_secret(),
                    "id": request_id,
                    "payload": payload,
                    "sentTime": round(time.time() * 1000),
                }
            )

            await asyncio.wait_for(future, WS_COMMAND_TIMEOUT)
            return future.result()

        except TimeoutError:
            return {"error": "timeout"}

        except Exception as e:
            _LOGGER.error("[%s] Send error: %r", self.name, e)
            return {"error": repr(e)}

        finally:
            self._waiters.pop(request_id, None)

    def _handle_message(self, data: dict[str, Any]) -> None:
        """Process a received WebSocket message."""
        request_id = data.get("requestId")
        if request_id in self._waiters:
            result: dict[str, Any] = {"status": data.get("status", "unknown")}
            # Preserve error/message fields for diagnostics
            if "error" in data:
                result["error"] = data["error"]
            if "message" in data:
                result["message"] = data["message"]
            _LOGGER.debug("[%s] Response for %s: %s", self.name, request_id, result)
            future = self._waiters[request_id]
            if not future.done():
                future.set_result(result)

        if self.update_handler:
            self.update_handler(data)

    async def _connect(self, fails: int) -> None:
        """Maintain persistent WebSocket connection with reconnect."""
        _LOGGER.info("[%s] Connecting to %s", self.name, self.url)
        fails += 1  # Will be reset on first message

        try:
            if not self.device_token:
                self.device_token = await self.get_device_token()
                _LOGGER.info("[%s] Got device token", self.name)

            # ssl=False: Yandex Station ships a self-signed device cert so cert
            # verification cannot be enabled. ``start()`` enforces that ``url``
            # points at a private/link-local/loopback IP to bound the impact.
            self.ws = await self.session.ws_connect(
                self.url,  # type: ignore[arg-type]  # url checked in start()
                heartbeat=WS_HEARTBEAT,
                ssl=False,
            )
            _LOGGER.info("[%s] WebSocket connected", self.name)
            await self._ping(command="softwareVersion")

            async for msg in self.ws:
                if isinstance(msg.data, ServerTimeoutError):
                    raise msg.data

                if msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED):
                    _LOGGER.debug("[%s] WS close frame received", self.name)
                    break
                if msg.type == WSMsgType.ERROR:
                    _LOGGER.warning("[%s] WS error frame: %s", self.name, msg.data)
                    break
                if msg.type != WSMsgType.TEXT:
                    continue

                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    _LOGGER.warning("[%s] Ignoring malformed WS payload: %s", self.name, msg.data)
                    continue
                fails = 0  # Any message = reset fails
                self._handle_message(data)

            # Connection closed normally — clear device token for next reconnect
            self.device_token = None

        except (ClientConnectorError, ConnectionResetError, ServerTimeoutError) as e:
            _LOGGER.warning("[%s] Connection error: %r", self.name, e)

        except (asyncio.CancelledError, RuntimeError) as e:
            if isinstance(e, RuntimeError) and e.args[0] != "Session is closed":
                raise
            _LOGGER.debug("[%s] Connection stopped: %r", self.name, e)
            if self.ws and not self.ws.closed:
                await self.ws.close()
            return

        except Exception:
            _LOGGER.exception("[%s] Unexpected error in local connection", self.name)

        # Notify handler that we're disconnected
        if self.update_handler:
            self.update_handler(None)

        # Stop reconnect attempts if stop() was called
        if not self.url:
            return

        if fails:
            delay = WS_RECONNECT_BASE_DELAY * min(fails - 1, WS_RECONNECT_MAX_MULTIPLIER)
            _LOGGER.debug("[%s] Reconnecting in %ds", self.name, delay)
            await asyncio.sleep(delay)

        self._connect_task = asyncio.create_task(self._connect(fails))

    async def _ping(self, command: str = "ping") -> None:
        """Send a ping/keepalive message."""
        if not self.ws or self.ws.closed or not self.device_token:
            return
        with contextlib.suppress(Exception):
            await self.ws.send_json(
                {
                    "conversationToken": self.device_token.get_secret(),
                    "id": str(uuid.uuid4()),
                    "payload": {"command": command},
                    "sentTime": round(time.time() * 1000),
                }
            )
