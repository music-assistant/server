"""
Waveform tap and WebSocket relay for the MilkDrop visualizer.

An in-process bridge visualizer role (the same pattern the Hue Lights Sync
plugin uses) joins the target player's Sendspin group, computes a
1024-sample uint8 offset-binary waveform tail per audio chunk, and fans the
frames out to browser viewers over a WebSocket route on the MA webserver.
One tap is shared per target player, refcounted by connected viewers.

Wire format to the browser:
- binary [22][ts:8 BE int64 µs][1024 x uint8, 0x80 = zero] - waveform tail
- binary [17][ts:8][flags:1, bit0 = downbeat] - beat schedule entries
- text {"type":"stream/start"|"stream/clear"|"stream/end", ...}
- replies to {"type":"client/time"} with {"type":"server/time"} (server clock)
"""

from __future__ import annotations

import asyncio
import logging
import re
import struct
from collections import deque
from typing import TYPE_CHECKING, cast

import numpy as np
from aiohttp import WSMsgType, web
from aiosendspin.models.core import ClientHelloPayload
from aiosendspin.models.core import DeviceInfo as SendspinDeviceInfo
from aiosendspin.models.visualizer import ClientHelloVisualizerSupport
from aiosendspin.server.roles.registry import register_role
from music_assistant_models.enums import PlaybackState
from orjson import dumps, loads

from music_assistant.controllers.webserver.helpers.auth_middleware import (
    get_authenticated_user,
    is_request_from_ingress,
)
from music_assistant.providers.sendspin.bridge_role import BridgeVisualizerRole
from music_assistant.providers.sendspin.player import SendspinBasePlayer

if TYPE_CHECKING:
    from collections.abc import Callable

    from aiosendspin.models.visualizer import BeatTiming
    from aiosendspin.server import SendspinClient
    from aiosendspin.server.roles import AudioChunk

    from music_assistant.providers.sendspin.provider import SendspinProvider

    from .provider import MilkdropVisualizerProvider

MILKDROP_ROLE_ID = "visualizer@_milkdrop"
RELAY_ROUTE = "/milkdrop_visualizer"
WAVE_SAMPLES = 1024
# How long a viewerless tap stays attached, so refreshes and the next track
# rejoin instantly instead of hitting the mid-stream join gap.
TAP_LINGER_SECONDS = 600
# Shortest normalized id tail accepted when matching a wrapper player id
# against the Sendspin player it embeds.
MIN_SUFFIX_MATCH_CHARS = 8

_LOGGER = logging.getLogger(__name__)


def _normalize_player_id(value: str) -> str:
    """Lowercase and strip separators so id spellings compare across providers."""
    return re.sub(r"[^a-z0-9]", "", value.lower())


class MilkdropWaveRole(BridgeVisualizerRole):
    """
    Bridge visualizer role that emits raw waveform tails instead of features.

    Receives the group's PCM (48 kHz / 16 bit / stereo per the base class
    audio requirements) and forwards one 1024-sample mono uint8 offset-binary
    tail per audio chunk, timestamped at chunk end in the server clock domain.
    """

    def __init__(self, client: SendspinClient) -> None:
        """
        Initialize the wave tap role.

        :param client: The Sendspin client this role belongs to.
        """
        super().__init__(client)
        self._wave_cb: Callable[[int, bytes], None] | None = None
        self._mono = np.zeros(0, dtype=np.float32)
        self._chunk_seen = False

    @property
    def role_id(self) -> str:
        """Return role identifier."""
        return MILKDROP_ROLE_ID

    def set_wave_callback(self, wave_cb: Callable[[int, bytes], None]) -> None:
        """
        Set the callback receiving (timestamp_us, 1024 uint8 sample bytes).

        :param wave_cb: Called once per audio chunk after warmup.
        """
        self._wave_cb = wave_cb

    def on_stream_start(self) -> None:
        """Reset the rolling buffer for a new stream (no feature extractor)."""
        _LOGGER.debug("wave role %s: stream start", self._client.client_id)
        self._mono = np.zeros(0, dtype=np.float32)
        self._chunk_seen = False
        if self._on_stream_start_cb:
            self._on_stream_start_cb()

    def on_audio_chunk(self, chunk: AudioChunk) -> None:
        """Append PCM and emit the waveform tail ending at this chunk."""
        if not self._chunk_seen:
            self._chunk_seen = True
            _LOGGER.debug(
                "wave role %s: first audio chunk (ts=%s)",
                self._client.client_id,
                chunk.timestamp_us,
            )
        if self._wave_cb is None:
            return
        raw = np.frombuffer(chunk.data, dtype="<i2")
        # A truncated chunk must not raise into the push stream's delivery
        # path: drop the dangling sample rather than fail the reshape.
        if raw.size % 2:
            raw = raw[:-1]
        if raw.size == 0:
            return
        mono = raw.reshape(-1, 2).mean(axis=1, dtype=np.float32) / 32768.0
        self._mono = np.concatenate([self._mono, mono])
        if self._mono.size >= WAVE_SAMPLES:
            tail = self._mono[-WAVE_SAMPLES:]
            quantized: np.ndarray = np.rint(np.clip(tail, -1.0, 1.0) * 127.0 + 128.0)
            ts_us = chunk.timestamp_us + chunk.duration_us
            self._wave_cb(ts_us, quantized.astype(np.uint8).tobytes())
            self._mono = self._mono[-WAVE_SAMPLES:]

    def on_stream_clear(self) -> None:
        """Reset the rolling buffer on seek/clear."""
        self._mono = np.zeros(0, dtype=np.float32)
        if self._on_stream_clear_cb:
            self._on_stream_clear_cb()

    def on_stream_end(self) -> None:
        """Reset the rolling buffer at stream end."""
        self._mono = np.zeros(0, dtype=np.float32)
        if self._on_stream_end_cb:
            self._on_stream_end_cb()


