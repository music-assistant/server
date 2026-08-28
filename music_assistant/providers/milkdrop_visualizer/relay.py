"""
WebSocket relay serving MilkDrop waveform frames to browser viewers.

Fans the frames produced by the audio tap (see tap.py) out to the MA web
frontend over a route on the MA webserver.

Wire format to the browser:
- binary [22][ts:8 BE int64 µs][1024 x uint8, 0x80 = zero] - waveform tail
- binary [17][ts:8][flags:1, bit0 = downbeat] - beat schedule entries
- text {"type":"color","payload":{field: [r,g,b]|null, ...}} - changed color@v1 fields
- text {"type":"stream/start"|"stream/clear"|"stream/end", ...}
- replies to {"type":"client/time"} with {"type":"server/time"} (server clock)
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from aiohttp import WSMsgType, web
from music_assistant_models.enums import PlaybackState
from orjson import dumps, loads

from music_assistant.controllers.webserver.helpers.auth_middleware import (
    get_authenticated_user,
    is_request_from_ingress,
)

from .tap import TapManager, ViewerQueue, server_now_us

if TYPE_CHECKING:
    from collections.abc import Callable

    from music_assistant.models.player import Player

    from .provider import MilkdropVisualizerProvider

RELAY_ROUTE = "/milkdrop_visualizer"


class MilkdropRelay:
    """WebSocket relay serving waveform frames to browser viewers."""

    def __init__(self, provider: MilkdropVisualizerProvider) -> None:
        """
        Initialize the relay.

        :param provider: The loaded MilkDrop visualizer provider instance.
        """
        self.provider = provider
        self.mass = provider.mass
        self.logger = provider.logger.getChild("relay")
        self.taps = TapManager(provider)
        self._unregister: Callable[[], None] | None = None
        self._sessions: set[web.WebSocketResponse] = set()

    def setup(self) -> None:
        """Register the WebSocket route on the MA webserver."""
        self._unregister = self.mass.webserver.register_dynamic_route(
            RELAY_ROUTE, self._handle_ws, "GET"
        )
        self.logger.info("MilkDrop visualizer relay active on %s", RELAY_ROUTE)

    async def close(self) -> None:
        """Unregister the route, close live viewers, and tear down the taps."""
        if self._unregister is not None:
            self._unregister()
            self._unregister = None
        # Close viewers explicitly: on a provider reload they would otherwise
        # freeze on a dead route until they happened to reconnect.
        for ws in list(self._sessions):
            with suppress(Exception):
                await ws.close()
        await self.taps.close()

    async def _handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        """Serve one browser connection: tap the target player's audio and stream frames."""
        ws = web.WebSocketResponse(heartbeat=25)
        await ws.prepare(request)

        if not await self._authenticate(request, ws):
            return ws

        query = request.query.get("player")
        target = self._resolve_target(query)
        if target is None:
            self.logger.warning("No player to visualize for %s", query or "any player")
            await ws.send_str(
                dumps(
                    {
                        "type": "error",
                        "message": "no such player",
                    }
                ).decode()
            )
            await ws.close()
            return ws
        self.logger.debug("Viewer session: query=%r -> target %s", query, target.display_name)

        queue = ViewerQueue()
        tap = await self.taps.acquire(target)
        tap.queues.add(queue)
        self._sessions.add(ws)

        try:
            types = ["waveform", "beat", "color"]
            await ws.send_str(
                dumps(
                    {
                        "type": "stream/start",
                        "payload": {"visualizer": {"types": types}},
                    }
                ).decode()
            )
            if tap.has_only_future_frames():
                # Production is pinned at the buffer's eviction edge, ahead of
                # what is audible; nothing buffered is drawable yet. Ask the tap
                # to re-anchor at the playhead instead of replaying frames the
                # viewer could only sit on.
                tap.realign_requested = True
                replayed_waves = []
            else:
                replayed_waves = list(tap.ring)
            replayed_beats = self.taps.pending_beat_frames(tap)
            for frame in replayed_waves + replayed_beats:
                await ws.send_bytes(frame)
            if tap.last_color:
                await ws.send_str(dumps({"type": "color", "payload": tap.last_color}).decode())
            self.logger.debug(
                "Replayed %s waveform frame(s), %s pending beat(s) and color=%s to the viewer",
                len(replayed_waves),
                len(replayed_beats),
                bool(tap.last_color),
            )
            await self._serve_session(ws, queue)
        finally:
            # Detach synchronously; the release runs detached so the request
            # handler (and its socket) is not held open for its duration.
            self._sessions.discard(ws)
            tap.queues.discard(queue)
            self.taps.schedule_release(target.player_id)
            if not ws.closed:
                await ws.close()
        return ws

    async def _authenticate(self, request: web.Request, ws: web.WebSocketResponse) -> bool:
        """
        Authenticate a viewer connection, mirroring the Sendspin proxy handshake.

        Ingress requests are authenticated by Home Assistant via headers;
        everything else must send `{"type": "auth", "token": ...}` first.

        :param request: The incoming HTTP request.
        :param ws: The prepared WebSocket response.
        :return: True when the viewer is authenticated.
        """

        async def reject(reason: bytes) -> bool:
            # A rejected viewer closes with 4001 and gives up, so without this the
            # failure is invisible on both ends: no picture and no explanation.
            self.logger.warning("Rejected viewer from %s: %s", request.remote, reason.decode())
            await ws.close(code=4001, message=reason)
            return False

        if is_request_from_ingress(request):
            if await get_authenticated_user(request) is None:
                return await reject(b"Ingress authentication failed")
            return True
        try:
            async with asyncio.timeout(10):
                msg = await ws.receive()
        except TimeoutError:
            return await reject(b"Authentication timed out")
        if msg.type != WSMsgType.TEXT:
            return await reject(b"Expected text message for auth")
        try:
            auth_data = loads(msg.data)
        except ValueError:
            return await reject(b"Invalid JSON in auth message")
        if auth_data.get("type") != "auth" or not (token := auth_data.get("token")):
            return await reject(b"First message must be auth with a token")
        if await self.mass.webserver.auth.authenticate_with_token(token) is None:
            return await reject(b"Invalid or expired token")
        await ws.send_str('{"type": "auth_ok"}')
        return True

    async def _serve_session(self, ws: web.WebSocketResponse, queue: ViewerQueue) -> None:
        """Pump binary frames to the browser and answer its time-sync pings."""

        async def pump() -> None:
            while True:
                item = await queue.get()
                if isinstance(item, str):
                    await ws.send_str(item)
                else:
                    await ws.send_bytes(item)

        pump_task = asyncio.create_task(pump())
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                # A malformed frame from an (authenticated) client must not read
                # as a server crash: log it and keep the session alive.
                try:
                    data = loads(msg.data)
                    await self._handle_client_message(ws, data)
                except ValueError, KeyError, TypeError:
                    self.logger.debug("Ignoring malformed viewer message", exc_info=True)
                    continue
                if data.get("type") == "client/goodbye":
                    break
        finally:
            pump_task.cancel()
            # The pump raises if the socket died mid-send; awaiting it here keeps
            # that from surfacing later as "Task exception was never retrieved".
            with suppress(asyncio.CancelledError, ConnectionError, RuntimeError):
                await pump_task

    async def _handle_client_message(self, ws: web.WebSocketResponse, data: dict[str, Any]) -> None:
        """Answer a single text frame from a viewer."""
        if data.get("type") == "client/time":
            now_us = server_now_us()
            await ws.send_str(
                dumps(
                    {
                        "type": "server/time",
                        "payload": {
                            "client_transmitted": data["payload"]["client_transmitted"],
                            "server_received": now_us,
                            "server_transmitted": now_us,
                        },
                    }
                ).decode()
            )
        elif data.get("type") == "client/error":
            # Cast displays and kiosks have no reachable console, so a renderer
            # that cannot start would otherwise fail in silence.
            self.logger.warning("Viewer reported a problem: %s", data.get("message", "unknown"))

    def _resolve_target(self, query: str | None) -> Player | None:
        """
        Pick the player whose audio to visualize.

        :param query: MA player id of the viewed player. A player that is idle,
            or playing a source Music Assistant does not decode itself, is still
            a valid target: the tap produces frames as soon as there are any.
            Auto-pick applies only when no player was named at all.
        """
        if query:
            return self.mass.players.get_player(query)
        for player in self.mass.players:
            queue = self.mass.player_queues.get_active_queue(player.player_id)
            if queue is not None and queue.state == PlaybackState.PLAYING:
                return player
        return None
