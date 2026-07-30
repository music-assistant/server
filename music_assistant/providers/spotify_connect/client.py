"""
Async client for a local go-librespot daemon.

go-librespot exposes a small HTTP+WebSocket API (enabled via its ``server`` config
block). This client wraps the REST control endpoints (resume/pause/next/prev/seek/
volume/play) and the ``/events`` WebSocket stream that pushes player state changes.
See https://github.com/devgianlu/go-librespot/blob/master/API.md for the protocol.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

from aiohttp import ClientError, ClientTimeout, WSMsgType

if TYPE_CHECKING:
    import logging

    from music_assistant.mass import MusicAssistant

# Called with (event_type, event_data) for every WebSocket event.
EventCallback = Callable[[str, dict[str, Any]], Awaitable[None]]


class GoLibrespotClient:
    """Thin async wrapper around a go-librespot daemon's REST + WebSocket API."""

    def __init__(self, mass: MusicAssistant, base_url: str, logger: logging.Logger) -> None:
        """
        Initialize the client.

        :param mass: The MusicAssistant instance (for its shared HTTP session).
        :param base_url: Base URL of the daemon's API server, e.g. ``http://127.0.0.1:3678``.
        :param logger: Logger to use for diagnostics.
        """
        self.mass = mass
        self.base_url = base_url.rstrip("/")
        self.logger = logger

    async def wait_until_ready(self, timeout: float = 30.0) -> bool:
        """
        Poll the daemon's root endpoint until the API server answers.

        :param timeout: Maximum seconds to wait for the API to come up.
        :return: True once the API responds, False if the timeout elapses.
        """
        deadline = self.mass.loop.time() + timeout
        while self.mass.loop.time() < deadline:
            try:
                async with self.mass.http_session.get(
                    f"{self.base_url}/", timeout=ClientTimeout(total=2)
                ) as resp:
                    if resp.status == HTTPStatus.OK:
                        return True
            except ClientError, TimeoutError, OSError:
                pass
            await asyncio.sleep(0.25)
        return False

    async def get_status(self) -> dict[str, Any] | None:
        """
        Fetch the current player status.

        :return: The status payload, or None when there is no active session
            (the daemon answers ``204 No Content`` in that case).
        """
        return await self._request("GET", "/status")

    async def resume(self) -> None:
        """Resume playback on the active session."""
        await self._request("POST", "/player/resume")

    async def pause(self) -> None:
        """Pause playback on the active session."""
        await self._request("POST", "/player/pause")

    async def next(self) -> None:
        """Skip to the next track."""
        await self._request("POST", "/player/next")

    async def prev(self) -> None:
        """Skip to the previous track (or rewind the current one)."""
        await self._request("POST", "/player/prev")

    async def seek(self, position_ms: int) -> None:
        """
        Seek to an absolute position in the current track.

        :param position_ms: Target position in milliseconds.
        """
        await self._request("POST", "/player/seek", {"position": max(0, position_ms)})

    async def set_volume(self, volume: int) -> None:
        """
        Set the absolute playback volume.

        :param volume: Volume on go-librespot's scale (0..volume_steps).
        """
        await self._request("POST", "/player/volume", {"volume": max(0, volume)})

    async def play(self, uri: str, *, skip_to_uri: str | None = None, paused: bool = False) -> None:
        """
        Start playing a Spotify URI/context, making this device the active one.

        go-librespot activates this device unconditionally for a play request, so
        this doubles as a "take playback back to us" call when another device is
        currently active.

        :param uri: Spotify URI (track, album, playlist, ...) — typically a context.
        :param skip_to_uri: Optional track URI within the context to start at.
        :param paused: When True, load the content paused instead of playing.
        """
        body: dict[str, Any] = {"uri": uri, "paused": paused}
        if skip_to_uri:
            body["skip_to_uri"] = skip_to_uri
        await self._request("POST", "/player/play", body)

    async def listen_events(self, on_event: EventCallback) -> None:
        """
        Connect to the ``/events`` WebSocket and dispatch events until it closes.

        Returns normally when the connection is closed by either side; raises on
        connection errors so the caller can implement a reconnect loop.

        :param on_event: Coroutine called with ``(event_type, event_data)`` per event.
        """
        ws_url = f"{self.base_url.replace('http', 'ws', 1)}/events"
        async with self.mass.http_session.ws_connect(ws_url, heartbeat=30) as ws:
            self.logger.debug("Connected to go-librespot events websocket")
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        payload = msg.json()
                    except ValueError:
                        self.logger.debug("Ignoring non-JSON websocket message: %s", msg.data)
                        continue
                    if event_type := payload.get("type"):
                        await on_event(event_type, payload.get("data") or {})
                elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED):
                    break
                elif msg.type == WSMsgType.ERROR:
                    raise ws.exception() or ClientError("websocket error")

    async def _request(
        self, method: str, path: str, json_body: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        """
        Issue a request to the daemon's REST API.

        :param method: HTTP method.
        :param path: API path (e.g. ``/player/resume``).
        :param json_body: Optional JSON request body.
        :return: Parsed JSON response, or None when the daemon has no active session
            (``204 No Content``) or returns no body.
        :raises ClientError: When the daemon answers with an error status.
        """
        async with self.mass.http_session.request(
            method, f"{self.base_url}{path}", json=json_body, timeout=ClientTimeout(total=10)
        ) as resp:
            # 204 = the daemon has no active Spotify session; surface as "no data"
            # rather than an error so callers can treat it as a no-op.
            if resp.status == HTTPStatus.NO_CONTENT:
                self.logger.debug("go-librespot has no active session for %s %s", method, path)
                return None
            resp.raise_for_status()
            if resp.content_type == "application/json":
                data: dict[str, Any] = await resp.json()
                return data
            return None