register_role(MILKDROP_ROLE_ID, lambda client: MilkdropWaveRole(client=client))


class _ViewerQueue:
    """
    Outbound queue for one viewer.

    Bounded so a stalled browser cannot stall the tap, but control messages
    (stream/clear, stream/end) are never dropped: losing one would leave the
    viewer animating stale audio after a seek or track change.
    """

    def __init__(self, capacity: int = 4096) -> None:
        self._items: deque[bytes | str] = deque()
        self._capacity = capacity
        self._wakeup = asyncio.Event()

    def push(self, item: bytes | str) -> None:
        """Enqueue an item, evicting the oldest waveform frame when full."""
        if len(self._items) >= self._capacity:
            for index, queued in enumerate(self._items):
                if isinstance(queued, bytes):
                    del self._items[index]
                    break
            else:
                self._items.popleft()
        self._items.append(item)
        self._wakeup.set()

    async def get(self) -> bytes | str:
        """Wait for and return the next item."""
        while not self._items:
            self._wakeup.clear()
            await self._wakeup.wait()
        return self._items.popleft()


class _Tap:
    """One in-process tap client shared by all viewers of the same target player."""

    def __init__(self, client_id: str) -> None:
        self.client_id = client_id
        self.queues: set[_ViewerQueue] = set()
        self.frames_seen = False
        # Packed beat frames with their scheduled timestamps, so viewers that
        # attach mid-track still receive the rest of the track's downbeats.
        self.beats: deque[tuple[int, bytes]] = deque(maxlen=4096)
        # Rolling history of packed waveform frames (~100s at 40fps). A newly
        # connecting viewer gets this replayed so near-playhead frames are
        # available instantly; without it a late viewer only receives frames
        # stamped at the production cursor, tens of seconds ahead of what is
        # audible on long-lead players.
        self.ring: deque[bytes] = deque(maxlen=4096)

    def fan_out(self, frame: bytes | str) -> None:
        """Deliver a packed frame to every attached viewer queue."""
        for queue in self.queues:
            queue.push(frame)


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
        self._unregister: Callable[[], None] | None = None
        # One shared tap per target player id, refcounted by viewer queues.
        self._taps: dict[str, _Tap] = {}
        self._taps_lock = asyncio.Lock()

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
        async with self._taps_lock:
            sendspin = self._sendspin_provider()
            for tap in self._taps.values():
                if sendspin is not None:
                    await sendspin.server_api.remove_client(tap.client_id)
            self._taps.clear()

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

        queue = _ViewerQueue()
        tap = await self._acquire_tap(target)
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
            for frame in self._pending_beat_frames(tap):
                await ws.send_bytes(frame)
            await self._serve_session(ws, queue)
        finally:
            # Detach synchronously; the linger runs detached so the request
            # handler (and its socket) is not held open for its duration.
            tap.queues.discard(queue)
            self.mass.create_task(self._linger_tap(target.player_id))
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
        if is_request_from_ingress(request):
            if await get_authenticated_user(request) is None:
                await ws.close(code=4001, message=b"Ingress authentication failed")
                return False
            return True
        try:
            async with asyncio.timeout(10):
                msg = await ws.receive()
        except TimeoutError:
            await ws.close(code=4001, message=b"Authentication timed out")
            return False
        if msg.type != WSMsgType.TEXT:
            await ws.close(code=4001, message=b"Expected text message for auth")
            return False
        try:
            auth_data = loads(msg.data)
        except ValueError:
            await ws.close(code=4001, message=b"Invalid JSON in auth message")
            return False
        if auth_data.get("type") != "auth" or not (token := auth_data.get("token")):
            await ws.close(code=4001, message=b"First message must be auth with a token")
            return False
        if await self.mass.webserver.auth.authenticate_with_token(token) is None:
            await ws.close(code=4001, message=b"Invalid or expired token")
            return False
        await ws.send_str('{"type": "auth_ok"}')
        return True

    async def _acquire_tap(self, target: SendspinBasePlayer) -> _Tap:
        """Return the shared tap for a target player, creating and grouping it on first use."""
        async with self._taps_lock:
            existing = self._taps.get(target.player_id)
            if existing is not None:
                return existing
            # Stable id: reconnects and multiple viewers reuse one hidden tap player.
            tap = _Tap(f"milkdrop-{target.player_id[-8:]}")
            viz_client = self._register_tap(tap)
            await target.api.group.add_client(viz_client)
            self._taps[target.player_id] = tap
            self.logger.info(
                "Waveform tap %s attached to group of %s", tap.client_id, target.display_name
            )
            self.logger.debug(
                "Tap acquired: target %s playback_state=%s",
                target.display_name,
                target.playback_state,
            )
            if target.playback_state == PlaybackState.PLAYING:
                self.logger.debug("Scheduling late-join watchdog for %s", tap.client_id)
                self.mass.create_task(self._late_join_watchdog(target, tap, viz_client))
            return tap

    async def _late_join_watchdog(
        self, target: SendspinBasePlayer, tap: _Tap, viz_client: SendspinClient
    ) -> None:
        """
        Re-kick a tap that joined an active stream but received no audio.

        Joining a running stream does not always wire a fresh in-process role
        into ongoing chunk delivery (depends on the push stream's cache state);
        a remove/re-add retriggers the late-join path. Without this, frames
        only start at the next stream start (pause/resume).
        """
        await asyncio.sleep(2.5)
        if tap.frames_seen or not tap.queues:
            return
        self.logger.info("Waveform tap %s got no audio after late join, re-kicking", tap.client_id)
        try:
            await target.api.group.remove_client(viz_client)
            await target.api.group.add_client(viz_client)
        except Exception:
            self.logger.exception("Waveform tap %s re-kick failed", tap.client_id)
            return
        await asyncio.sleep(2.5)
        if not tap.frames_seen and tap.queues:
            self.logger.warning(
                "Waveform tap %s still idle after re-kick; pause/resume playback to activate",
                tap.client_id,
            )

    def _pending_beat_frames(self, tap: _Tap) -> list[bytes]:
        """Return the tap's beat frames that are still in the future."""
        sendspin = self._sendspin_provider()
        if sendspin is None or not tap.beats:
            return []
        now_us = sendspin.server_api.clock.now_us()
        return [frame for ts_us, frame in tap.beats if ts_us > now_us]

    async def _linger_tap(self, target_player_id: str) -> None:
        """
        Remove a viewerless tap after the linger window.

        The linger keeps the tap in the group across refreshes and track
        changes during an active viewing session, so subsequent streams are
        covered from their start (avoiding the mid-stream join gap). It is
        removed once nobody has watched for a while.

        :param target_player_id: The player whose tap may now be idle.
        """
        tap = self._taps.get(target_player_id)
        if tap is None or tap.queues:
            return
        await asyncio.sleep(TAP_LINGER_SECONDS)
        async with self._taps_lock:
            tap = self._taps.get(target_player_id)
            if tap is None or tap.queues:
                return
            self._taps.pop(target_player_id, None)
            sendspin = self._sendspin_provider()
            if sendspin is not None:
                await sendspin.server_api.remove_client(tap.client_id)
            self.logger.info("Waveform tap %s removed (viewers gone)", tap.client_id)

    def _register_tap(self, tap: _Tap) -> SendspinClient:
        """Register the in-process tap client and wire its callbacks."""

        def on_wave(ts_us: int, samples: bytes) -> None:
            tap.frames_seen = True
            frame = struct.pack(">Bq", 22, ts_us) + samples
            tap.ring.append(frame)
            tap.fan_out(frame)

        def on_beats(beats: list[BeatTiming]) -> None:
            for beat in beats:
                frame = struct.pack(">BqB", 17, beat.timestamp_us, 1 if beat.is_downbeat else 0)
                tap.beats.append((beat.timestamp_us, frame))
                tap.fan_out(frame)

        def on_stream_boundary(message: str) -> None:
            # Buffered frames and the beat schedule both belong to the audio
            # that just ended; drop them so viewers do not replay stale state.
            tap.ring.clear()
            tap.beats.clear()
            tap.fan_out(message)

        sendspin = self._sendspin_provider()
        if sendspin is None:
            msg = "Sendspin provider is not available"
            raise RuntimeError(msg)
        # Taps must never surface as MA players (no group chips, no UI churn).
        sendspin.register_headless_client(tap.client_id)
        support = ClientHelloVisualizerSupport(buffer_capacity=65536, rate_max=60, types=["beat"])
        hello = ClientHelloPayload(
            client_id=tap.client_id,
            name="MilkDrop Visualizer",
            version=1,
            supported_roles=[MILKDROP_ROLE_ID],
            device_info=SendspinDeviceInfo(
                manufacturer="Music Assistant", product_name="MilkDrop Visualizer"
            ),
            visualizer_support=support,
        )
        viz_client = sendspin.server_api.register_external_player(
            hello, on_stream_start=lambda _req: None
        )
        role = cast("MilkdropWaveRole", viz_client.roles_by_family("visualizer")[0])
        role.set_wave_callback(on_wave)
        role.set_callbacks(
            on_frame=lambda _frame: None,
            on_beats=on_beats,
            on_beats_clear=lambda: None,
            on_stream_start=lambda: None,
            # Forward stream boundaries so viewers drop buffered future frames
            # immediately on skip/seek/stop instead of draining them.
            on_stream_clear=lambda: on_stream_boundary('{"type": "stream/clear"}'),
            on_stream_end=lambda: on_stream_boundary('{"type": "stream/end"}'),
        )
        role.setup_visualizer(support)
        viz_client.attach_preinitialized_roles()
        return viz_client

    async def _serve_session(self, ws: web.WebSocketResponse, queue: _ViewerQueue) -> None:
        """Pump binary frames to the browser and answer its time-sync pings."""
        sendspin = self._sendspin_provider()
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

    def _sendspin_provider(self) -> SendspinProvider | None:
        """Return the loaded Sendspin provider, if available."""
        return cast("SendspinProvider | None", self.mass.get_provider("sendspin"))

    def _resolve_target(self, query: str | None) -> SendspinBasePlayer | None:
        """
        Pick the Sendspin player whose group to visualize.

        :param query: Optional MA player id (preferred) or name substring.
        """
        candidates = [
            player
            for player in self.mass.players
            if isinstance(player, SendspinBasePlayer) and player.api is not None
        ]
        if query:
            # Normalized id match: the query may be the Sendspin player itself, a
            # player a Sendspin bridge rides on (underlying_player_id), or a
            # wrapper id spelling the same device differently (e.g. universal
            # player "up20f83b..." vs Sendspin "20:F8:3B:...").
            normalized_query = _normalize_player_id(query)
            for player in candidates:
                ids = (player.player_id, player.underlying_player_id or "")
                for candidate_id in ids:
                    normalized_id = _normalize_player_id(candidate_id)
                    if not normalized_id:
                        continue
                    if normalized_query == normalized_id:
                        return player
                    # Suffix matching pairs wrapper ids with the device id they
                    # embed (universal "up20f83b..." vs sendspin "20:F8:3B:...");
                    # require enough shared tail that it cannot match by chance.
                    shortest = min(len(normalized_query), len(normalized_id))
                    if shortest >= MIN_SUFFIX_MATCH_CHARS and (
                        normalized_query.endswith(normalized_id)
                        or normalized_id.endswith(normalized_query)
                    ):
                        return player
            lowered = query.lower()
            matched = [
                player
                for player in candidates
                if lowered in player.player_id.lower() or lowered in player.display_name.lower()
            ]
            # An unmatched query (e.g. a group/universal player id) falls back
            # to auto-pick rather than failing the session.
            if matched:
                candidates = matched
            else:
                self.logger.debug(
                    "Player query %r matched no Sendspin player, using auto-pick", query
                )
        playing = [p for p in candidates if p.playback_state == PlaybackState.PLAYING]
        chosen = (playing or candidates)[:1]
        return chosen[0] if chosen else None
