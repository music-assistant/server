"""
Cloud connection manager for Yandex Smart Home via yaha-cloud.ru relay.

Manages a persistent WebSocket connection to the yaha-cloud.ru relay service.
Incoming Yandex Smart Home API requests are received over WS, processed by
the on_request callback, and the response is sent back over WS.

Adapted from dext0r/yandex_smart_home cloud.py, stripped of HA dependencies.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import aiohttp

if TYPE_CHECKING:
    from ya_dialogs_api import SecretStr

from .constants import (
    CLOUD_BASE_URL,
    CLOUD_HEARTBEAT_INTERVAL,
    CLOUD_RECONNECT_MAX,
    CLOUD_RECONNECT_MIN,
    CLOUD_REGISTER_URL,
    CLOUD_WS_URL,
)
from .schema import CloudRequest

_LOGGER = logging.getLogger(__name__)


class CloudManager:
    """Manages WebSocket connection to yaha-cloud.ru for Smart Home API relay."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        connection_token: SecretStr,
        on_request: Callable[[CloudRequest], Awaitable[dict[str, Any]]],
        logger: logging.Logger | None = None,
    ) -> None:
        """Initialize cloud relay manager."""
        self._session = session
        self._token = connection_token
        self._on_request = on_request
        self._logger = logger or _LOGGER
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._running = False
        self._reconnect_delay = CLOUD_RECONNECT_MIN

    @property
    def connected(self) -> bool:
        """Return True if WebSocket is connected."""
        return self._ws is not None and not self._ws.closed

    async def connect(self) -> None:
        """Start the WebSocket connection loop (runs until disconnect is called)."""
        self._running = True
        while self._running:
            try:
                await self._connect_once()
            except asyncio.CancelledError:
                break
            except Exception:
                if not self._running:
                    break  # type: ignore[unreachable]
                self._logger.exception(
                    "Cloud connection error, reconnecting in %ds", self._reconnect_delay
                )
            if not self._running:
                break  # type: ignore[unreachable]
            # Backoff before reconnect (both after errors and clean disconnects)
            await asyncio.sleep(self._reconnect_delay)
            self._reconnect_delay = min(self._reconnect_delay * 2, CLOUD_RECONNECT_MAX)

    async def disconnect(self) -> None:
        """Stop the connection loop and close WebSocket."""
        self._running = False
        if self._ws and not self._ws.closed:
            await self._ws.close()
        self._ws = None
        self._logger.info("Cloud relay disconnected")

    async def _connect_once(self) -> None:
        """Single WebSocket connection attempt + message loop."""
        headers = {"Authorization": f"Bearer {self._token.get_secret()}"}
        async with self._session.ws_connect(
            CLOUD_WS_URL,
            headers=headers,
            heartbeat=CLOUD_HEARTBEAT_INTERVAL,
        ) as ws:
            self._ws = ws
            self._reconnect_delay = CLOUD_RECONNECT_MIN
            self._logger.info("Connected to cloud relay at %s", CLOUD_WS_URL)

            async for msg in ws:
                if not self._running:
                    break

                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except json.JSONDecodeError:
                        self._logger.warning("Received invalid JSON from cloud relay: %r", msg.data)
                        continue
                    await self._handle_message(ws, data)
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    self._logger.error("WebSocket error: %s", ws.exception())
                    break
                elif msg.type in (
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSING,
                    aiohttp.WSMsgType.CLOSED,
                ):
                    break

            self._ws = None
            self._logger.info("Cloud relay connection closed")

    async def _handle_message(
        self, ws: aiohttp.ClientWebSocketResponse, data: dict[str, Any]
    ) -> None:
        """Parse incoming WS message, call handler, and send response."""
        try:
            # message may be a JSON string or already parsed dict
            raw_message = data.get("message")
            if isinstance(raw_message, str) and raw_message:
                raw_message = json.loads(raw_message)
            request = CloudRequest(
                request_id=data["request_id"],
                action=data["action"],
                message=raw_message if isinstance(raw_message, dict) else None,
            )
            self._logger.debug("Cloud request: action=%s", request.action)
            response = await self._on_request(request)
            await ws.send_json(response)
        except Exception:
            self._logger.exception("Error handling cloud message: %s", data)
            # Send best-effort error response so the relay doesn't hang
            request_id = data.get("request_id") if isinstance(data, dict) else None
            if request_id and ws and not ws.closed:
                try:
                    await ws.send_json(
                        {"request_id": request_id, "payload": {"error": "INTERNAL_ERROR"}}
                    )
                except Exception:
                    self._logger.debug("Failed to send error response for %s", request_id)


# ---------------------------------------------------------------------------
# Cloud instance registration helpers
# ---------------------------------------------------------------------------


async def register_cloud_instance(
    session: aiohttp.ClientSession,
    platform: str | None = None,
) -> dict[str, str]:
    """
    Register a new cloud instance on yaha-cloud.ru.

    Returns dict with 'id', 'password', 'connection_token'.
    No authentication is required — the relay auto-generates credentials.

    For Cloud Plus mode, pass platform="yandex" so the relay can validate
    the client_id during OAuth account linking.
    """
    kwargs: dict[str, Any] = {}
    if platform:
        kwargs["json"] = {"platform": platform}
    async with session.post(CLOUD_REGISTER_URL, **kwargs) as resp:
        resp.raise_for_status()
        # yaha-cloud.ru may return text/plain content-type for JSON
        data = await resp.json(content_type=None)
        _LOGGER.info("Registered cloud instance: %s", data.get("id"))
        return dict(data)


async def get_cloud_otp(
    session: aiohttp.ClientSession,
    instance_id: str,
    token: SecretStr,
) -> str:
    """
    Get a one-time password for linking the instance in the Yandex app.

    User enters this OTP in the Yandex Smart Home app to link their account.
    The token parameter is the connection_token from registration.
    """
    url = f"{CLOUD_BASE_URL}/api/home_assistant/v1/instance/{instance_id}/otp"
    headers = {"Authorization": f"Bearer {token.get_secret()}"}
    async with session.post(url, headers=headers) as resp:
        resp.raise_for_status()
        # yaha-cloud.ru may return text/plain content-type for JSON
        data = await resp.json(content_type=None)
        return str(data["code"])
