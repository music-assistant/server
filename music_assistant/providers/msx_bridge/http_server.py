"""Embedded HTTP server for the MSX Bridge Provider."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import io
import json
import logging
import secrets
import time
from html import escape as html_escape
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, NamedTuple, cast
from urllib.parse import quote, urlsplit, urlunsplit

import aiohttp
from aiohttp import WSMsgType, web
from music_assistant_models.enums import ContentType
from music_assistant_models.errors import InvalidProviderURI
from music_assistant_models.media_items import AudioFormat, Track

from music_assistant.constants import SENDSPIN_SERVER_PORT
from music_assistant.controllers.streams.audio_processing import get_media_session_id
from music_assistant.controllers.streams.constants import (
    SINGLE_ITEM_READRATE,
    SINGLE_ITEM_READRATE_INITIAL_BURST,
)
from music_assistant.controllers.webserver.helpers.auth_middleware import ImpersonatedUser
from music_assistant.helpers.ffmpeg import get_ffmpeg_stream
from music_assistant.helpers.uri import parse_uri
from music_assistant.helpers.util import join_task

from .constants import (
    CONF_SHOW_STOP_NOTIFICATION,
    DEFAULT_SHOW_STOP_NOTIFICATION,
    MSX_PLAYER_ID_PREFIX,
    PLAYER_ID_SANITIZE_RE,
    PRE_BUFFER_BYTES,
)
from .mappers import (
    append_device_param,
    get_image_url,
    map_album_to_msx,
    map_artist_to_msx,
    map_playlist_to_msx,
    map_track_to_msx,
    map_tracks_to_msx_playlist,
)
from .models import MsxContent, MsxItem, MsxTemplate
from .player import MSXPlayer

if TYPE_CHECKING:
    from collections.abc import Sequence

    from multidict import MultiMapping
    from music_assistant_models.player import PlayerMedia

    from music_assistant.helpers.dsp import ComplexFilter

    from .provider import MSXBridgeProvider

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

_KNOWN_EXTENSIONS = (".mp3", ".json", ".flac", ".aac")

PARTY_CACHE_TTL = 10.0
PARTY_CALL_TIMEOUT = 5.0

# The local proxy modes encode audio themselves, so they carry the core streamserver's
# pacing ceiling rather than handing a track over as fast as ffmpeg can produce it.
# See the usage policy note on SINGLE_ITEM_READRATE.
_READRATE_ARGS = [
    "-readrate",
    SINGLE_ITEM_READRATE,
    "-readrate_initial_burst",
    SINGLE_ITEM_READRATE_INITIAL_BURST,
]


class PartyInfo(NamedTuple):
    """Active-party details resolved from the MA Party plugin."""

    join_url: str
    name: str | None
    qr_text: str | None
    qr_version: str


def _int_param(query: MultiMapping[str], name: str, default: int, max_val: int = 10000) -> int:
    """Parse an integer query parameter safely, clamping to [0, max_val]."""
    try:
        return max(0, min(int(query.get(name, str(default))), max_val))
    except ValueError, TypeError:
        return default


async def _is_media_item_uri(uri: str) -> bool:
    """
    Check that a caller-supplied uri names a media item rather than a raw stream URL.

    Both spellings of a raw URL — bare, and wrapped as ``builtin://<media_type>/<url>`` —
    resolve to the builtin provider, which would make the server fetch and play whatever
    the caller names, so the resolved provider is what decides rather than the uri text.
    The bridge only ever hands out uris of library or music provider items.
    """
    if "://" not in uri:
        # keeps an item_id-shaped value away from parse_uri's local-file branch
        return False
    try:
        _, provider_instance_id_or_domain, _ = await parse_uri(uri)
    except InvalidProviderURI:
        return False
    return provider_instance_id_or_domain != "builtin"


def _is_audio_path(path: str) -> bool:
    """Check whether the path is one of the audio routes."""
    return path.startswith(("/stream/", "/msx/audio/"))


def _strip_known_extension(value: str) -> str:
    """Strip only known audio/data extensions from a value."""
    for ext in _KNOWN_EXTENSIONS:
        if value.endswith(ext):
            return value[: -len(ext)]
    return value


@functools.lru_cache(maxsize=4)
def _render_qr(join_url: str, kind: str) -> bytes:
    """
    Render the join URL as a QR image (blocking on a miss; run in a worker thread).

    Results are memoized — the output only changes when the join code rotates.
    """
    import segno  # noqa: PLC0415  # only needed when the Party plugin is used

    buf = io.BytesIO()
    segno.make(join_url, error="m").save(buf, kind=kind, scale=8)
    return buf.getvalue()


def _render_qr_cover(join_url: str, cover_bytes: bytes) -> bytes:
    """Render the QR and composite it onto the cover (blocking; run in a worker thread)."""
    return _stamp_qr_on_cover(cover_bytes, _render_qr(join_url, "png"))


def _stamp_qr_on_cover(cover_bytes: bytes, qr_bytes: bytes) -> bytes:
    """Composite the QR into the cover's bottom-right corner; returns PNG bytes."""
    from PIL import Image  # noqa: PLC0415  # only needed when the Party plugin is used

    cover = Image.open(io.BytesIO(cover_bytes)).convert("RGB")
    qr = Image.open(io.BytesIO(qr_bytes)).convert("RGB")
    # ~28% of the smaller cover side keeps the QR scannable without hiding the art;
    # NEAREST preserves the hard module edges QR readers need.
    side = max(48, min(cover.width, cover.height) * 28 // 100)
    qr = qr.resize((side, side), Image.Resampling.NEAREST)
    margin = side // 8
    cover.paste(qr, (cover.width - side - margin, cover.height - side - margin))
    out = io.BytesIO()
    cover.save(out, format="PNG")
    return out.getvalue()


def _sort_album_tracks(tracks: list[Any]) -> list[Any]:
    """
    Sort album tracks deterministically.

    MA sorts by (disc_number, track_number) but tracks with identical values
    get non-deterministic ordering between calls. Adding name as a tiebreaker
    ensures the display page and playlist endpoint always agree on track order.
    """
    return sorted(
        tracks,
        key=lambda t: (
            getattr(t, "disc_number", 0) or 0,
            getattr(t, "track_number", 0) or 0,
            getattr(t, "name", "") or "",
        ),
    )


class MSXHTTPServer:
    """HTTP server that serves MSX bootstrap, library API, and stream proxy."""

    def __init__(self, provider: MSXBridgeProvider, port: int) -> None:
        """Initialize the HTTP server."""
        self.provider = provider
        self.port = port
        self.app = web.Application(middlewares=[self._cors_middleware])
        self._runner: web.AppRunner | None = None
        self._ws_clients: dict[str, set[web.WebSocketResponse]] = {}
        self._active_stream_tasks: dict[str, set[asyncio.Task[None]]] = {}
        self._active_stream_transports: dict[str, set[Any]] = {}
        self._party_cache: tuple[float, PartyInfo | None] | None = None
        self._qr_cover_cache: dict[tuple[str, str], bytes] = {}
        self._qr_cover_inflight: dict[tuple[str, str], asyncio.Task[bytes]] = {}
        self._client_prefixes: dict[str, str] = {}
        self._setup_routes()

    async def start(self) -> None:
        """Start the HTTP server."""
        self._runner = web.AppRunner(self.app)
        await self._runner.setup()
        # reuse_address + reuse_port allow fast restart after reload.
        # 0.0.0.0 is required: MSX TVs on LAN must reach this server by host IP;
        # binding to 127.0.0.1 would prevent TV connections.
        site = web.TCPSite(
            self._runner,
            "0.0.0.0",
            self.port,
            reuse_address=True,
            reuse_port=True,
        )
        await site.start()
        logger.info("MSX Bridge HTTP server started on port %s", self.port)

    async def stop(self) -> None:
        """Stop the HTTP server."""
        # iterate over copies: closing a WS wakes its handler, whose cleanup
        # discards the WS from these collections mid-iteration
        for clients in list(self._ws_clients.values()):
            for ws in list(clients):
                if not ws.closed:
                    await ws.close()
        self._ws_clients.clear()
        for player_id in list(self._active_stream_tasks):
            self.cancel_streams_for_player(player_id)
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        logger.info("MSX Bridge HTTP server stopped")

    def broadcast_play(
        self,
        player_id: str,
        *,
        title: str | None = None,
        artist: str | None = None,
        image_url: str | None = None,
        duration: int | None = None,
        next_action: str | None = None,
        prev_action: str | None = None,
    ) -> None:
        """Notify subscribed WebSocket clients to start playback with metadata."""
        clients = self._ws_clients.get(player_id, set())
        if not clients:
            logger.warning(
                "broadcast_play: no WebSocket clients for player_id=%s (connected: %s)",
                player_id,
                list(self._ws_clients.keys()),
            )
            return
        logger.info(
            "broadcast_play: player_id=%s, sending to %d client(s)",
            player_id,
            len(clients),
        )

        # We always use direct stream for maximum compatibility.
        play_path = f"/stream/{player_id}?token={self.provider.get_stream_token(player_id)}"

        payload: dict[str, Any] = {
            "type": "play",
            "path": play_path,
            "player_id": player_id,
        }
        if title:
            payload["title"] = title
        if artist:
            payload["artist"] = artist
        if image_url:
            # During a party the play background carries the join QR (MSX has
            # no overlays); the endpoint falls back to the original image when
            # the party is over, so a stale cache entry here is harmless.
            if self._cached_party() and (client_prefix := self._client_prefixes.get(player_id)):
                image_url = (
                    f"{client_prefix}/api/party/qr-cover.png?image={quote(image_url, safe='')}"
                )
            payload["image_url"] = image_url
        if duration is not None:
            payload["duration"] = duration
        if next_action:
            payload["next_action"] = next_action
        if prev_action:
            payload["prev_action"] = prev_action
        msg = json.dumps(payload)
        for ws in list(clients):
            if not ws.closed:
                self.provider.mass.create_task(self._ws_send(ws, msg, player_id))

    def broadcast_playlist(self, player_id: str, playlist_url: str) -> None:
        """Notify subscribed WebSocket clients to load an MSX native playlist."""
        clients = self._ws_clients.get(player_id, set())
        if not clients:
            logger.warning(
                "broadcast_playlist: no WebSocket clients for player_id=%s (connected: %s)",
                player_id,
                list(self._ws_clients.keys()),
            )
            return
        logger.info(
            "broadcast_playlist: player_id=%s, url=%s, sending to %d client(s)",
            player_id,
            playlist_url,
            len(clients),
        )
        payload: dict[str, Any] = {
            "type": "playlist",
            "url": playlist_url,
            "player_id": player_id,
        }
        msg = json.dumps(payload)
        for ws in list(clients):
            if not ws.closed:
                self.provider.mass.create_task(self._ws_send(ws, msg, player_id))

    def broadcast_sendspin(self, player_id: str, url: str) -> None:
        """Notify WebSocket clients to open the Sendspin kiosk (bridge stream start)."""
        clients = self._ws_clients.get(player_id, set())
        if not clients:
            logger.warning(
                "broadcast_sendspin: no WebSocket clients for player_id=%s (connected: %s)",
                player_id,
                list(self._ws_clients.keys()),
            )
            return
        logger.info(
            "broadcast_sendspin: player_id=%s, url=%s, sending to %d client(s)",
            player_id,
            url,
            len(clients),
        )
        msg = json.dumps({"type": "sendspin", "url": url, "player_id": player_id})
        for ws in list(clients):
            if not ws.closed:
                self.provider.mass.create_task(self._ws_send(ws, msg, player_id))

    def broadcast_goto_index(self, player_id: str, index: int) -> None:
        """Notify subscribed WebSocket clients to jump to a playlist index."""
        clients = self._ws_clients.get(player_id, set())
        if not clients:
            return
        logger.info(
            "broadcast_goto_index: player_id=%s, index=%d, sending to %d client(s)",
            player_id,
            index,
            len(clients),
        )
        payload: dict[str, Any] = {"type": "goto_index", "index": index}
        msg = json.dumps(payload)
        for ws in list(clients):
            if not ws.closed:
                self.provider.mass.create_task(self._ws_send(ws, msg, player_id))

    def cancel_streams_for_player(self, player_id: str) -> None:
        """Cancel stream tasks and abort connections for the given player."""
        tasks = self._active_stream_tasks.pop(player_id, set())
        transports = self._active_stream_transports.pop(player_id, set())
        for task in tasks:
            if not task.done():
                task.cancel()
        for transport in transports:
            with contextlib.suppress(Exception):
                if transport and hasattr(transport, "abort"):
                    transport.abort()
        if tasks or transports:
            logger.debug(
                "Cancelled %d task(s), aborted %d transport(s) for player %s",
                len(tasks),
                len(transports),
                player_id,
            )

    def broadcast_pause(self, player_id: str) -> None:
        """Notify subscribed WebSocket clients to pause playback."""
        clients = self._ws_clients.get(player_id, set())
        if not clients:
            return
        logger.info(
            "broadcast_pause: player_id=%s, sending to %d client(s)",
            player_id,
            len(clients),
        )
        msg = json.dumps({"type": "pause"})
        for ws in list(clients):
            if not ws.closed:
                self.provider.mass.create_task(self._ws_send(ws, msg, player_id))

    def broadcast_resume(self, player_id: str) -> None:
        """Notify subscribed WebSocket clients to resume playback."""
        clients = self._ws_clients.get(player_id, set())
        if not clients:
            return
        logger.info(
            "broadcast_resume: player_id=%s, sending to %d client(s)",
            player_id,
            len(clients),
        )
        msg = json.dumps({"type": "resume"})
        for ws in list(clients):
            if not ws.closed:
                self.provider.mass.create_task(self._ws_send(ws, msg, player_id))

    def broadcast_stop(self, player_id: str) -> None:
        """Notify subscribed WebSocket clients to stop playback."""
        clients = self._ws_clients.get(player_id, set())
        if not clients:
            logger.warning(
                "broadcast_stop: no WebSocket clients for player_id=%s (connected: %s)",
                player_id,
                list(self._ws_clients.keys()),
            )
            return
        logger.info(
            "broadcast_stop: player_id=%s, sending to %d client(s)",
            player_id,
            len(clients),
        )
        show_notification = self.provider.config.get_value(
            CONF_SHOW_STOP_NOTIFICATION, DEFAULT_SHOW_STOP_NOTIFICATION
        )
        payload: dict[str, Any] = {
            "type": "stop",
            "showNotification": bool(show_notification),
        }
        msg = json.dumps(payload)
        for ws in list(clients):
            if not ws.closed:
                self.provider.mass.create_task(self._ws_send(ws, msg, player_id))

    def broadcast_seek(self, player_id: str, position_seconds: int) -> None:
        """Notify subscribed WebSocket clients to seek to a position."""
        clients = self._ws_clients.get(player_id, set())
        if not clients:
            logger.debug("broadcast_seek: no WebSocket clients for player_id=%s", player_id)
            return
        msg = json.dumps({"type": "seek", "position": position_seconds})
        for ws in list(clients):
            if not ws.closed:
                self.provider.mass.create_task(self._ws_send(ws, msg, player_id))

    def _setup_routes(self) -> None:
        """Register all HTTP routes."""
        self._setup_msx_routes()
        self._setup_api_routes()

    def _setup_msx_routes(self) -> None:
        """Register MSX bootstrap, content, and playback routes."""
        # MSX bootstrap
        self.app.router.add_get("/", self._handle_root)
        self.app.router.add_get("/msx/start.json", self._handle_start_json)
        self.app.router.add_get("/msx/launcher.json", self._handle_launcher_json)
        self.app.router.add_get("/msx/plugin.html", self._handle_msx_plugin_html)
        self.app.router.add_get(
            "/msx/tvx-plugin-module.min.js",
            self._serve_static("tvx-plugin-module.min.js"),
        )
        self.app.router.add_get("/msx/tvx-plugin.min.js", self._serve_static("tvx-plugin.min.js"))
        self.app.router.add_get("/msx/input.html", self._handle_msx_input_html)
        self.app.router.add_get("/msx/input.js", self._serve_static("input.js"))

        # MSX content pages (native MSX JSON navigation)
        self.app.router.add_get("/msx/menu.json", self._handle_msx_menu)
        self.app.router.add_get("/msx/albums.json", self._handle_msx_albums)
        self.app.router.add_get("/msx/artists.json", self._handle_msx_artists)
        self.app.router.add_get("/msx/playlists.json", self._handle_msx_playlists)
        self.app.router.add_get("/msx/tracks.json", self._handle_msx_tracks)
        self.app.router.add_get("/msx/recently-played.json", self._handle_msx_recently_played)
        self.app.router.add_get("/msx/search-page.json", self._handle_msx_search_page)
        self.app.router.add_get("/msx/search-input.json", self._handle_msx_search_input)
        self.app.router.add_get("/msx/search.json", self._handle_msx_search)
        self.app.router.add_get("/msx/party.json", self._handle_msx_party)

        # MSX detail pages
        self.app.router.add_get("/msx/albums/{item_id}/tracks.json", self._handle_msx_album_tracks)
        self.app.router.add_get(
            "/msx/artists/{item_id}/albums.json", self._handle_msx_artist_albums
        )
        self.app.router.add_get(
            "/msx/playlists/{item_id}/tracks.json", self._handle_msx_playlist_tracks
        )

        # MSX queue playlist (MA queue → MSX native playlist)
        self.app.router.add_get("/msx/queue-playlist/{player_id}.json", self._handle_queue_playlist)

        # MSX playlist endpoints (native MSX playlist JSON)
        self.app.router.add_get(
            "/msx/playlist/album/{item_id}.json", self._handle_msx_album_playlist
        )
        self.app.router.add_get(
            "/msx/playlist/playlist/{item_id}.json", self._handle_msx_playlist_playlist
        )
        self.app.router.add_get("/msx/playlist/tracks.json", self._handle_msx_tracks_playlist)
        self.app.router.add_get(
            "/msx/playlist/recently-played.json",
            self._handle_msx_recently_played_playlist,
        )
        self.app.router.add_get("/msx/playlist/search.json", self._handle_msx_search_playlist)

        # MSX audio playback
        self.app.router.add_get("/msx/audio/{player_id}", self._handle_msx_audio)
        self.app.router.add_get("/msx/audio/{player_id}.mp3", self._handle_msx_audio)

        # Kiosk web player (browser-based, no MSX app needed)
        self.app.router.add_get("/web", self._handle_web_app)
        self.app.router.add_static("/web/", STATIC_DIR / "web")

        # Health
        self.app.router.add_get("/health", self._handle_health)

        # WebSocket for push playback (MA -> MSX)
        self.app.router.add_get("/ws", self._handle_ws)

        # Stream proxy
        self.app.router.add_get("/stream/{player_id}", self._handle_stream)
        self.app.router.add_get("/stream/{player_id}.mp3", self._handle_stream)

    def _setup_api_routes(self) -> None:
        """Register Library and Playback API routes."""
        # Library API
        self.app.router.add_get("/api/albums", self._handle_albums)
        self.app.router.add_get("/api/albums/{item_id}/tracks", self._handle_album_tracks)
        self.app.router.add_get("/api/artists", self._handle_artists)
        self.app.router.add_get("/api/artists/{item_id}/albums", self._handle_artist_albums)
        self.app.router.add_get("/api/playlists", self._handle_playlists)
        self.app.router.add_get("/api/playlists/{item_id}/tracks", self._handle_playlist_tracks)
        self.app.router.add_get("/api/tracks", self._handle_tracks)
        self.app.router.add_get("/api/search", self._handle_search)
        self.app.router.add_get("/api/recently-played", self._handle_recently_played)
        self.app.router.add_get("/api/lyrics/{player_id}", self._handle_lyrics)
        self.app.router.add_get("/api/queue/{player_id}", self._handle_queue)
        self.app.router.add_get("/api/party", self._handle_party_status)
        self.app.router.add_get("/api/party/qr.svg", self._handle_party_qr)
        self.app.router.add_get("/api/party/qr.png", self._handle_party_qr)
        self.app.router.add_get("/api/party/qr-cover.png", self._handle_party_qr_cover)

        # Playback control — GET (MSX interaction plugin) + POST (web player,
        # dashboard). Never wildcard: extra methods only widen the CSRF surface.
        self.app.router.add_post("/api/play", self._handle_play)
        for path, handler in (
            ("/api/pause/{player_id}", self._handle_pause),
            ("/api/stop/{player_id}", self._handle_stop),
            ("/api/quick-stop/{player_id}", self._handle_quick_stop),
            ("/api/next/{player_id}", self._handle_next),
            ("/api/previous/{player_id}", self._handle_previous),
        ):
            self.app.router.add_get(path, handler)
            self.app.router.add_post(path, handler)

    # --- Server Lifecycle ---

    @web.middleware
    async def _cors_middleware(self, request: web.Request, handler: Any) -> web.StreamResponse:
        """
        Add CORS headers to all responses.

        Wildcard CORS is intentional: this server runs on LAN (default port 8099).
        The web player (/web) and MSX plugin (/msx/plugin.html) are served from the
        same origin, so browser playback-control POSTs are always same-origin.
        MSX TV app only makes GET requests. This matches MA's own webserver pattern.

        The audio routes are the exception and get no header at all: a media element
        plays a cross-origin source without CORS, and the kiosk visualizer reads the
        stream same-origin, so withholding it costs nothing and keeps a cross-origin
        fetch() from reading the audio.
        """
        if request.method == "OPTIONS":
            return web.Response(
                headers={
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                    "Access-Control-Allow-Headers": "*",
                }
            )
        response: web.StreamResponse = await handler(request)
        if not _is_audio_path(request.path):
            response.headers["Access-Control-Allow-Origin"] = "*"
        return response

    # --- MSX Bootstrap Routes ---

    async def _handle_root(self, request: web.Request) -> web.Response:
        """Serve status dashboard."""
        players = self.provider.players
        # base is derived from the Host header, so escape it before embedding in HTML
        prefix = self._get_prefix(request)
        base = html_escape(prefix)
        player_rows = []
        for p in players:
            row = (
                f'<li class="player-row"><span>'
                f"{html_escape(p.display_name)} — {html_escape(p.playback_state.value)}"
                f"</span>"
            )
            row += f'<form method="post" action="{base}/api/quick-stop/{html_escape(p.player_id)}" '
            row += 'style="display:inline">'
            row += '<button type="submit" class="btn">Quick stop</button></form></li>'
            player_rows.append(row)
        player_info = "".join(player_rows) if player_rows else ""

        # Build URLs
        safe_host: str = html_escape(request.host)  # escape for HTML display
        _raw_host: str = request.url.host or request.host.split(":")[0]  # IPv6-safe, no port
        hostname = f"[{_raw_host}]" if ":" in _raw_host else _raw_host
        sendspin_url = f"http://{hostname}:{SENDSPIN_SERVER_PORT}"
        kiosk_html5_url = f"{base}/web?kiosk=1"
        # escape the composed URL as a whole: host-derived prefix plus & separators
        sendspin_query = f"sendspin=1&sendspin_url={quote(sendspin_url, safe='')}"
        sendspin_web_url = html_escape(f"{prefix}/web?{sendspin_query}")
        sendspin_kiosk_url = html_escape(f"{prefix}/web?kiosk=1&{sendspin_query}")

        html = f"""<!DOCTYPE html>
<html>
<head><title>MSX Bridge</title>
<style>
body {{ font-family: system-ui, sans-serif; max-width: 800px; margin: 50px auto; padding: 20px; }}
.info {{ background: #e3f2fd; padding: 15px; border-radius: 5px; margin: 10px 0; }}
.info-sendspin {{ background: #e8f5e9; }}
code {{ background: #f5f5f5; padding: 2px 6px; border-radius: 3px; word-break: break-all; }}
.player-row {{ display: flex; align-items: center; gap: 12px; margin: 8px 0; list-style: none; }}
.player-row form {{ margin: 0; }}
.btn {{ padding: 6px 12px; border-radius: 4px; border: 1px solid #1976d2;
  background: #1976d2; color: white; cursor: pointer; font-size: 14px; }}
.btn:hover {{ background: #1565c0; }}
.link-row {{ margin: 8px 0; }}
.builder-row {{ margin: 6px 0; }}
.builder-row label {{ margin-right: 16px; cursor: pointer; }}
.link-row a {{ color: #1976d2; text-decoration: none; }}
.link-row a:hover {{ text-decoration: underline; }}
small {{ color: #666; display: block; margin-top: 4px; }}
</style>
</head>
<body>
<h1>MSX Music Assistant Bridge</h1>

<div class="info">
<h3>MSX Setup URL</h3>
<code>http://{safe_host}/msx/start.json</code>
</div>

<div class="info">
<h3>Web Player</h3>
<div class="link-row">
<a href="/web">http://{safe_host}/web</a>
<small>Browser-based player with library navigation (HTTP streaming)</small>
</div>
<div class="link-row">
<a href="{kiosk_html5_url}">Kiosk Mode (HTML5)</a>
<small>Fullscreen player with WebSocket push - ideal for dedicated displays</small>
</div>
</div>

<div class="info info-sendspin">
<h3>Sendspin Player (Synchronized Audio)</h3>
<div class="link-row">
<a href="{sendspin_web_url}">Web Player + Sendspin</a>
<small>Library navigation with clock-synchronized audio</small>
</div>
<div class="link-row">
<a href="{sendspin_kiosk_url}">Kiosk Mode (Sendspin)</a>
<small>Fullscreen player with clock-synchronized audio</small>
</div>
<div class="link-row" style="margin-top: 12px;">
<strong>Custom Sendspin URL:</strong><br>
<code>/web?kiosk=1&amp;sendspin=1&amp;sendspin_url=http://&lt;ma-server&gt;:{SENDSPIN_SERVER_PORT}</code>
</div>
</div>

<div class="info">
<h3>Kiosk URL Builder</h3>
<div id="kiosk-builder">
<div class="builder-row">
<label><input type="radio" name="kiosk-mode" value="html5" checked> HTML5</label>
<label><input type="radio" name="kiosk-mode" value="sendspin"> Sendspin</label>
</div>
<div class="builder-row">
<label><input type="checkbox" data-kiosk-param="controls" checked> Controls</label>
<label><input type="checkbox" data-kiosk-param="party" checked> Party QR</label>
<label><input type="checkbox" data-kiosk-param="viz" checked> Visualizer</label>
<label><input type="checkbox" data-kiosk-param="lyrics" checked> Lyrics</label>
</div>
<div class="link-row">
<a id="kiosk-builder-link" href="/web?kiosk=1" target="_blank">Open kiosk</a>
</div>
<code id="kiosk-builder-url"></code>
</div>
<script>
(function () {{
    var builder = document.getElementById('kiosk-builder');
    var link = document.getElementById('kiosk-builder-link');
    var urlOut = document.getElementById('kiosk-builder-url');

    function rebuild() {{
        var params = ['kiosk=1'];
        var mode = builder.querySelector('input[name="kiosk-mode"]:checked').value;
        if (mode === 'sendspin') {{
            params.push('sendspin=1');
        }}
        var boxes = builder.querySelectorAll('input[data-kiosk-param]');
        for (var i = 0; i < boxes.length; i++) {{
            // only non-default choices land in the URL
            if (!boxes[i].checked) {{
                params.push(boxes[i].getAttribute('data-kiosk-param') + '=0');
            }}
        }}
        var url = location.origin + '/web?' + params.join('&');
        link.href = url;
        urlOut.textContent = url;
    }}

    builder.addEventListener('change', rebuild);
    rebuild();
}})();
</script>
</div>

<div class="info">
<h3>Players</h3>
<ul>{player_info or "<li>No players registered</li>"}</ul>
</div>
</body>
</html>"""
        return web.Response(text=html, content_type="text/html")

    async def _handle_start_json(self, request: web.Request) -> web.Response:
        """Return MSX start configuration pointing to the launcher menu."""
        prefix = self._get_prefix(request)
        return web.json_response(
            {
                "name": "Music Assistant",
                "version": "1.0.7",
                "parameter": f"content:{prefix}/msx/launcher.json",
            }
        )

    async def _handle_launcher_json(self, request: web.Request) -> web.Response:
        """Return MSX launcher page with MSX Player and Web Kiosk options."""
        prefix = self._get_prefix(request)
        content = MsxContent(
            headline="Music Assistant",
            template=MsxTemplate(
                type="separate",
                layout="0,0,2,4",
                icon="msx-white-soft:music-note",
                action="content:{context:content}",
            ),
            items=[
                MsxItem(
                    label="MSX Player",
                    icon="msx-white-soft:tv",
                    action=f"menu:request:interaction:init@{prefix}/msx/plugin.html?v=8",
                ),
                MsxItem(
                    label="Web Kiosk",
                    icon="msx-white-soft:open-in-browser",
                    action=f"link:{prefix}/web?kiosk=1",
                ),
            ],
        )
        return web.json_response(content.model_dump(by_alias=True, exclude_none=True))

    def _serve_static(self, filename: str) -> Any:
        """Create a handler that serves a static file from the static directory."""
        path = STATIC_DIR / filename

        async def handler(_request: web.Request) -> web.FileResponse:
            return web.FileResponse(path)

        return handler

    async def _handle_msx_plugin_html(self, _request: web.Request) -> web.StreamResponse:
        """Serve plugin.html with cache-busting headers."""
        response = cast("web.StreamResponse", web.FileResponse(STATIC_DIR / "plugin.html"))
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response

    async def _handle_msx_input_html(self, request: web.Request) -> web.FileResponse:
        """Serve input.html and ensure player is registered when Search is opened."""
        await self._ensure_player_for_request(request)
        return web.FileResponse(STATIC_DIR / "input.html")

    async def _handle_web_app(self, request: web.Request) -> web.Response:
        """Serve the web player SPA (browser-based, no MSX app needed)."""
        response = cast("web.Response", web.FileResponse(STATIC_DIR / "web" / "index.html"))
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return response

    # --- MSX Content Pages (native MSX JSON) ---

    async def _handle_msx_menu(self, request: web.Request) -> web.Response:
        """Return the main library menu as an MSX content page."""
        _, device_param, _ = await self._ensure_player_for_request(request)
        prefix = self._get_prefix(request)
        items = [
            (
                "Recently played",
                "msx-white-soft:history",
                f"{prefix}/msx/recently-played.json",
            ),
            ("Albums", "msx-white-soft:album", f"{prefix}/msx/albums.json"),
            ("Artists", "msx-white-soft:person", f"{prefix}/msx/artists.json"),
            (
                "Playlists",
                "msx-white-soft:playlist-play",
                f"{prefix}/msx/playlists.json",
            ),
            ("Tracks", "msx-white-soft:audiotrack", f"{prefix}/msx/tracks.json"),
            ("Search", "search", f"{prefix}/msx/search-page.json"),
        ]
        if await self._get_active_party() is not None:
            items.append(("Party", "msx-white-soft:qr-code", f"{prefix}/msx/party.json"))
        content = MsxContent(
            headline="Music Assistant",
            template=MsxTemplate(
                type="separate",
                layout="0,0,2,4",
                icon="msx-white-soft:music-note",
                action="content:{context:content}",
            ),
            items=[
                MsxItem(
                    label=label,
                    icon=icon,
                    content=append_device_param(url, device_param),
                )
                for label, icon, url in items
            ],
        )
        return web.json_response(content.model_dump(by_alias=True, exclude_none=True))

    async def _handle_msx_albums(self, request: web.Request) -> web.Response:
        """Return albums as an MSX content page."""
        _, device_param, _ = await self._ensure_player_for_request(request)
        prefix = self._get_prefix(request)
        limit = _int_param(request.query, "limit", 50)
        offset = _int_param(request.query, "offset", 0)
        try:
            albums = await asyncio.wait_for(
                self.provider.mass.music.albums.library_items(
                    limit=limit, offset=offset, summary=False
                ),
                timeout=10.0,
            )
        except Exception:
            logger.exception("Failed to fetch albums")
            albums = []

        items = await asyncio.gather(
            *(map_album_to_msx(a, prefix, self.provider, device_param) for a in albums)
        )
        content = MsxContent(
            headline="Albums",
            template=MsxTemplate(
                type="separate",
                layout="0,0,3,4",
                color="msx-glass",
            ),
            items=items if items else [MsxItem(title="No albums found")],
        )
        return web.json_response(content.model_dump(by_alias=True, exclude_none=True))

    async def _handle_msx_artists(self, request: web.Request) -> web.Response:
        """Return artists as an MSX content page."""
        _, device_param, _ = await self._ensure_player_for_request(request)
        prefix = self._get_prefix(request)
        limit = _int_param(request.query, "limit", 50)
        offset = _int_param(request.query, "offset", 0)
        try:
            artists = await asyncio.wait_for(
                self.provider.mass.music.artists.library_items(
                    limit=limit, offset=offset, summary=False
                ),
                timeout=10.0,
            )
        except Exception:
            logger.exception("Failed to fetch artists")
            artists = []

        items = [map_artist_to_msx(a, prefix, self.provider, device_param) for a in artists]
        content = MsxContent(
            headline="Artists",
            template=MsxTemplate(
                type="separate",
                layout="0,0,2,3",
                color="msx-glass",
            ),
            items=items if items else [MsxItem(title="No artists found")],
        )
        return web.json_response(content.model_dump(by_alias=True, exclude_none=True))

    async def _handle_msx_playlists(self, request: web.Request) -> web.Response:
        """Return playlists as an MSX content page."""
        _, device_param, _ = await self._ensure_player_for_request(request)
        prefix = self._get_prefix(request)
        limit = _int_param(request.query, "limit", 50)
        offset = _int_param(request.query, "offset", 0)
        try:
            playlists = await asyncio.wait_for(
                self.provider.mass.music.playlists.library_items(
                    limit=limit, offset=offset, summary=False
                ),
                timeout=10.0,
            )
        except Exception:
            logger.exception("Failed to fetch playlists")
            playlists = []

        items = [map_playlist_to_msx(p, prefix, self.provider, device_param) for p in playlists]
        content = MsxContent(
            headline="Playlists",
            template=MsxTemplate(
                type="separate",
                layout="0,0,3,4",
                color="msx-glass",
            ),
            items=items if items else [MsxItem(title="No playlists found")],
        )
        return web.json_response(content.model_dump(by_alias=True, exclude_none=True))

    async def _handle_msx_tracks(self, request: web.Request) -> web.Response:
        """Return tracks as an MSX content page."""
        player_id, device_param, _ = await self._ensure_player_for_request(request)
        prefix = self._get_prefix(request)
        limit = _int_param(request.query, "limit", 50)
        offset = _int_param(request.query, "offset", 0)
        try:
            tracks = await asyncio.wait_for(
                self.provider.mass.music.tracks.library_items(
                    limit=limit, offset=offset, summary=False
                ),
                timeout=10.0,
            )
        except Exception:
            logger.exception("Failed to fetch tracks")
            tracks = []

        playlist_base = f"{prefix}/msx/playlist/tracks.json?limit={limit}&offset={offset}"
        playlist_base = append_device_param(playlist_base, device_param)
        items = [
            map_track_to_msx(
                t,
                prefix,
                player_id,
                self.provider,
                device_param,
                playlist_url=f"{playlist_base}&start={idx}",
            )
            for idx, t in enumerate(tracks)
        ]
        content = MsxContent(
            headline="Tracks",
            template=MsxTemplate(
                type="default",
                layout="0,0,6,1",
                image_width=0.83,
                color="msx-glass",
            ),
            items=items if items else [MsxItem(title="No tracks found")],
        )
        return web.json_response(content.model_dump(by_alias=True, exclude_none=True))

    async def _handle_msx_recently_played(self, request: web.Request) -> web.Response:
        """Return recently played tracks as an MSX content page."""
        player_id, device_param, _ = await self._ensure_player_for_request(request)
        prefix = self._get_prefix(request)
        try:
            tracks = await asyncio.wait_for(
                self.provider.mass.music.tracks.library_items(
                    limit=50, order_by="last_played", summary=False
                ),
                timeout=10.0,
            )
        except Exception:
            logger.exception("Failed to fetch recently played tracks")
            tracks = []
        playlist_base = f"{prefix}/msx/playlist/recently-played.json"
        playlist_base = append_device_param(playlist_base, device_param)
        items = [
            map_track_to_msx(
                t,
                prefix,
                player_id,
                self.provider,
                device_param,
                playlist_url=f"{playlist_base}{'&' if '?' in playlist_base else '?'}start={idx}",
            )
            for idx, t in enumerate(tracks)
        ]
        content = MsxContent(
            headline="Recently played",
            template=MsxTemplate(
                type="default",
                layout="0,0,6,1",
                image_width=0.83,
                color="msx-glass",
            ),
            items=items if items else [MsxItem(title="No recently played tracks")],
        )
        return web.json_response(content.model_dump(by_alias=True, exclude_none=True))

    async def _handle_msx_search_page(self, request: web.Request) -> web.Response:
        """Return a content page whose page-level action launches the Input Plugin keyboard."""
        _, device_param, _ = await self._ensure_player_for_request(request)
        prefix = self._get_prefix(request)
        search_url = append_device_param(
            f"{prefix}/msx/search-input.json?q={{INPUT}}", device_param
        )
        action = (
            f"content:request:interaction:"
            f"{search_url}"
            f"|search:3|en|Search Music||||Search..."
            f"@{prefix}/msx/input.html"
        )
        content = MsxContent(
            headline="Search",
            action=action,
            template=MsxTemplate(
                type="separate",
                layout="0,0,2,4",
            ),
            items=[
                MsxItem(
                    title="Search Music",
                    title_footer="Press OK to open keyboard",
                    icon="search",
                    action=action,
                )
            ],
        )
        return web.json_response(content.model_dump(by_alias=True, exclude_none=True))

    async def _handle_msx_search_input(self, request: web.Request) -> web.Response:
        """Return search results for the MSX Input Plugin (search keyboard)."""
        player_id, device_param, _ = await self._ensure_player_for_request(request)
        prefix = self._get_prefix(request)
        query = request.query.get("q", "")
        if not query:
            content = MsxContent(
                headline="{ico:search} Search",
                hint="Type to search...",
                template=MsxTemplate(
                    type="separate",
                    layout="0,0,2,4",
                    image_filler="default",
                ),
                items=[MsxItem(title="Start typing to search")],
            )
            return web.json_response(content.model_dump(by_alias=True, exclude_none=True))

        limit = _int_param(request.query, "limit", 20)
        items = await self._build_search_items(
            query,
            limit,
            player_id,
            device_param,
            prefix,
        )

        content = MsxContent(
            headline=f'{{ico:search}} "{query}"',
            hint=f"Found {len(items)} items",
            template=MsxTemplate(
                type="separate",
                layout="0,0,2,4",
                image_filler="default",
            ),
            items=items if items else [MsxItem(title="No results found")],
        )
        return web.json_response(content.model_dump(by_alias=True, exclude_none=True))

    async def _handle_msx_search(self, request: web.Request) -> web.Response:
        """Return search results as an MSX content page."""
        player_id, device_param, _ = await self._ensure_player_for_request(request)
        prefix = self._get_prefix(request)
        query = request.query.get("q", "")
        if not query:
            return web.json_response(
                MsxContent(
                    headline="Search",
                    items=[MsxItem(title="Please enter a search query")],
                ).model_dump(by_alias=True, exclude_none=True)
            )

        limit = _int_param(request.query, "limit", 20)
        items = await self._build_search_items(
            query,
            limit,
            player_id,
            device_param,
            prefix,
        )

        content = MsxContent(
            headline=f"Search: {query}",
            template=MsxTemplate(
                type="separate",
                layout="0,0,2,4",
                image_filler="default",
            ),
            items=items if items else [MsxItem(title="No results found")],
        )
        return web.json_response(content.model_dump(by_alias=True, exclude_none=True))

    async def _handle_msx_party(self, request: web.Request) -> web.Response:
        """Return MSX page with the party QR code, or a hint when no party is active."""
        await self._ensure_player_for_request(request)
        prefix = self._get_prefix(request)
        party = await self._get_active_party()
        if party is None:
            item = MsxItem(
                title="No active party",
                label="Enable guest access in the Music Assistant Party plugin",
            )
        else:
            # PNG, not SVG: MSX image slots on older TV engines cannot decode SVG
            item = MsxItem(
                image=f"{prefix}/api/party/qr.png",
                label=party.qr_text or "Scan to join the party",
            )
        content = MsxContent(
            headline=(party.name if party else None) or "Party",
            template=MsxTemplate(type="separate", layout="0,0,4,4"),
            items=[item],
        )
        return web.json_response(content.model_dump(by_alias=True, exclude_none=True))

    async def _build_search_items(
        self,
        query: str,
        limit: int,
        player_id: str,
        device_param: str,
        prefix: str,
    ) -> list[MsxItem]:
        """Build MSX items from search results (shared by search handlers)."""
        results = await self.provider.mass.music.search(query, limit=limit)
        items: list[MsxItem] = []
        for artist in results.artists:
            item = map_artist_to_msx(artist, prefix, self.provider, device_param)
            item.label = "Artist"
            item.icon = "msx-white-soft:person"
            items.append(item)
        for album in results.albums:
            item = await map_album_to_msx(album, prefix, self.provider, device_param)
            item.label = f"Album — {getattr(album, 'artist_str', '')}"
            item.icon = "msx-white-soft:album"
            items.append(item)
        playlist_base = f"{prefix}/msx/playlist/search.json?q={quote(query, safe='')}"
        playlist_base = append_device_param(playlist_base, device_param)
        for idx, track in enumerate(results.tracks):
            item = map_track_to_msx(
                track,
                prefix,
                player_id,
                self.provider,
                device_param,
                playlist_url=f"{playlist_base}&start={idx}",
            )
            item.label = f"Track — {getattr(track, 'artist_str', '')}"
            item.icon = "msx-white-soft:audiotrack"
            items.append(item)
        return items

    # --- MSX Detail Pages ---

    async def _handle_msx_album_tracks(self, request: web.Request) -> web.Response:
        """Return tracks for an album as an MSX content page."""
        player_id, device_param, _ = await self._ensure_player_for_request(request)
        prefix = self._get_prefix(request)
        item_id = request.match_info["item_id"]
        provider = request.query.get("provider", "library")
        try:
            tracks = _sort_album_tracks(
                await self.provider.mass.music.albums.tracks(item_id, provider)
            )
        except Exception:
            logger.exception("Failed to fetch tracks for album %s", item_id)
            tracks = []
        playlist_base = f"{prefix}/msx/playlist/album/{item_id}.json?provider={provider}"
        playlist_base = append_device_param(playlist_base, device_param)
        items = [
            map_track_to_msx(
                t,
                prefix,
                player_id,
                self.provider,
                device_param,
                playlist_url=f"{playlist_base}&start={idx}",
            )
            for idx, t in enumerate(tracks)
        ]
        content = MsxContent(
            headline="Album Tracks",
            template=MsxTemplate(
                type="default",
                layout="0,0,6,1",
                image_width=0.83,
                color="msx-glass",
            ),
            items=items if items else [MsxItem(title="No tracks found")],
        )
        return web.json_response(content.model_dump(by_alias=True, exclude_none=True))

    async def _handle_msx_artist_albums(self, request: web.Request) -> web.Response:
        """Return albums for an artist as an MSX content page."""
        _, device_param, _ = await self._ensure_player_for_request(request)
        prefix = self._get_prefix(request)
        item_id = request.match_info["item_id"]
        try:
            albums = await self.provider.mass.music.artists.albums(item_id, "library")
        except Exception:
            logger.exception("Failed to fetch albums for artist %s", item_id)
            albums = []

        items = await asyncio.gather(
            *(map_album_to_msx(a, prefix, self.provider, device_param) for a in albums)
        )
        content = MsxContent(
            headline="Artist Albums",
            template=MsxTemplate(
                type="default",
                layout="0,0,6,2",
                image_width=1.5,
                color="msx-glass",
            ),
            items=items if items else [MsxItem(title="No albums found")],
        )
        return web.json_response(content.model_dump(by_alias=True, exclude_none=True))

    async def _handle_msx_playlist_tracks(self, request: web.Request) -> web.Response:
        """Return tracks for a playlist as an MSX content page."""
        player_id, device_param, _ = await self._ensure_player_for_request(request)
        prefix = self._get_prefix(request)
        item_id = request.match_info["item_id"]
        try:
            tracks = [
                t async for t in self.provider.mass.music.playlists.tracks(item_id, "library")
            ]
        except Exception:
            logger.exception("Failed to fetch tracks for playlist %s", item_id)
            tracks = []
        playlist_base = f"{prefix}/msx/playlist/playlist/{item_id}.json"
        playlist_base = append_device_param(playlist_base, device_param)
        items = [
            map_track_to_msx(
                t,
                prefix,
                player_id,
                self.provider,
                device_param,
                playlist_url=f"{playlist_base}{'&' if '?' in playlist_base else '?'}start={idx}",
            )
            for idx, t in enumerate(tracks)
        ]
        content = MsxContent(
            headline="Playlist Tracks",
            template=MsxTemplate(
                type="default",
                layout="0,0,6,1",
                image_width=0.83,
                color="msx-glass",
            ),
            items=items if items else [MsxItem(title="No tracks found")],
        )
        return web.json_response(content.model_dump(by_alias=True, exclude_none=True))

    # --- MSX Playlist Endpoints ---

    async def _handle_msx_album_playlist(self, request: web.Request) -> web.Response:
        """Return album tracks as an MSX playlist JSON."""
        player_id, device_param, _ = await self._ensure_player_for_request(request)
        prefix = self._get_prefix(request)
        item_id = request.match_info["item_id"]
        provider_name = request.query.get("provider", "library")
        start = _int_param(request.query, "start", 0)
        try:
            tracks = _sort_album_tracks(
                await self.provider.mass.music.albums.tracks(item_id, provider_name)
            )
        except Exception:
            logger.exception("Failed to fetch tracks for album playlist %s", item_id)
            tracks = []
        playlist = map_tracks_to_msx_playlist(
            tracks,
            start,
            prefix,
            player_id,
            self.provider,
            device_param,
            qr_cover_base=await self._qr_cover_base(prefix),
        )
        return web.json_response(playlist.model_dump(by_alias=True, exclude_none=True))

    async def _handle_msx_playlist_playlist(self, request: web.Request) -> web.Response:
        """Return playlist tracks as an MSX playlist JSON."""
        player_id, device_param, _ = await self._ensure_player_for_request(request)
        prefix = self._get_prefix(request)
        item_id = request.match_info["item_id"]
        start = _int_param(request.query, "start", 0)
        try:
            tracks = [
                t async for t in self.provider.mass.music.playlists.tracks(item_id, "library")
            ]
        except Exception:
            logger.exception("Failed to fetch tracks for playlist playlist %s", item_id)
            tracks = []
        playlist = map_tracks_to_msx_playlist(
            tracks,
            start,
            prefix,
            player_id,
            self.provider,
            device_param,
            qr_cover_base=await self._qr_cover_base(prefix),
        )
        return web.json_response(playlist.model_dump(by_alias=True, exclude_none=True))

    async def _handle_msx_tracks_playlist(self, request: web.Request) -> web.Response:
        """Return library tracks as an MSX playlist JSON."""
        player_id, device_param, _ = await self._ensure_player_for_request(request)
        prefix = self._get_prefix(request)
        limit = _int_param(request.query, "limit", 50)
        offset = _int_param(request.query, "offset", 0)
        start = _int_param(request.query, "start", 0)
        tracks = await self.provider.mass.music.tracks.library_items(
            limit=limit, offset=offset, summary=False
        )
        playlist = map_tracks_to_msx_playlist(
            list(tracks),
            start,
            prefix,
            player_id,
            self.provider,
            device_param,
            qr_cover_base=await self._qr_cover_base(prefix),
        )
        return web.json_response(playlist.model_dump(by_alias=True, exclude_none=True))

    async def _handle_msx_recently_played_playlist(self, request: web.Request) -> web.Response:
        """Return recently played tracks as an MSX playlist JSON."""
        player_id, device_param, _ = await self._ensure_player_for_request(request)
        prefix = self._get_prefix(request)
        start = _int_param(request.query, "start", 0)
        tracks = await self.provider.mass.music.tracks.library_items(
            limit=50, order_by="last_played", summary=False
        )
        playlist = map_tracks_to_msx_playlist(
            list(tracks),
            start,
            prefix,
            player_id,
            self.provider,
            device_param,
            qr_cover_base=await self._qr_cover_base(prefix),
        )
        return web.json_response(playlist.model_dump(by_alias=True, exclude_none=True))

    async def _handle_msx_search_playlist(self, request: web.Request) -> web.Response:
        """Return search track results as an MSX playlist JSON."""
        player_id, device_param, _ = await self._ensure_player_for_request(request)
        prefix = self._get_prefix(request)
        query = request.query.get("q", "")
        start = _int_param(request.query, "start", 0)
        if not query:
            return web.json_response(
                MsxContent(items=[]).model_dump(by_alias=True, exclude_none=True)
            )
        limit = _int_param(request.query, "limit", 20)
        results = await self.provider.mass.music.search(query, limit=limit)
        playlist = map_tracks_to_msx_playlist(
            list(results.tracks),
            start,
            prefix,
            player_id,
            self.provider,
            device_param,
            qr_cover_base=await self._qr_cover_base(prefix),
        )
        return web.json_response(playlist.model_dump(by_alias=True, exclude_none=True))

    # --- MSX Queue Playlist ---

    async def _handle_queue_playlist(self, request: web.Request) -> web.Response:
        """Return the current MA queue as an MSX native playlist."""
        _, device_param, _ = await self._ensure_player_for_request(request)
        prefix = self._get_prefix(request)
        player_id = request.match_info["player_id"]
        queue_id = request.query.get("queue_id", player_id)
        start = _int_param(request.query, "start", 0)

        try:
            queue_items = self.provider.mass.player_queues.items(queue_id)
        except Exception:
            logger.exception("Failed to fetch queue items for %s", player_id)
            queue_items = []

        # Convert QueueItems to track-like objects for map_tracks_to_msx_playlist
        tracks: list[Any] = []
        for qi in queue_items:
            mi = getattr(qi, "media_item", None)
            tracks.append(
                SimpleNamespace(
                    name=getattr(mi, "name", None) or getattr(qi, "name", "") or "",
                    uri=getattr(mi, "uri", None) or "",
                    duration=getattr(mi, "duration", None) or getattr(qi, "duration", 0) or 0,
                    artist_str=getattr(mi, "artist_str", "") if mi else "",
                    image=getattr(qi, "image", None),
                )
            )

        playlist = map_tracks_to_msx_playlist(
            tracks,
            start,
            prefix,
            player_id,
            self.provider,
            device_param,
            qr_cover_base=await self._qr_cover_base(prefix),
        )
        return web.json_response(playlist.model_dump(by_alias=True, exclude_none=True))

    # --- MSX Audio Playback ---

    async def _handle_msx_audio(self, request: web.Request) -> web.StreamResponse:
        """Trigger playback via MA queue and stream audio to MSX."""
        player_id = _strip_known_extension(request.match_info["player_id"])

        uri = request.query.get("uri")
        if not uri or not await _is_media_item_uri(uri):
            return web.Response(status=400, text="Invalid uri parameter")

        from_playlist = request.query.get("from_playlist") == "1"

        player = self.provider.mass.players.get_player(player_id)
        if not player or not isinstance(player, MSXPlayer):
            return web.Response(status=404, text="Player not found")
        if rejected := self._reject_invalid_stream_token(request, player_id):
            return rejected
        self.provider.on_player_activity(player_id)

        # When MA is driving the queue (next/prev from MA UI), current_media is
        # already set by player.play_media() before the WS goto_index reaches MSX.
        # Re-enqueuing would recreate the queue from the track URI, destroying it.
        # We verify by checking that current_media's queue item URI matches the
        # requested track URI — if not, MSX auto-advanced and we must re-enqueue.
        if (
            from_playlist
            and player._playing_from_queue
            and self._current_media_matches_uri(player, uri)
        ):
            logger.debug("Queue-driven: using current_media for %s", uri)
            media = player.current_media
        else:
            # Suppress WS broadcast when called from MSX playlist to avoid conflicts
            if from_playlist:
                player._skip_ws_notify = True

            # Arm BEFORE enqueuing so wait_for_media() waits for the new track's
            # play_media() instead of returning the previous track's media.
            player.expect_new_media()
            try:
                async with ImpersonatedUser(
                    self.provider.mass, await self.provider.get_owner_username()
                ):
                    await self.provider.mass.player_queues.play_media(player_id, uri)
            finally:
                if from_playlist:
                    player._skip_ws_notify = False

            # Wait for play_media() to signal media is ready (replaces 10s polling loop)
            media = await player.wait_for_media(timeout=10.0)

        if not media:
            return web.Response(status=504, text="Playback setup timeout")

        return await self._serve_audio_stream(
            request,
            player,
            media,
            duration=self._resolve_served_duration(media),
        )

    # --- Audio Streaming Infrastructure ---

    def _resolve_served_duration(self, media: PlayerMedia) -> int:
        """
        Return the length in seconds of the audio served for the given media, or 0 if unknown.

        This is what the Content-Length header is derived from, so it describes
        the audio we actually serve rather than the media item: starting
        playback at a seek position yields a shorter stream.

        :param media: The media being served.
        """
        duration = media.stream_duration or media.duration or 0
        if not duration and media.source_id and media.queue_item_id:
            queue_item = self.provider.mass.player_queues.get_item(
                media.source_id, media.queue_item_id
            )
            if queue_item:
                if queue_item.media_item:
                    duration = getattr(queue_item.media_item, "duration", None) or duration
                if not duration and queue_item.duration:
                    duration = queue_item.duration
        return int(duration)

    @staticmethod
    def _build_audio_params(
        output_format_str: str, duration: int
    ) -> tuple[AudioFormat, AudioFormat, dict[str, str]]:
        """Build PCM input format, encoded output format, and HTTP headers."""
        pcm_format = AudioFormat(
            content_type=ContentType.PCM_S16LE,
            sample_rate=44100,
            bit_depth=16,
            channels=2,
        )
        content_type_map: dict[str, tuple[ContentType, str]] = {
            "mp3": (ContentType.MP3, "audio/mpeg"),
            "aac": (ContentType.AAC, "audio/aac"),
            "flac": (ContentType.FLAC, "audio/flac"),
        }
        codec, mime_type = content_type_map.get(output_format_str, (ContentType.MP3, "audio/mpeg"))
        out_format = AudioFormat(
            content_type=codec,
            sample_rate=44100,
            bit_depth=16,
            channels=2,
        )
        bitrate_map = {"mp3": 40_000, "aac": 32_000}
        bytes_per_sec = bitrate_map.get(output_format_str, 0)
        headers: dict[str, str] = {
            "Content-Type": mime_type,
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Accept-Ranges": "none",
        }
        if duration and bytes_per_sec:
            capped_duration = min(float(duration), 43200)  # cap at 12h
            headers["Content-Length"] = str(int(capped_duration * bytes_per_sec))
        return pcm_format, out_format, headers

    async def _serve_audio_stream(
        self,
        request: web.Request,
        player: MSXPlayer,
        media: Any,
        duration: int = 0,
    ) -> web.StreamResponse:
        """
        Unified method to stream audio from MA to MSX via ffmpeg.

        Supports three modes based on provider configuration:
        1. Independent (default): Each player gets its own ffmpeg stream
        2. Shared Buffer: Group members share one ffmpeg process via SharedGroupStream
        3. MA Redirect: 302 redirect to MA Streamserver (requires MA 2.6+)

        Pre-buffers audio data before sending HTTP headers so MSX receives
        the response and initial audio burst simultaneously, preventing
        stutter/restart from an empty initial buffer.
        """
        player_id = player.player_id

        # --- Mode 1: MA Redirect ---
        if self.provider.is_redirect_stream_mode():
            redirect_url = await self.provider.get_ma_stream_url(player_id, media)
            if redirect_url:
                redirect_url = self._rewrite_stream_host(request, redirect_url)
                logger.info(
                    "[StreamMode:redirect] Player %s -> MA Streamserver: %s",
                    player_id,
                    redirect_url,
                )
                raise web.HTTPFound(location=redirect_url)
            # Fallback to independent mode if redirect fails
            logger.warning(
                "[StreamMode:redirect] Failed to get MA URL for %s, "
                "falling back to independent mode",
                player_id,
            )

        # Resolve effective output format: per-player config overrides provider default.
        # CONF_ENTRY_OUTPUT_CODEC_DEFAULT_MP3 uses key "output_codec"; fall back to
        # player.output_format (set from provider-level config during registration).
        # Only the proxy paths below need this — in redirect mode the MA streamserver
        # applies the same per-player codec config itself.
        effective_format = cast(
            "str",
            player.config.get_value("output_codec", player.output_format),
        )

        pcm_format, out_format, headers = self._build_audio_params(
            effective_format,
            duration,
        )

        # --- Mode 2: Shared Buffer (for groups) ---
        group_id = self.provider.get_group_id_for_player(player)
        if group_id and self.provider.is_shared_stream_mode():
            logger.info(
                "[StreamMode:shared] Player %s in group %s, using shared stream",
                player_id,
                group_id,
            )
            return await self._serve_shared_stream(
                request, player, media, group_id, pcm_format, out_format, headers
            )

        # --- Mode 3: Independent (default) ---
        logger.debug(
            "[StreamMode:independent] Serving audio %s: format=%s, duration=%s",
            player_id,
            effective_format,
            duration,
        )

        audio_source = self.provider.mass.streams.get_stream(
            media,
            pcm_format,
            force_flow_mode=False,
        )
        output_plan = self.provider.mass.streams.audio.get_player_output_plan(
            player_id,
            pcm_format,
            out_format,
            queue_id=getattr(media, "source_id", None),
            session_id=get_media_session_id(media),
            queue_item_id=getattr(media, "queue_item_id", None),
        )

        response = web.StreamResponse(status=200, headers=headers)
        stream_task: asyncio.Task[None] = asyncio.create_task(
            self._stream_with_prebuffer(
                request,
                response,
                player,
                headers,
                audio_source,
                pcm_format,
                out_format,
                output_plan.filter_params,
            )
        )
        transport = getattr(request, "transport", None)
        await self._run_stream_task(player_id, stream_task, transport)

        return response

    async def _serve_shared_stream(
        self,
        request: web.Request,
        player: MSXPlayer,
        media: Any,
        group_id: str,
        pcm_format: AudioFormat,
        out_format: AudioFormat,
        headers: dict[str, str],
    ) -> web.StreamResponse:
        """
        Serve audio from a shared group stream.

        Multiple players in a group read from the same SharedGroupStream,
        which has a single ffmpeg producer.
        """
        player_id = player.player_id
        media_uri = getattr(media, "uri", "") or str(media)

        # Check if we need to create a new shared stream (leader creates it)
        existing_stream = self.provider._shared_streams.get(group_id)
        is_leader = player_id == group_id

        if existing_stream and not existing_stream.finished:
            # Reuse existing stream
            logger.debug(
                "[SharedStream] Player %s subscribing to existing stream for group %s",
                player_id,
                group_id,
            )
            shared_stream = existing_stream
        elif is_leader:
            # Leader creates the shared stream
            logger.info(
                "[SharedStream] Leader %s creating shared stream for group %s",
                player_id,
                group_id,
            )
            audio_source = self.provider.mass.streams.get_stream(
                media,
                pcm_format,
                force_flow_mode=False,
            )
            output_plan = self.provider.mass.streams.audio.get_player_output_plan(
                player_id,
                pcm_format,
                out_format,
                queue_id=getattr(media, "source_id", None),
                session_id=get_media_session_id(media),
                queue_item_id=getattr(media, "queue_item_id", None),
            )
            # Create ffmpeg chunk generator
            audio_chunks = get_ffmpeg_stream(
                audio_input=audio_source,
                input_format=pcm_format,
                output_format=out_format,
                filter_params=output_plan.filter_params,
                extra_input_args=_READRATE_ARGS,
            )
            shared_stream = await self.provider.get_or_create_shared_stream(
                group_id, media_uri, audio_chunks
            )
            shared_stream.output_plan = output_plan
        else:
            # Member but no existing stream - wait briefly for leader
            logger.info(
                "[SharedStream] Member %s waiting for leader to create stream for group %s",
                player_id,
                group_id,
            )
            for _ in range(30):  # Wait up to 3 seconds
                await asyncio.sleep(0.1)
                existing_stream = self.provider._shared_streams.get(group_id)
                if existing_stream and not existing_stream.finished:
                    shared_stream = existing_stream
                    break
            else:
                # Timeout - fallback to independent stream
                logger.warning(
                    "[SharedStream] Timeout waiting for leader stream, "
                    "falling back to independent for %s",
                    player_id,
                )
                return await self._serve_independent_stream(
                    request, player, media, pcm_format, out_format, headers
                )

        queue_id = getattr(media, "source_id", None)
        session_id = get_media_session_id(media)
        if (
            shared_stream.output_plan is not None
            and queue_id is not None
            and session_id is not None
        ):
            self.provider.mass.streams.audio_processing.update_output(
                player_id,
                shared_stream.output_plan,
                queue_id=queue_id,
                session_id=session_id,
                queue_item_id=getattr(media, "queue_item_id", None),
            )

        # Subscribe to shared stream
        response = web.StreamResponse(status=200, headers=headers)
        await response.prepare(request)

        total_bytes = 0
        try:
            async for chunk in shared_stream.subscribe(player_id):
                await response.write(chunk)
                total_bytes += len(chunk)
        except ConnectionResetError, BrokenPipeError, ConnectionAbortedError:
            logger.debug(
                "[SharedStream] Client %s disconnected after %d bytes",
                player_id,
                total_bytes,
            )
        except asyncio.CancelledError:
            logger.debug("[SharedStream] Stream cancelled for %s", player_id)
            raise

        logger.info(
            "[SharedStream] Player %s finished, wrote %d bytes",
            player_id,
            total_bytes,
        )
        return response

    async def _serve_independent_stream(
        self,
        request: web.Request,
        player: MSXPlayer,
        media: Any,
        pcm_format: AudioFormat,
        out_format: AudioFormat,
        headers: dict[str, str],
    ) -> web.StreamResponse:
        """Serve audio via independent ffmpeg stream (fallback)."""
        player_id = player.player_id
        logger.debug(
            "[StreamMode:independent] Fallback stream for %s",
            player_id,
        )

        audio_source = self.provider.mass.streams.get_stream(
            media,
            pcm_format,
            force_flow_mode=False,
        )
        output_plan = self.provider.mass.streams.audio.get_player_output_plan(
            player_id,
            pcm_format,
            out_format,
            queue_id=getattr(media, "source_id", None),
            session_id=get_media_session_id(media),
            queue_item_id=getattr(media, "queue_item_id", None),
        )

        response = web.StreamResponse(status=200, headers=headers)
        stream_task: asyncio.Task[None] = asyncio.create_task(
            self._stream_with_prebuffer(
                request,
                response,
                player,
                headers,
                audio_source,
                pcm_format,
                out_format,
                output_plan.filter_params,
            )
        )
        transport = getattr(request, "transport", None)
        await self._run_stream_task(player_id, stream_task, transport)

        return response

    async def _stream_with_prebuffer(
        self,
        request: web.Request,
        response: web.StreamResponse,
        player: MSXPlayer,
        headers: dict[str, str],
        audio_source: Any,
        pcm_format: AudioFormat,
        out_format: AudioFormat,
        filter_params: Sequence[str | ComplexFilter],
    ) -> None:
        """Pre-buffer audio chunks, then send HTTP headers and stream remaining data."""
        player_id = player.player_id
        chunk_queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=32)

        async def producer() -> None:
            try:
                async for chunk in get_ffmpeg_stream(
                    audio_input=audio_source,
                    input_format=pcm_format,
                    output_format=out_format,
                    filter_params=filter_params,
                    extra_input_args=_READRATE_ARGS,
                ):
                    await chunk_queue.put(chunk)
            finally:
                with contextlib.suppress(asyncio.QueueFull):
                    chunk_queue.put_nowait(None)

        producer_task: asyncio.Task[None] | None = None
        total_bytes = 0
        try:
            producer_task = asyncio.create_task(producer())

            # Phase 1: Pre-buffer — collect chunks until we have enough data
            pre_buffer: list[bytes] = []
            pre_buffer_size = 0
            while pre_buffer_size < PRE_BUFFER_BYTES:
                chunk = await chunk_queue.get()
                if chunk is None:
                    break
                pre_buffer.append(chunk)
                pre_buffer_size += len(chunk)

            # Re-check: stop may have been called while buffering
            if not player.current_media and not pre_buffer:
                return

            # NOW send HTTP headers + pre-buffer burst
            await response.prepare(request)
            for buf_chunk in pre_buffer:
                await response.write(buf_chunk)
                total_bytes += len(buf_chunk)

            # If pre-buffer ended with sentinel, we're done
            if chunk is None:
                return

            # Phase 2: Stream remaining chunks normally
            while True:
                chunk = await chunk_queue.get()
                if chunk is None:
                    break
                await response.write(chunk)
                total_bytes += len(chunk)
        except ConnectionResetError, BrokenPipeError, ConnectionAbortedError:
            logger.debug("Client disconnected from stream %s", player_id)
        except asyncio.CancelledError:
            logger.debug("Stream cancelled for player %s", player_id)
            raise
        finally:
            if producer_task and not producer_task.done():
                producer_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await producer_task
            content_length = headers.get("Content-Length")
            if content_length:
                logger.debug(
                    "Stream %s: wrote %d bytes, Content-Length=%s, diff=%d",
                    player_id,
                    total_bytes,
                    content_length,
                    total_bytes - int(content_length),
                )
            else:
                logger.debug("Stream %s finished: wrote %d bytes", player_id, total_bytes)

    async def _run_stream_task(
        self,
        player_id: str,
        stream_task: asyncio.Task[None],
        transport: Any,
    ) -> None:
        """Run a stream task with registration and error handling."""
        self._register_stream(player_id, stream_task, transport)
        try:
            await stream_task
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Stream error for player %s", player_id)
        finally:
            self._unregister_stream(player_id, stream_task, transport)

    # --- WebSocket, Broadcast & Health ---

    async def _handle_health(self, request: web.Request) -> web.Response:
        """Health check endpoint."""
        return web.json_response(
            {
                "status": "ok",
                "provider": "msx_bridge",
                "players": len(self.provider.players),
            }
        )

    async def _handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        """
        WebSocket for push playback — clients subscribe by player_id.

        Uses the same player_id derivation (device_id or IP) as content and
        stream endpoints so broadcast_stop reaches the correct client.
        Registers the player in MA on connect so the player appears when MSX starts.
        """
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)

        player_id, _, player = await self._ensure_player_for_request(request)
        if player_id not in self._ws_clients:
            self._ws_clients[player_id] = set()
        self._ws_clients[player_id].add(ws)
        logger.info(
            "WebSocket connected: player_id=%s, clients_for_player=%d, all_players=%s",
            player_id,
            len(self._ws_clients[player_id]),
            list(self._ws_clients.keys()),
        )
        if player and isinstance(player, MSXPlayer):
            player.on_ws_connected()

        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    self._handle_ws_message(player_id, msg.data)
        finally:
            self._ws_clients.get(player_id, set()).discard(ws)
            if not self._ws_clients.get(player_id):
                self._ws_clients.pop(player_id, None)
                # Notify the player that its last WS client disconnected
                offline_player = self.provider.mass.players.get_player(player_id)
                if offline_player and isinstance(offline_player, MSXPlayer):
                    offline_player.on_ws_disconnected()
            logger.debug("WebSocket client disconnected for player %s", player_id)

        return ws

    def _register_stream(self, player_id: str, task: asyncio.Task[None], transport: Any) -> None:
        """Register active stream task and transport for cancel on stop."""
        if player_id not in self._active_stream_tasks:
            self._active_stream_tasks[player_id] = set()
            self._active_stream_transports[player_id] = set()
        if task:
            self._active_stream_tasks[player_id].add(task)
        if transport:
            self._active_stream_transports[player_id].add(transport)

    def _unregister_stream(self, player_id: str, task: asyncio.Task[None], transport: Any) -> None:
        """Unregister stream when done (from finally block)."""
        if player_id not in self._active_stream_tasks:
            return
        if task:
            self._active_stream_tasks[player_id].discard(task)
        if transport:
            self._active_stream_transports[player_id].discard(transport)
        if not self._active_stream_tasks[player_id]:
            del self._active_stream_tasks[player_id]
            del self._active_stream_transports[player_id]

    async def _ws_send(
        self, ws: web.WebSocketResponse, text: str, player_id: str | None = None
    ) -> None:
        """Send text to WebSocket; on failure warn and remove the stale client."""
        try:
            await ws.send_str(text)
        except Exception as exc:
            logger.warning("WebSocket send failed (player=%s): %s", player_id, exc)
            if player_id:
                self._ws_clients.get(player_id, set()).discard(ws)

    async def _cmd_pause_no_echo(self, player_id: str) -> None:
        """Pause player without echoing back to MSX."""
        player = self.provider.mass.players.get_player(player_id)
        if not (player and isinstance(player, MSXPlayer)):
            return
        player._skip_ws_notify = True
        try:
            await self.provider.mass.players.cmd_pause(player_id)
        finally:
            player._skip_ws_notify = False

    async def _cmd_play_no_echo(self, player_id: str) -> None:
        """Resume player without echoing back to MSX."""
        player = self.provider.mass.players.get_player(player_id)
        if not (player and isinstance(player, MSXPlayer)):
            return
        player._skip_ws_notify = True
        try:
            await self.provider.mass.players.cmd_play(player_id)
        finally:
            player._skip_ws_notify = False

    def _handle_ws_message(self, player_id: str, data: str) -> None:
        """Process an inbound WebSocket message from MSX."""
        try:
            msg = json.loads(data)
        except json.JSONDecodeError, TypeError:
            logger.debug("Invalid WS message from %s: %s", player_id, data)
            return

        msg_type = msg.get("type")
        if msg_type == "position":
            position = msg.get("position")
            if position is not None and isinstance(position, (int, float)):
                player = self.provider.mass.players.get_player(player_id)
                if player and isinstance(player, MSXPlayer):
                    player.update_position(float(position))
                    self.provider.on_player_activity(player_id)
        elif msg_type == "pause":
            player = self.provider.mass.players.get_player(player_id)
            if player and isinstance(player, MSXPlayer):
                position = msg.get("position")
                if position is not None and isinstance(position, (int, float)):
                    player.update_position(float(position))
                self.provider.mass.create_task(self._cmd_pause_no_echo(player_id))
                self.provider.on_player_activity(player_id)
        elif msg_type == "resume":
            player = self.provider.mass.players.get_player(player_id)
            if player and isinstance(player, MSXPlayer):
                self.provider.mass.create_task(self._cmd_play_no_echo(player_id))
                self.provider.on_player_activity(player_id)
        else:
            logger.debug("Unknown WS message type from %s: %s", player_id, msg_type)

    # --- Stream Proxy ---

    async def _handle_stream(self, request: web.Request) -> web.StreamResponse:
        """Stream audio from MA to the TV using internal API."""
        player_id = _strip_known_extension(request.match_info["player_id"])

        player = self.provider.mass.players.get_player(player_id)
        if not player or not isinstance(player, MSXPlayer):
            return web.Response(status=404, text="Player not found")
        if rejected := self._reject_invalid_stream_token(request, player_id):
            return rejected
        self.provider.on_player_activity(player_id)

        media = player.current_media
        if not media:
            return web.Response(status=404, text="No active stream")

        return await self._serve_audio_stream(
            request,
            player,
            media,
            duration=self._resolve_served_duration(media),
        )

    # --- Library API Routes ---

    async def _handle_albums(self, request: web.Request) -> web.Response:
        """List albums."""
        limit = _int_param(request.query, "limit", 50)
        offset = _int_param(request.query, "offset", 0)
        albums = await self.provider.mass.music.albums.library_items(
            limit=limit, offset=offset, summary=False
        )
        return web.json_response(
            {
                "items": [
                    {
                        "item_id": str(album.item_id),
                        "name": album.name,
                        "artist": getattr(album, "artist_str", ""),
                        "image": get_image_url(album, self.provider),
                        "uri": album.uri,
                    }
                    for album in albums
                ],
                "total": albums.total if hasattr(albums, "total") else len(albums),
            }
        )

    async def _handle_album_tracks(self, request: web.Request) -> web.Response:
        """List tracks for an album."""
        item_id = request.match_info["item_id"]
        tracks = await self.provider.mass.music.albums.tracks(item_id, "library")
        return web.json_response(
            {
                "items": [self._format_track(track) for track in tracks],
            }
        )

    async def _handle_artists(self, request: web.Request) -> web.Response:
        """List artists."""
        limit = _int_param(request.query, "limit", 50)
        offset = _int_param(request.query, "offset", 0)
        artists = await self.provider.mass.music.artists.library_items(
            limit=limit, offset=offset, summary=False
        )
        return web.json_response(
            {
                "items": [
                    {
                        "item_id": str(artist.item_id),
                        "name": artist.name,
                        "image": get_image_url(artist, self.provider),
                        "uri": artist.uri,
                    }
                    for artist in artists
                ],
                "total": artists.total if hasattr(artists, "total") else len(artists),
            }
        )

    async def _handle_artist_albums(self, request: web.Request) -> web.Response:
        """List albums for an artist."""
        item_id = request.match_info["item_id"]
        albums = await self.provider.mass.music.artists.albums(item_id, "library")
        return web.json_response(
            {
                "items": [
                    {
                        "item_id": str(album.item_id),
                        "name": album.name,
                        "artist": getattr(album, "artist_str", ""),
                        "image": get_image_url(album, self.provider),
                        "uri": album.uri,
                    }
                    for album in albums
                ],
            }
        )

    async def _handle_playlists(self, request: web.Request) -> web.Response:
        """List playlists."""
        limit = _int_param(request.query, "limit", 50)
        offset = _int_param(request.query, "offset", 0)
        playlists = await self.provider.mass.music.playlists.library_items(
            limit=limit, offset=offset, summary=False
        )
        return web.json_response(
            {
                "items": [
                    {
                        "item_id": str(playlist.item_id),
                        "name": playlist.name,
                        "image": get_image_url(playlist, self.provider),
                        "uri": playlist.uri,
                    }
                    for playlist in playlists
                ],
                "total": playlists.total if hasattr(playlists, "total") else len(playlists),
            }
        )

    async def _handle_playlist_tracks(self, request: web.Request) -> web.Response:
        """List tracks for a playlist."""
        item_id = request.match_info["item_id"]
        tracks = [t async for t in self.provider.mass.music.playlists.tracks(item_id, "library")]
        return web.json_response(
            {
                "items": [self._format_track(track) for track in tracks],
            }
        )

    async def _handle_tracks(self, request: web.Request) -> web.Response:
        """List tracks."""
        limit = _int_param(request.query, "limit", 50)
        offset = _int_param(request.query, "offset", 0)
        tracks = await self.provider.mass.music.tracks.library_items(
            limit=limit, offset=offset, summary=False
        )
        return web.json_response(
            {
                "items": [self._format_track(track) for track in tracks],
                "total": tracks.total if hasattr(tracks, "total") else len(tracks),
            }
        )

    async def _handle_search(self, request: web.Request) -> web.Response:
        """Search the music library."""
        query = request.query.get("q", "")
        if not query:
            return web.json_response({"error": "Missing query parameter 'q'"}, status=400)
        limit = _int_param(request.query, "limit", 20)
        results = await self.provider.mass.music.search(query, limit=limit)
        return web.json_response(
            {
                "artists": [
                    {
                        "item_id": str(a.item_id),
                        "name": a.name,
                        "image": get_image_url(a, self.provider),
                        "uri": a.uri,
                    }
                    for a in results.artists
                ],
                "albums": [
                    {
                        "item_id": str(a.item_id),
                        "name": a.name,
                        "artist": getattr(a, "artist_str", ""),
                        "image": get_image_url(a, self.provider),
                        "uri": a.uri,
                    }
                    for a in results.albums
                ],
                "tracks": [self._format_track(t) for t in results.tracks],
                "playlists": [
                    {
                        "item_id": str(p.item_id),
                        "name": p.name,
                        "image": get_image_url(p, self.provider),
                        "uri": p.uri,
                    }
                    for p in results.playlists
                ],
            }
        )

    async def _handle_recently_played(self, request: web.Request) -> web.Response:
        """Return recently played items."""
        limit = _int_param(request.query, "limit", 20)
        tracks = await self.provider.mass.music.tracks.library_items(
            limit=limit, order_by="last_played", summary=False
        )
        return web.json_response(
            {
                "items": [self._format_track(track) for track in tracks],
            }
        )

    async def _handle_lyrics(self, request: web.Request) -> web.Response:
        """Return lyrics for the currently playing track on a given player."""
        player_id = request.match_info["player_id"]
        empty = web.json_response({"lyrics": None, "lrc_lyrics": None})

        player = self.provider.mass.players.get_player(player_id)
        if not player or not isinstance(player, MSXPlayer):
            return empty

        media = player.current_media
        if not media or not media.source_id or not media.queue_item_id:
            return empty

        queue_item = self.provider.mass.player_queues.get_item(media.source_id, media.queue_item_id)
        if not queue_item or not queue_item.media_item:
            return empty

        track = queue_item.media_item
        if not isinstance(track, Track):
            return empty
        try:
            lyrics, lrc_lyrics = await self.provider.mass.metadata.get_track_lyrics(track)
        except Exception:
            lyrics, lrc_lyrics = None, None

        return web.json_response(
            {
                "title": getattr(track, "name", ""),
                "artist": getattr(track, "artist_str", ""),
                "lyrics": lyrics,
                "lrc_lyrics": lrc_lyrics,
            }
        )

    async def _handle_queue(self, request: web.Request) -> web.Response:
        """Return the current playback queue for a given player."""
        player_id = request.match_info["player_id"]

        player = self.provider.mass.players.get_player(player_id)
        if not player or not isinstance(player, MSXPlayer):
            return web.json_response({"items": [], "current_index": -1})

        queue_id = player_id
        try:
            queue_items = self.provider.mass.player_queues.items(queue_id)
        except Exception:
            logger.debug("Failed to fetch queue items for player %s", player_id, exc_info=True)
            queue_items = []

        current_uri = None
        media = player.current_media
        if media and media.source_id and media.queue_item_id:
            qi = self.provider.mass.player_queues.get_item(media.source_id, media.queue_item_id)
            if qi and qi.media_item:
                current_uri = getattr(qi.media_item, "uri", None)

        items: list[dict[str, Any]] = []
        current_index = -1
        for i, qi in enumerate(queue_items):
            mi = getattr(qi, "media_item", None)
            uri = getattr(mi, "uri", None) or ""
            img = None
            if hasattr(qi, "image") and qi.image:
                img = self.provider.mass.metadata.get_image_url(qi.image)
            items.append(
                {
                    "title": getattr(mi, "name", None) or getattr(qi, "name", "") or "",
                    "artist": getattr(mi, "artist_str", "") if mi else "",
                    "duration": getattr(mi, "duration", None) or getattr(qi, "duration", 0) or 0,
                    "image": img,
                    "uri": uri,
                }
            )
            if current_uri and uri == current_uri and current_index < 0:
                current_index = i

        return web.json_response({"items": items, "current_index": current_index})

    # --- Party Mode ---

    def _cached_party(self) -> PartyInfo | None:
        """Return the last cached party state without refreshing (sync contexts)."""
        return self._party_cache[1] if self._party_cache else None

    async def _qr_cover_base(self, prefix: str) -> str | None:
        """Return the QR-cover endpoint base when a party is active, else None."""
        if await self._get_active_party() is None:
            return None
        return f"{prefix}/api/party/qr-cover.png"

    async def _get_active_party(self) -> PartyInfo | None:
        """
        Return details of the active party, or None when no party is active.

        Never raises: a broken or slow Party plugin degrades to "no party" so
        the core UI (menu, kiosk) keeps working. Results are cached briefly.
        """
        now = time.monotonic()
        if self._party_cache is not None and now - self._party_cache[0] < PARTY_CACHE_TTL:
            return self._party_cache[1]
        info: PartyInfo | None = None
        try:
            party = cast("Any", self.provider.mass.get_provider("party"))
            if party is not None:
                join_url = await asyncio.wait_for(party.get_party_url(), PARTY_CALL_TIMEOUT)
                if join_url:
                    config = await asyncio.wait_for(party.get_party_config(), PARTY_CALL_TIMEOUT)
                    info = PartyInfo(
                        join_url=join_url,
                        name=getattr(config, "party_name", None),
                        qr_text=getattr(config, "qr_text", None),
                        qr_version=hashlib.sha256(join_url.encode()).hexdigest()[:12],
                    )
        except Exception:
            logger.warning("Party plugin status check failed", exc_info=True)
        self._party_cache = (now, info)
        return info

    async def _handle_party_status(self, _request: web.Request) -> web.Response:
        """Return party status for the kiosk overlay."""
        party = await self._get_active_party()
        if party is None:
            return web.json_response({"active": False})
        # the join URL itself is deliberately not exposed — clients only get the QR
        # image URL (relative, so it works behind reverse proxies) plus an opaque
        # version so they refetch the image only when the join code rotates
        return web.json_response(
            {
                "active": True,
                "name": party.name,
                "qr_text": party.qr_text,
                "qr_url": "/api/party/qr.svg",
                "qr_version": party.qr_version,
            }
        )

    async def _handle_party_qr(self, request: web.Request) -> web.Response:
        """Serve the guest join URL as a QR code image (SVG or PNG by route)."""
        party = await self._get_active_party()
        if party is None:
            return web.Response(status=404, text="No active party")
        kind = "png" if request.path.endswith(".png") else "svg"
        body = await asyncio.to_thread(_render_qr, party.join_url, kind)
        return web.Response(
            body=body,
            content_type="image/png" if kind == "png" else "image/svg+xml",
            headers={"Cache-Control": "no-store"},
        )

    async def _handle_party_qr_cover(self, request: web.Request) -> web.Response:
        """
        Serve a cover image with the party QR stamped into its corner (PNG).

        MSX cannot render overlays, so during a party the playback background
        is routed through this endpoint. Degrades to a redirect to the
        original image when the party ended, the source is not ours, or the
        fetch/composite fails — stale playlist JSON on TVs keeps working.
        """
        image_url = request.query.get("image", "")
        if not image_url:
            return web.Response(status=400, text="Missing image parameter")
        # Reject non-MA sources outright — redirecting would be an open
        # redirect and fetching would be an SSRF proxy.
        if not self._is_allowed_cover_source(request, image_url):
            return web.Response(status=400, text="Image source not permitted")
        party = await self._get_active_party()
        if party is None:
            raise web.HTTPFound(location=image_url)
        cache_key = (image_url, party.qr_version)
        if (cached := self._qr_cover_cache.get(cache_key)) is None:
            try:
                # join: a TV dropping its request must not cancel the shared
                # render — late joiners and the cache still get the result
                cached = await join_task(self._qr_cover_task(cache_key, image_url, party.join_url))
            except Exception as err:
                logger.debug("QR cover composite failed for %s: %s", image_url, err)
                raise web.HTTPFound(location=image_url) from None
        return web.Response(
            body=cached,
            content_type="image/png",
            headers={"Cache-Control": "no-store"},
        )

    def _qr_cover_task(
        self, cache_key: tuple[str, str], image_url: str, join_url: str
    ) -> asyncio.Task[bytes]:
        """Return the in-flight render task for this cover, starting one if needed."""
        if (task := self._qr_cover_inflight.get(cache_key)) is None:
            task = asyncio.create_task(self._fetch_and_render_cover(cache_key, image_url, join_url))
            self._qr_cover_inflight[cache_key] = task

            def _cleanup(finished: asyncio.Task[bytes]) -> None:
                self._qr_cover_inflight.pop(cache_key, None)
                # consume the exception so a task whose waiters were all
                # cancelled never logs "exception was never retrieved"
                if not finished.cancelled():
                    finished.exception()

            task.add_done_callback(_cleanup)
        return task

    async def _fetch_and_render_cover(
        self, cache_key: tuple[str, str], image_url: str, join_url: str
    ) -> bytes:
        """Fetch the cover, composite the QR onto it, and cache the PNG."""
        async with self.provider.mass.http_session.get(
            image_url,
            timeout=aiohttp.ClientTimeout(total=10),
            allow_redirects=False,
        ) as resp:
            if resp.status != 200:
                raise ValueError(f"cover fetch returned HTTP {resp.status}")
            cover_bytes = await resp.read()
        # PIL decode/re-encode blocks; on this loop it would stall audio
        # streaming for every player, so hop to a worker thread
        rendered = await asyncio.to_thread(_render_qr_cover, join_url, cover_bytes)
        # QR rotation changes the cache key; keep the cache tiny and bounded
        if len(self._qr_cover_cache) >= 32:
            self._qr_cover_cache.clear()
        self._qr_cover_cache[cache_key] = rendered
        return rendered

    @staticmethod
    def _rewrite_stream_host(request: web.Request, url: str) -> str:
        """
        Point a stream URL at the host the client already uses to reach us.

        The MA streamserver advertises its own IP, which is unreachable for
        the TV when MA runs behind Docker/NAT. The host the TV used for this
        request is known-good, so only the URL's host is replaced — scheme,
        port, path and query are preserved.
        """
        client_host = request.url.host
        if not client_host:
            return url
        parts = urlsplit(url)
        if ":" in client_host:  # IPv6 literals need brackets in a netloc
            client_host = f"[{client_host}]"
        netloc = f"{client_host}:{parts.port}" if parts.port else client_host
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))

    @staticmethod
    def _url_origin(url: str) -> tuple[str, str | None, int | None]:
        """Return (scheme, hostname, port); raises ValueError on malformed URLs."""
        parts = urlsplit(url)
        # .port is lazy and raises on garbage like "host:8095.evil.example"
        return (parts.scheme, parts.hostname, parts.port)

    def _is_allowed_cover_source(self, request: web.Request, image_url: str) -> bool:
        """Only composite covers served by this provider or MA itself (no open proxy)."""
        try:
            target_origin = self._url_origin(image_url)
        except ValueError:
            return False
        if target_origin[0] not in ("http", "https") or not target_origin[1]:
            return False
        allowed_bases = [self._get_prefix(request)]
        for source in (
            getattr(self.provider.mass, "webserver", None),
            getattr(self.provider.mass, "streams", None),
        ):
            base_url = getattr(source, "base_url", None)
            if isinstance(base_url, str) and base_url.startswith("http"):
                allowed_bases.append(base_url)
        # Compare parsed origins, not string prefixes: "http://ma:8095.evil.com"
        # must not pass for the allowed base "http://ma:8095".
        for base in allowed_bases:
            try:
                if target_origin == self._url_origin(base):
                    return True
            except ValueError:
                continue
        return False

    # --- Playback Control ---

    def _reject_invalid_stream_token(
        self, request: web.Request, player_id: str
    ) -> web.Response | None:
        """
        Reject an audio request that does not carry the player's own stream token.

        A TV cannot send an auth header, so the token travels in the URL the bridge
        itself generated. This stops a request that was never handed out — a web page
        firing an <audio> tag at this LAN server. A URL that was handed out stays valid
        until the provider reloads, so this is not a defence against a captured URL.
        """
        expected = self.provider.get_stream_token(player_id)
        if not secrets.compare_digest(request.query.get("token", ""), expected):
            return web.Response(status=403, text="Invalid or missing stream token")
        return None

    @staticmethod
    def _reject_cross_site(request: web.Request) -> web.Response | None:
        """
        Reject browser cross-site requests to state-changing endpoints (CSRF guard).

        Any web page can fire an unauthenticated GET at this LAN server via an
        img/script tag; modern browsers mark such requests with
        Sec-Fetch-Site: cross-site. Legitimate callers are same-origin (web
        player, MSX interaction plugin, dashboard) or non-browser clients that
        omit the header entirely — both pass.
        """
        if request.headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
            return web.json_response({"error": "Cross-site request rejected"}, status=403)
        return None

    async def _handle_play(self, request: web.Request) -> web.Response:
        """Start playback of a track."""
        if rejected := self._reject_cross_site(request):
            return rejected
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON body"}, status=400)

        track_uri = body.get("track_uri")
        player_id = body.get("player_id")
        # the body is untyped JSON, so the type matters as much as the presence
        if not isinstance(track_uri, str) or not isinstance(player_id, str):
            return web.json_response({"error": "Invalid track_uri or player_id"}, status=400)
        if not track_uri or not player_id:
            return web.json_response({"error": "Missing track_uri or player_id"}, status=400)
        if not await _is_media_item_uri(track_uri):
            return web.json_response({"error": "Invalid track_uri"}, status=400)

        if self._get_msx_player(player_id) is None:
            return web.json_response({"error": "Unknown MSX player"}, status=404)

        async with ImpersonatedUser(self.provider.mass, await self.provider.get_owner_username()):
            await self.provider.mass.player_queues.play_media(player_id, track_uri)
        return web.json_response({"status": "ok"})

    async def _handle_pause(self, request: web.Request) -> web.Response:
        """Pause playback."""
        if rejected := self._reject_cross_site(request):
            return rejected
        player_id = _strip_known_extension(request.match_info["player_id"])
        if self._get_msx_player(player_id) is None:
            return web.json_response({"error": "Unknown MSX player"}, status=404)
        self.provider.on_player_activity(player_id)
        await self.provider.mass.players.cmd_pause(player_id)
        return web.json_response({"status": "ok"})

    async def _handle_stop(self, request: web.Request) -> web.Response:
        """Stop playback."""
        if rejected := self._reject_cross_site(request):
            return rejected
        player_id = _strip_known_extension(request.match_info["player_id"])
        if self._get_msx_player(player_id) is None:
            return web.json_response({"error": "Unknown MSX player"}, status=404)
        self.provider.on_player_activity(player_id)
        await self.provider.mass.players.cmd_stop(player_id)
        return web.json_response({"status": "ok"})

    async def _handle_quick_stop(self, request: web.Request) -> web.Response:
        """Stop playback on MSX immediately (same signal as Disable)."""
        if rejected := self._reject_cross_site(request):
            return rejected
        player_id = _strip_known_extension(request.match_info["player_id"])
        if self._get_msx_player(player_id) is None:
            return web.json_response({"error": "Unknown MSX player"}, status=404)
        self.provider.on_player_activity(player_id)
        await self.provider.mass.players.cmd_stop(player_id)
        self.provider.notify_play_stopped(player_id)
        accept = request.headers.get("Accept", "")
        if "text/html" in accept:
            return web.Response(status=303, headers={"Location": "/"})
        return web.json_response({"status": "ok"})

    async def _handle_next(self, request: web.Request) -> web.Response:
        """Skip to next track."""
        if rejected := self._reject_cross_site(request):
            return rejected
        player_id = _strip_known_extension(request.match_info["player_id"])
        if self._get_msx_player(player_id) is None:
            return web.json_response({"error": "Unknown MSX player"}, status=404)
        self.provider.on_player_activity(player_id)
        await self.provider.mass.players.cmd_next_track(player_id)
        return web.json_response({"status": "ok"})

    async def _handle_previous(self, request: web.Request) -> web.Response:
        """Skip to previous track."""
        if rejected := self._reject_cross_site(request):
            return rejected
        player_id = _strip_known_extension(request.match_info["player_id"])
        if self._get_msx_player(player_id) is None:
            return web.json_response({"error": "Unknown MSX player"}, status=404)
        self.provider.on_player_activity(player_id)
        await self.provider.mass.players.cmd_previous_track(player_id)
        return web.json_response({"status": "ok"})

    # --- Helpers ---

    def _get_msx_player(self, player_id: str) -> MSXPlayer | None:
        """Return the MSXPlayer for player_id if it belongs to this provider, else None."""
        player = self.provider.mass.players.get_player(player_id, raise_unavailable=False)
        if isinstance(player, MSXPlayer) and player.provider == self.provider:
            return player
        return None

    def _get_prefix(self, request: web.Request) -> str:
        """
        Build URL prefix for JSON content, using our known port.

        Uses aiohttp's parsed URL host (IPv6-safe, no port) and substitutes
        self.port. Note: host is still derived from the Host header; a crafted
        header can influence the returned host, but the server binds to 0.0.0.0
        so there is no single canonical IP to validate against.
        """
        host: str = request.url.host or request.host.split(":")[0]  # IPv6-safe, no port
        host_addr = f"[{host}]" if ":" in host else host  # bracket IPv6 literals for URLs
        return f"http://{host_addr}:{self.port}"

    def _get_player_id_and_device_param(self, request: web.Request) -> tuple[str, str]:
        """
        Extract player_id and device_id query param from request.

        Returns (player_id, device_param) where device_param is e.g. "device_id=xxx"
        or "" if using IP fallback.
        """
        device_id = request.query.get("device_id")
        remote_ip = request.remote or "unknown"

        if device_id:
            device_id = device_id[:64]  # clamp before sanitizing (UUIDs are 36 chars)
            sanitized = PLAYER_ID_SANITIZE_RE.sub("_", device_id).strip("_") or "device"
            player_id = f"{MSX_PLAYER_ID_PREFIX}{sanitized}"
            param = f"device_id={quote(device_id, safe='')}"
            logger.info(
                "[PlayerID] device_id=%s, remote_ip=%s -> player_id=%s",
                device_id,
                remote_ip,
                player_id,
            )
        else:
            ip = remote_ip if remote_ip != "unknown" else "0_0_0_0"
            sanitized = PLAYER_ID_SANITIZE_RE.sub("_", ip.replace(".", "_")).strip("_") or "ip"
            player_id = f"{MSX_PLAYER_ID_PREFIX}{sanitized}"
            param = ""
            logger.info(
                "[PlayerID] no device_id, remote_ip=%s -> player_id=%s",
                remote_ip,
                player_id,
            )
        return player_id, param

    async def _ensure_player_for_request(
        self, request: web.Request
    ) -> tuple[str, str, MSXPlayer | None]:
        """
        Get or register player for this request.

        Returns (player_id, device_param, player).
        Player may be None if registration failed.
        """
        player_id, device_param = self._get_player_id_and_device_param(request)
        # Remember how this client reaches us — WS pushes have no request context
        self._client_prefixes[player_id] = self._get_prefix(request)
        remote_ip = request.remote
        # Web player clients pass source=web to distinguish from MSX TV players
        prefix_label = "WEB TV" if request.query.get("source") == "web" else "MSX TV"
        display_name = self.provider._player_display_name_from_id(
            player_id, prefix_label=prefix_label, remote_ip=remote_ip
        )
        player = await self.provider.get_or_register_player(
            player_id, display_name=display_name, ip_address=remote_ip
        )
        return player_id, device_param, player

    def _current_media_matches_uri(self, player: MSXPlayer, track_uri: str) -> bool:
        """Check if player's current_media corresponds to the requested track URI."""
        media = player.current_media
        if not media or not media.source_id or not media.queue_item_id:
            return False
        queue_item = self.provider.mass.player_queues.get_item(media.source_id, media.queue_item_id)
        if queue_item and queue_item.media_item:
            return getattr(queue_item.media_item, "uri", None) == track_uri
        return False

    def _format_track(self, track: Any) -> dict[str, Any]:
        """Format a track object for the API response."""
        return {
            "item_id": str(track.item_id),
            "name": track.name,
            "artist": getattr(track, "artist_str", ""),
            "album": getattr(getattr(track, "album", None), "name", ""),
            "duration": getattr(track, "duration", 0),
            "image": self.provider.mass.metadata.get_image_url(track.image)
            if hasattr(track, "image") and track.image
            else None,
            "uri": track.uri,
        }
