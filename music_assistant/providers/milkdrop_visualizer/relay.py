"""
WebSocket relay serving MilkDrop waveform frames to browser viewers.

Fans the frames produced by the audio tap (see tap.py) out to the MA web
frontend over a route on the MA webserver.

Wire format to the browser:
- binary [22][ts:8 BE int64 µs][1024 x uint8, 0x80 = zero] - waveform tail
- binary [17][ts:8][flags:1, bit0 = downbeat] - beat schedule entries
- text {"type":"stream/start"|"stream/clear"|"stream/end", ...}
- replies to {"type":"client/time"} with {"type":"server/time"} (server clock)
"""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING

from aiohttp import WSMsgType, web
from music_assistant_models.enums import PlaybackState
from orjson import dumps, loads

from music_assistant.controllers.webserver.helpers.auth_middleware import (
    get_authenticated_user,
    is_request_from_ingress,
)
from music_assistant.providers.sendspin.player import SendspinBasePlayer

from .tap import TapManager, ViewerQueue, get_sendspin_provider

if TYPE_CHECKING:
    from collections.abc import Callable

    from .provider import MilkdropVisualizerProvider

RELAY_ROUTE = "/milkdrop_visualizer"
# Shortest normalized id tail accepted when matching a wrapper player id
# against the Sendspin player it embeds.
MIN_SUFFIX_MATCH_CHARS = 8


def _normalize_player_id(value: str) -> str:
    """Lowercase and strip separators so id spellings compare across providers."""
    return re.sub(r"[^a-z0-9]", "", value.lower())


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

    def setup(self) -> None:
        """Register the WebSocket route on the MA webserver."""
        self._unregister = self.mass.webserver.register_dynamic_route(
            RELAY_ROUTE, self._handle_ws, "GET"
        )
        self.logger.info("MilkDrop visualizer relay active on %s", RELAY_ROUTE)

    async def close(self) -> None:
        """Unregister the route and tear down any live taps."""
        if self._unregister is not None:
            self._unregister()
            self._unregister = None
        await self.taps.close()

    async def _handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        """Serve one browser connection: tap the target group and stream frames."""
        ws = web.WebSocketResponse(heartbeat=25)
        await ws.prepare(request)

        if not await self._authenticate(request, ws):
            return ws

        query = request.query.get("player")
        target = self._resolve_target(query)
        if target is None:
            self.logger.warning("Viewer connected but no Sendspin player found (query=%r)", query)
            await ws.send_str(
                dumps({"type": "error", "message": "no sendspin player found"}).decode()
            )
            await ws.close()
            return ws
        self.logger.debug("Viewer session: query=%r -> target %s", query, target.display_name)

        queue = ViewerQueue()
        tap = await self.taps.acquire(target)
        tap.queues.add(queue)

        try:
            await ws.send_str(
                dumps(
                    {
                        "type": "stream/start",
                        "payload": {"visualizer": {"types": ["waveform", "beat"]}},
                    }
                ).decode()
            )
            for frame in list(tap.ring):
                await ws.send_bytes(frame)
            for frame in self.taps.pending_beat_frames(tap):
                await ws.send_bytes(frame)
            await self._serve_session(ws, queue)
        finally:
            # Detach synchronously; the linger runs detached so the request
            # handler (and its socket) is not held open for its duration.
            tap.queues.discard(queue)
            self.mass.create_task(self.taps.release(target.player_id))
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
        sendspin = get_sendspin_provider(self.mass)
        if sendspin is None:
            return
        clock = sendspin.server_api.clock

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
                data = loads(msg.data)
                if data.get("type") == "client/time":
                    now_us = clock.now_us()
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
                elif data.get("type") == "client/goodbye":
                    break
        finally:
            pump_task.cancel()

    def _resolve_target(self, query: str | None) -> SendspinBasePlayer | None:
        """
        Pick the Sendspin player whose group to visualize.

        :param query: MA player id of the viewed player; an unmatched (or
            missing) id falls back to the playing Sendspin player.
        """
        candidates = [
            player
            for player in self.mass.players
            if isinstance(player, SendspinBasePlayer) and player.api is not None
        ]
        if query:
            # The query may be the Sendspin player itself, a player a Sendspin
            # bridge rides on (underlying_player_id), or a wrapper id spelling
            # the same device differently (universal "up20f83b..." vs Sendspin
            # "20:F8:3B:..."), hence the normalized suffix match.
            normalized_query = _normalize_player_id(query)
            for player in candidates:
                for candidate_id in (player.player_id, player.underlying_player_id or ""):
                    normalized_id = _normalize_player_id(candidate_id)
                    if not normalized_id:
                        continue
                    if normalized_query == normalized_id:
                        return player
                    shortest = min(len(normalized_query), len(normalized_id))
                    if shortest >= MIN_SUFFIX_MATCH_CHARS and (
                        normalized_query.endswith(normalized_id)
                        or normalized_id.endswith(normalized_query)
                    ):
                        return player
            self.logger.debug("Player query %r matched no Sendspin player, using auto-pick", query)
        playing = [p for p in candidates if p.playback_state == PlaybackState.PLAYING]
        chosen = (playing or candidates)[:1]
        return chosen[0] if chosen else None
