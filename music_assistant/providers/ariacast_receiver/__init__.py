"""AriaCast Receiver Plugin — native Python implementation of the AriaCast protocol."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import AsyncGenerator, Callable
from contextlib import suppress
from ipaddress import AddressValueError, IPv4Address
from typing import TYPE_CHECKING, Any, cast

import aiohttp
from aiohttp import ClientTimeout, web
from music_assistant_models.enums import (
    ContentType,
    ImageType,
    MediaType,
    PlaybackState,
    ProviderFeature,
    SourceControl,
    StreamType,
)
from music_assistant_models.errors import AudioError, MediaNotFoundError, SetupFailedError
from music_assistant_models.media_items import (
    AudioFormat,
    AudioSource,
    MediaItemImage,
    ProviderMapping,
)
from music_assistant_models.streamdetails import StreamDetails, StreamMetadata

from music_assistant.constants import CONF_ENTRY_WARN_PREVIEW, WILDCARD_BIND_IPS
from music_assistant.models.plugin import PluginProvider, SourceControlValue

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry, ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CONF_MASS_PLAYER_ID = "mass_player_id"
CONF_ARIACAST_NAME = "ariacast_name"
DEFAULT_ARIACAST_NAME = "Music Assistant"
PLAYER_ID_AUTO = "__auto__"
SUPPORTED_FEATURES = {ProviderFeature.AUDIO_SOURCE}
AUDIO_SOURCE_ID = "main"

ARIACAST_PORT = 12889
DISCOVERY_PORT = 12888
FRAME_SIZE = 3840  # 20 ms of PCM S16LE 48 kHz stereo


# ---------------------------------------------------------------------------
# Provider entry points
# ---------------------------------------------------------------------------


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Return a new provider instance."""
    return AriaCastReceiver(mass, manifest, config)


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class AriaCastReceiver(PluginProvider):
    """
    Native Python AriaCast protocol server for Music Assistant.

    Listens on port 12889 and implements the AriaCast v1.1 wire protocol
    directly — no external binary or named pipe required.  Audio frames
    received from the Android sender flow into an asyncio.Queue and are
    yielded by get_audio_stream exactly like the VBAN receiver.
    """

    reload_on_streams_network_change = True

    @property
    def supported_features(self) -> set[ProviderFeature]:
        """Return the features supported by this provider."""
        return SUPPORTED_FEATURES

    def __init__(
        self, mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
    ) -> None:
        """Initialize the AriaCast Receiver provider."""
        super().__init__(mass, manifest, config, SUPPORTED_FEATURES)
        # Avoid str(None), which would bypass automatic player selection.
        self._default_player_id = str(self.get_setup_value(CONF_MASS_PLAYER_ID) or PLAYER_ID_AUTO)
        self._ariacast_name = (
            cast("str", self.get_setup_value(CONF_ARIACAST_NAME)) or DEFAULT_ARIACAST_NAME
        )

        # Audio pipeline: one asyncio.Queue, drained per stream (VBAN pattern)
        self._audio_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=100)

        # AriaCast protocol state
        self._audio_sender_ws: web.WebSocketResponse | None = None
        self._control_senders: set[web.WebSocketResponse] = set()
        self._meta_sockets: set[web.WebSocketResponse] = set()
        self._stats_sockets: set[web.WebSocketResponse] = set()
        self._artwork_bytes: bytes | None = None
        self._last_artwork_url: str | None = None
        self._is_playing: bool = False

        # /stats counters (spec: transport.md "GET /stats")
        self._stats_received_frames: int = 0
        self._stats_overruns: int = 0
        self._stats_task: asyncio.Task[None] | None = None

        # MA stream-routing state
        self._active_player_id: str | None = None
        self._in_use_by_queue: str | None = None
        self._active_session_id: str | None = None

        # Metadata pushed to the consuming MA queue
        self._stream_meta = StreamMetadata(title="AriaCast Ready")

        # aiohttp server handles
        self._runner: web.AppRunner | None = None
        self._discovery_transport: asyncio.BaseTransport | None = None
        # Guard so an unusable publish IP is reported once instead of on every probe
        self._discovery_address_warned: bool = False

        self._audio_format = AudioFormat(
            content_type=ContentType.PCM_S16LE,
            sample_rate=48000,
            bit_depth=16,
            channels=2,
        )
        self._audio_source = AudioSource(
            item_id=AUDIO_SOURCE_ID,
            provider=self.instance_id,
            name=self.name,
            provider_mappings={
                ProviderMapping(
                    item_id=AUDIO_SOURCE_ID,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    audio_format=self._audio_format,
                )
            },
            can_play_pause=True,
            can_seek=False,
            can_next_previous=True,
            exclusive=True,
            allow_external_trigger=True,
            # Source only appears when an Android sender connects and starts playing
            can_initiate=False,
        )

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return runtime options for this provider."""
        return (CONF_ENTRY_WARN_PREVIEW,)

    # -----------------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------------

    async def handle_async_init(self) -> None:
        """Start the AriaCast WebSocket server."""
        app = web.Application()
        app.router.add_get("/audio", self._ws_audio)
        app.router.add_get("/control", self._ws_control)
        app.router.add_get("/metadata", self._ws_metadata)
        app.router.add_get("/stats", self._ws_stats)
        app.router.add_post("/metadata", self._http_metadata)
        app.router.add_post("/api/command", self._http_command)
        app.router.add_get("/image/artwork", self._http_artwork)
        app.router.add_get("/artwork", self._http_artwork)

        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        bind_ip = self.mass.streams.bind_ip
        # a wildcard string binds one address family only, None binds both
        host = None if bind_ip in WILDCARD_BIND_IPS else bind_ip
        site = web.TCPSite(self._runner, host, ARIACAST_PORT, reuse_address=True)
        try:
            await site.start()
        except OSError as err:
            raise SetupFailedError(
                f"Cannot bind AriaCast server on {bind_ip}:{ARIACAST_PORT}: {err}"
            ) from err

        self.logger.info(
            "AriaCast server '%s' listening on %s:%d",
            self._ariacast_name,
            bind_ip,
            ARIACAST_PORT,
        )
        self.mass.create_task(self._run_udp_discovery())
        self._stats_task = self.mass.create_task(self._run_stats_broadcast())

    async def unload(self, is_removed: bool = False) -> None:
        """Tear down the server and close all connections."""
        # Close client sockets first, but never let a failure here prevent the
        # critical server/port teardown below (otherwise the port stays bound
        # and the next load fails with "address already in use").
        if self._stats_task:
            self._stats_task.cancel()
            with suppress(Exception):
                await self._stats_task
            self._stats_task = None
        for ws in [*self._control_senders, *self._meta_sockets, *self._stats_sockets]:
            with suppress(Exception):
                await ws.close()
        self._control_senders.clear()
        self._meta_sockets.clear()
        self._stats_sockets.clear()
        self._audio_sender_ws = None
        if self._discovery_transport:
            with suppress(Exception):
                self._discovery_transport.close()
            self._discovery_transport = None
        if self._runner:
            with suppress(Exception):
                await self._runner.cleanup()
            self._runner = None

    # -----------------------------------------------------------------------
    # PluginProvider audio-source contract
    # -----------------------------------------------------------------------

    async def get_audio_sources(self) -> list[AudioSource]:
        """Return the single AriaCast audio source."""
        return [self._audio_source]

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Return stream details for the given audio source."""
        if item_id != AUDIO_SOURCE_ID:
            raise MediaNotFoundError(f"Unknown AudioSource: {item_id}")
        # Allow through if currently playing OR if a player has played before (resume path)
        if not self._is_playing and not self._active_player_id:
            raise AudioError(
                "No AriaCast sender is streaming — open the AriaCast app on your device first"
            )
        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=self._audio_format,
            media_type=MediaType.AUDIO_SOURCE,
            stream_type=StreamType.CUSTOM,
            stream_metadata=self._stream_meta,
        )

    async def get_audio_stream(
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes]:
        """Yield raw PCM frames from the AriaCast sender queue (VBAN-style)."""
        consumer_queue = self._in_use_by_queue
        captured_session = self._active_session_id
        acquired = False

        # Drain any stale frames accumulated while the stream was idle
        # (avoids playing silence that built up during a pause)
        while not self._audio_queue.empty():
            with suppress(asyncio.QueueEmpty):
                self._audio_queue.get_nowait()

        self.logger.debug("Audio stream started: queue=%s", consumer_queue)

        try:
            while True:
                if (
                    self._in_use_by_queue != consumer_queue
                    or self._active_session_id != captured_session
                ):
                    self.logger.debug("Stream ownership changed, stopping")
                    break

                try:
                    async with asyncio.timeout(1):
                        frame = await self._audio_queue.get()
                    if not acquired:
                        acquired = True
                        self.logger.debug("First frame received from sender")
                    yield frame
                except TimeoutError:
                    # Cold-start check: fail fast if sender never starts sending
                    if not acquired and not self._is_playing:
                        raise AudioError("AriaCast sender is not streaming audio") from None
                    continue
        finally:
            self.logger.debug("Audio stream ended: queue=%s", consumer_queue)
            # Drain queue so the next stream starts clean
            while not self._audio_queue.empty():
                with suppress(asyncio.QueueEmpty):
                    self._audio_queue.get_nowait()
            if (
                self._in_use_by_queue == consumer_queue
                and self._active_session_id == captured_session
            ):
                self._in_use_by_queue = None

    async def on_source_selected(
        self, source_id: str, player_id: str, queue_id: str, stream_session_id: str
    ) -> None:
        """Handle source selection by a player queue."""
        if source_id != AUDIO_SOURCE_ID:
            return
        self._in_use_by_queue = queue_id
        self._active_session_id = stream_session_id
        self._active_player_id = player_id  # player_id for cmd_stop/cmd_power, not queue_id

    async def on_source_unselected(
        self, source_id: str, queue_id: str, stream_session_id: str
    ) -> None:
        """Handle source deselection by a player queue."""
        if source_id != AUDIO_SOURCE_ID:
            return
        if self._active_session_id != stream_session_id:
            return
        self._active_session_id = None
        if self._in_use_by_queue == queue_id:
            self._in_use_by_queue = None

    async def on_source_control(
        self, source_id: str, action: SourceControl, value: SourceControlValue = None
    ) -> None:
        """Handle playback control actions forwarded from the queue."""
        if source_id != AUDIO_SOURCE_ID:
            return
        if action == SourceControl.PLAY:
            await self._cmd_play()
        elif action == SourceControl.PAUSE:
            await self._cmd_pause()
        elif action == SourceControl.NEXT:
            await self._forward_action("next")
        elif action == SourceControl.PREVIOUS:
            await self._forward_action("previous")

    async def resolve_image(self, path: str) -> bytes:
        """Return image bytes for the given path."""
        if path.startswith("artwork_") and self._artwork_bytes:
            return self._artwork_bytes
        return b""

    # -----------------------------------------------------------------------
    # AriaCast protocol — WebSocket handlers
    # -----------------------------------------------------------------------

    async def _ws_audio(self, request: web.Request) -> web.WebSocketResponse:
        """Receive raw PCM frames from the AriaCast sender."""
        # Spec (transport.md): only one audio Sender at a time. A second
        # connection attempt while one is active is rejected with HTTP 403.
        if self._audio_sender_ws is not None:
            self.logger.warning(
                "Rejecting second /audio sender from %s — one is already streaming",
                request.remote,
            )
            raise web.HTTPForbidden(text="An AriaCast sender is already connected")

        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._audio_sender_ws = ws

        # Protocol handshake
        await ws.send_json(
            {
                "status": "READY",
                "sample_rate": 48000,
                "channels": 2,
                "frame_size": FRAME_SIZE,
            }
        )
        self.logger.info("AriaCast sender connected from %s", request.remote)

        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.BINARY:
                    if len(msg.data) == FRAME_SIZE:
                        self._stats_received_frames += 1
                        try:
                            self._audio_queue.put_nowait(msg.data)
                        except asyncio.QueueFull:
                            # Drop the oldest frame to make room for the new one
                            self._stats_overruns += 1
                            with suppress(asyncio.QueueEmpty):
                                self._audio_queue.get_nowait()
                            with suppress(asyncio.QueueFull):
                                self._audio_queue.put_nowait(msg.data)
                    else:
                        self.logger.warning(
                            "Dropping /audio frame with unexpected size %d (expected %d)",
                            len(msg.data),
                            FRAME_SIZE,
                        )
                elif msg.type in (
                    aiohttp.WSMsgType.ERROR,
                    aiohttp.WSMsgType.CLOSING,
                    aiohttp.WSMsgType.CLOSED,
                ):
                    break
        finally:
            if self._audio_sender_ws is ws:
                self._audio_sender_ws = None

        self.logger.info("AriaCast sender disconnected from %s", request.remote)
        # If we were the active stream, mark as not playing so get_audio_stream can exit cleanly
        if self._is_playing:
            self.logger.debug("Sender disconnected while playing - clearing is_playing")
            self._is_playing = False
        return ws

    async def _ws_control(self, request: web.Request) -> web.WebSocketResponse:
        """Register a sender for command delivery and accept inbound commands."""
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._control_senders.add(ws)
        self.logger.info("Control client connected from %s", request.remote)

        # Spec (transport.md): the Python server sends an initial status frame
        # on /control (unlike the Go server, which sends nothing here).
        current_volume = self._get_current_volume()
        await ws.send_json(
            {
                "status": "READY",
                "volume_available": current_volume is not None,
                "current_volume": current_volume if current_volume is not None else -1,
            }
        )

        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    with suppress(Exception):
                        payload = json.loads(msg.data)
                        await self._handle_inbound_control(ws, payload)
                elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSING):
                    break
        finally:
            self._control_senders.discard(ws)

        return ws

    async def _handle_inbound_control(
        self, ws: web.WebSocketResponse, payload: dict[str, Any]
    ) -> None:
        """
        Process a command-keyed or action-keyed message sent by a /control client.

        Per control.md, the Python server (unlike the Go server) accepts both
        ``{"command": ...}`` and ``{"action": ...}`` messages from senders and
        replies with an ack in the same key style.
        """
        key = "command" if "command" in payload else "action" if "action" in payload else None
        if key is None:
            return
        # A payload carrying "success" is a reply/ack (the shape this server
        # sends itself), not a command — dispatching it would relay a client's
        # ack of a broadcast action as a fresh command (and loop on a client
        # that acks our own ack).
        if "success" in payload:
            return
        command = str(payload.get(key) or "").lower()
        if not command:
            return

        if command in ("volume", "volume_set"):
            await self._handle_volume_command(ws, command, payload)
            return

        reply: dict[str, Any] = {key: command, "success": True}

        if command == "play":
            await self._cmd_play()
        elif command == "pause":
            await self._cmd_pause()
        elif command in ("play_pause", "toggle"):
            if self._is_playing:
                await self._cmd_pause()
            else:
                await self._cmd_play()
        elif command == "stop":
            await self._cmd_pause()
        elif command in ("next", "previous"):
            # Relay to the other control clients so the streaming sender skips
            # within its own playlist (the MA queue holds no track boundaries for
            # a live cast stream). Never routed through player_queues.next: the
            # queue delegates transport commands for an active AudioSource back
            # to this plugin, which would echo the command to its originator
            # (and loop forever on a client that echoes actions back).
            if self._in_use_by_queue:
                await self._forward_action(command, exclude=ws)
        elif command == "seek":
            # The live AriaCast source has no seekable timeline on the MA side
            # (audio_source.can_seek is False); accept and ack per spec, no-op.
            position = payload.get("position_ms", payload.get("value"))
            reply["position_ms"] = position
        else:
            self.logger.debug("Unknown /control %s: %r", key, command)
            reply["success"] = False

        with suppress(Exception):
            await ws.send_json(reply)

    def _get_current_volume(self) -> int | None:
        """Return the current volume (0-100) of the active MA player, if known."""
        player_id = self._active_player_id or self._get_target_player_id()
        if not player_id:
            return None
        player = self.mass.players.get_player(player_id)
        if not player or player.state.volume_level is None:
            return None
        return int(player.state.volume_level)

    async def _handle_volume_command(
        self, ws: web.WebSocketResponse, command: str, payload: dict[str, Any]
    ) -> None:
        """Handle a volume/volume_set command per control.md."""
        player_id = self._active_player_id or self._get_target_player_id()
        if not player_id:
            with suppress(Exception):
                await ws.send_json({"command": "volume", "level": -1, "success": False})
            return
        player = self.mass.players.get_player(player_id)

        if not player or player.state.volume_level is None:
            with suppress(Exception):
                await ws.send_json(
                    {"command": "volume", "action": "volume", "level": -1, "success": False}
                )
            return

        try:
            if command == "volume_set":
                level = payload.get("level")
                if level is not None:
                    await self.mass.players.cmd_volume_set(player_id, int(level))
            else:
                direction = payload.get("direction")
                value = payload.get("value")
                if direction == "up":
                    await self.mass.players.cmd_volume_up(player_id)
                elif direction == "down":
                    await self.mass.players.cmd_volume_down(player_id)
                elif direction == "get":
                    pass
                elif value is not None:
                    await self.mass.players.cmd_volume_set(player_id, int(value))
        except Exception as exc:
            self.logger.debug("Volume command failed: %s", exc)
            with suppress(Exception):
                await ws.send_json({"command": "volume", "level": -1, "success": False})
            return

        # Re-fetch the resulting level after the command was applied.
        player = self.mass.players.get_player(player_id)
        vol = player.state.volume_level if player else None
        level = int(vol) if vol is not None else -1
        with suppress(Exception):
            await ws.send_json({"command": "volume", "level": level, "success": level != -1})

    async def _ws_metadata(self, request: web.Request) -> web.WebSocketResponse:
        """Stream metadata updates to subscribers."""
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._meta_sockets.add(ws)

        # Immediately push current state on connect (spec requirement)
        await ws.send_json({"type": "metadata", "data": self._meta_dict()})

        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    with suppress(Exception):
                        payload = json.loads(msg.data)
                        ptype = payload.get("type")
                        if ptype == "update":
                            await self._apply_meta(payload.get("data", {}))
                            await ws.send_json({"type": "ack", "success": True})
                        elif ptype == "get":
                            await ws.send_json({"type": "metadata", "data": self._meta_dict()})
                        elif ptype == "clear":
                            self._stream_meta = StreamMetadata(title="AriaCast Ready")
                            await ws.send_json({"type": "ack", "success": True})
                elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSING):
                    break
        finally:
            self._meta_sockets.discard(ws)

        return ws

    async def _ws_stats(self, request: web.Request) -> web.WebSocketResponse:
        """GET /stats — subscribe to periodic buffer/playback statistics."""
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._stats_sockets.add(ws)
        self.logger.debug("Stats client connected from %s", request.remote)

        with suppress(Exception):
            await ws.send_json(self._stats_dict())

        try:
            async for msg in ws:
                if msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSING):
                    break
        finally:
            self._stats_sockets.discard(ws)

        return ws

    def _stats_dict(self) -> dict[str, Any]:
        """Serialise current buffer/playback statistics per transport.md."""
        queued = self._audio_queue.qsize()
        capacity = self._audio_queue.maxsize or 1
        return {
            "receivedFrames": self._stats_received_frames,
            "playedCallbacks": self._stats_received_frames,
            "underruns": 0,
            "overruns": self._stats_overruns,
            "queuedFrames": queued,
            "bufferLevel": f"{(queued / capacity) * 100:.1f}%",
        }

    async def _run_stats_broadcast(self) -> None:
        """Push stats to all /stats subscribers every second (spec: transport.md)."""
        with suppress(asyncio.CancelledError):
            while True:
                await asyncio.sleep(1)
                if not self._stats_sockets:
                    continue
                msg = self._stats_dict()
                dead: set[web.WebSocketResponse] = set()
                for ws in list(self._stats_sockets):
                    try:
                        await ws.send_json(msg)
                    except Exception:
                        dead.add(ws)
                self._stats_sockets -= dead

    # -----------------------------------------------------------------------
    # AriaCast protocol — HTTP handlers
    # -----------------------------------------------------------------------

    async def _http_metadata(self, request: web.Request) -> web.Response:
        """POST /metadata — sender pushes track info."""
        try:
            body = await request.json()
            # Spec: sender may wrap payload in {"data": {...}}
            data = body.get("data", body)
            await self._apply_meta(data)
        except Exception as exc:
            return web.Response(status=400, text=str(exc))
        return web.Response(status=200)

    async def _http_command(self, request: web.Request) -> web.Response:
        """POST /api/command — MA (or web dashboard) triggers a playback action."""
        try:
            body = await request.json()
            action = body.get("action")
            if not action:
                return web.Response(status=400, text="Missing action")
            if action == "play":
                await self._cmd_play()
            elif action == "pause":
                await self._cmd_pause()
            else:
                await self._forward_action(action)
            return web.Response(status=200)
        except Exception as exc:
            return web.Response(status=400, text=str(exc))

    async def _http_artwork(self, _request: web.Request) -> web.Response:
        """GET /image/artwork or /artwork — serve cached artwork."""
        if not self._artwork_bytes:
            return web.Response(status=404, text="No artwork available")
        return web.Response(body=self._artwork_bytes, content_type="image/jpeg")

    # -----------------------------------------------------------------------
    # Metadata helpers
    # -----------------------------------------------------------------------

    def _meta_dict(self) -> dict[str, Any]:
        """Serialise current metadata to the canonical AriaCast wire format."""
        m = self._stream_meta
        return {
            "title": m.title,
            "artist": m.artist,
            "album": m.album,
            "artwork_url": m.image_url,
            "duration_ms": int(m.duration * 1000) if m.duration else None,
            "position_ms": int(m.elapsed_time * 1000) if m.elapsed_time is not None else None,
            "is_playing": self._is_playing,
        }

    async def _apply_meta(self, data: dict[str, Any]) -> None:
        """Merge a partial metadata update from the sender into local state."""
        m = self._stream_meta

        if "title" in data:
            m.title = data["title"]
        if "artist" in data:
            m.artist = data["artist"]
        if "album" in data:
            m.album = data["album"]

        # Accept both camelCase (Android) and snake_case (spec broadcast) per interop rule
        duration = data.get("durationMs") or data.get("duration_ms")
        if duration is not None:
            m.duration = int(duration) // 1000

        # the fallback is on the key rather than on the value, so a reported position
        # of 0 (the start of a track) is applied instead of read as 'not reported'
        position = data.get("positionMs", data.get("position_ms"))
        if position is not None:
            m.elapsed_time = int(position) // 1000
            m.elapsed_time_last_updated = time.time()

        artwork = data.get("artworkUrl") or data.get("artwork_url")
        if artwork and artwork != self._last_artwork_url:
            self._last_artwork_url = artwork
            self._artwork_bytes = None
            m.image_url = None
            self.mass.create_task(self._fetch_artwork(artwork))

        # Handle is_playing in both casings
        if "isPlaying" in data:
            is_playing = bool(data["isPlaying"])
        elif "is_playing" in data:
            is_playing = bool(data["is_playing"])
        else:
            is_playing = None

        if is_playing is not None:
            await self._handle_playback_state(is_playing)
        else:
            await self._broadcast_meta()

    async def _handle_playback_state(self, is_playing: bool) -> None:
        """React to is_playing transitions from the sender."""
        was_playing = self._is_playing
        self._is_playing = is_playing

        if is_playing and not self._in_use_by_queue:
            target = self._active_player_id or self._get_target_player_id()
            if target:
                # _active_player_id holds player_id; _in_use_by_queue gets the real
                # queue_id from on_source_selected once MA confirms the stream
                if not self._active_player_id:
                    self._active_player_id = target
                self._in_use_by_queue = target  # optimistic guard vs duplicate events
                self.logger.debug("Triggering play on player %s", target)
                self.mass.create_task(self._safe_play_media(target))
        elif not is_playing and was_playing and self._in_use_by_queue:
            player_id = self._active_player_id
            # Clear the queue guard before the stop so a fast resume can re-trigger
            self._in_use_by_queue = None
            if player_id:
                self.mass.create_task(self.mass.players.cmd_stop(player_id))

        await self._broadcast_meta()

    async def _broadcast_meta(self) -> None:
        """Push current metadata to all /metadata WebSocket subscribers and to MA."""
        msg = {"type": "metadata", "data": self._meta_dict()}
        dead: set[web.WebSocketResponse] = set()
        for ws in list(self._meta_sockets):
            try:
                await ws.send_json(msg)
            except Exception:
                dead.add(ws)
        self._meta_sockets -= dead

        if self._in_use_by_queue:
            self.mass.streams.update_stream_metadata(
                self._in_use_by_queue, AUDIO_SOURCE_ID, self.instance_id, self._stream_meta
            )

    async def _fetch_artwork(self, url: str) -> None:
        """Download artwork from the sender's HTTP server and cache it."""
        await asyncio.sleep(0.2)  # let the sender stabilise the image
        try:
            async with self.mass.http_session.get(url, timeout=ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    if data:
                        self._artwork_bytes = data
                        img_hash = hashlib.md5(data).hexdigest()[:8]
                        image = MediaItemImage(
                            type=ImageType.THUMB,
                            path=f"artwork_{img_hash}",
                            provider=self.instance_id,
                            remotely_accessible=False,
                        )
                        self._stream_meta.image_url = self.mass.metadata.get_image_url(image)
                        await self._broadcast_meta()
        except Exception as exc:
            self.logger.debug("Artwork fetch failed: %s", exc)

    # -----------------------------------------------------------------------
    # Playback commands
    # -----------------------------------------------------------------------

    async def _cmd_play(self) -> None:
        self.logger.info("PLAY")
        # Optimistically mark playing before the sender confirms so that
        # get_stream_details passes on an immediate resume.
        self._is_playing = True
        await self._forward_action("play")
        if not self._in_use_by_queue and self._active_player_id:
            target = self._active_player_id
            self._in_use_by_queue = target
            self.mass.create_task(self._safe_play_media(target))

    async def _cmd_pause(self) -> None:
        self.logger.info("PAUSE")
        player_id = self._active_player_id
        # Clear queue guard before stop so a fast resume can re-trigger play_media
        self._in_use_by_queue = None
        self._is_playing = False
        if player_id:
            await self.mass.players.cmd_stop(player_id)
        await self._forward_action("pause")
        await self._broadcast_meta()

    async def _forward_action(
        self, action: str, exclude: web.WebSocketResponse | None = None
    ) -> None:
        """
        Send an action to all connected /control WebSocket senders.

        :param action: The action to broadcast.
        :param exclude: Optional sender to skip (the originator of a relayed command).
        """
        msg = {"action": action}
        dead: set[web.WebSocketResponse] = set()
        for ws in list(self._control_senders):
            if ws is exclude:
                continue
            try:
                await ws.send_json(msg)
            except Exception:
                dead.add(ws)
        self._control_senders -= dead

    async def _safe_play_media(self, target: str) -> None:
        uri = str(self._audio_source.uri)
        self.logger.debug("play_media %s → %s", uri, target)
        try:
            await self.mass.player_queues.play_media(target, uri)
        except Exception as exc:
            self.logger.warning("play_media failed for player %s: %s", target, exc)
            if self._in_use_by_queue == target:
                self._in_use_by_queue = None

    # -----------------------------------------------------------------------
    # UDP discovery (AriaCast v1.1 spec)
    # -----------------------------------------------------------------------

    async def _run_udp_discovery(self) -> None:
        """Respond to DISCOVER_AUDIOCAST UDP broadcasts on port 12888."""
        loop = asyncio.get_running_loop()

        class _Proto(asyncio.DatagramProtocol):
            def __init__(
                self,
                transport_holder: list[asyncio.DatagramTransport],
                build_payload: Callable[[], bytes | None],
                logger: Any,
            ) -> None:
                self._holder = transport_holder
                self._build_payload = build_payload
                self._log = logger

            def connection_made(self, transport: asyncio.BaseTransport) -> None:
                if isinstance(transport, asyncio.DatagramTransport):
                    self._holder.append(transport)

            def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
                if data.strip() != b"DISCOVER_AUDIOCAST":
                    return
                self._log.debug("Discovery from %s", addr)
                if (payload := self._build_payload()) is None:
                    return
                transport = self._holder[0] if self._holder else None
                if transport:
                    with suppress(Exception):
                        transport.sendto(payload, addr)

        holder: list[asyncio.DatagramTransport] = []
        try:
            transport, _ = await loop.create_datagram_endpoint(
                lambda: _Proto(holder, self._build_discovery_payload, self.logger),
                # Senders find us by broadcast, which is only delivered to a socket
                # bound to the wildcard address, so this cannot follow streams.bind_ip.
                local_addr=("0.0.0.0", DISCOVERY_PORT),
                allow_broadcast=True,
            )
            self._discovery_transport = transport
            self.logger.info("UDP discovery active on port %d", DISCOVERY_PORT)
        except Exception as exc:
            self.logger.warning(
                "UDP discovery unavailable (port %d in use?): %s", DISCOVERY_PORT, exc
            )

    def _build_discovery_payload(self) -> bytes | None:
        """
        Return the discovery reply to send to a sender, or None to stay silent.

        None means this server has no address a sender could connect back to.
        """
        publish_ip = str(self.mass.streams.publish_ip)
        if not _is_advertisable_address(publish_ip):
            if not self._discovery_address_warned:
                self._discovery_address_warned = True
                self.logger.warning(
                    "Ignoring AriaCast discovery requests: the streamserver publish IP (%s) "
                    "is not an IPv4 address senders can reach. Set the publish IP in "
                    "Settings --> System --> Streams.",
                    publish_ip,
                )
            return None
        self._discovery_address_warned = False
        return json.dumps(
            {
                "server_name": self._ariacast_name,
                "ip": publish_ip,
                "port": ARIACAST_PORT,
                "samplerate": 48000,
                "channels": 2,
            }
        ).encode()

    # -----------------------------------------------------------------------
    # Player selection helper
    # -----------------------------------------------------------------------

    def _get_target_player_id(self) -> str | None:
        if self._active_player_id:
            if self.mass.players.get_player(self._active_player_id):
                return self._active_player_id
            self.logger.debug("Stored player %s no longer available", self._active_player_id)
            self._active_player_id = None

        if self._default_player_id == PLAYER_ID_AUTO:
            for player in self.mass.players.all_players(False, False):
                if player.state.playback_state == PlaybackState.PLAYING:
                    self.logger.debug(
                        "Auto-selected playing player: %s (%s)",
                        player.display_name,
                        player.player_id,
                    )
                    return player.player_id
            players = list(self.mass.players.all_players(False, False))
            if players:
                self.logger.debug(
                    "Auto-selected first player: %s (%s)",
                    players[0].display_name,
                    players[0].player_id,
                )
                return players[0].player_id
            self.logger.warning("No MA players available to route AriaCast audio")
            return None

        self.logger.debug("Using configured player: %s", self._default_player_id)
        return self._default_player_id


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_advertisable_address(address: str) -> bool:
    """
    Return whether an address may be advertised to an AriaCast sender.

    Senders reach discovery over IPv4 and connect straight back to the bare address
    in the reply, so anything that is not a routable IPv4 address is unusable here.

    :param address: The address that would be advertised.
    """
    try:
        parsed = IPv4Address(address)
    except AddressValueError:
        return False
    return not (
        parsed.is_loopback
        or parsed.is_unspecified
        or parsed.is_link_local
        or parsed.is_multicast
        or parsed.is_reserved
    )
