"""Embedded HTTP server for the MSX Bridge Provider."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import secrets
from html import escape as html_escape
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeGuard, cast
from urllib.parse import quote

import aiohttp
from aiohttp import WSMsgType, web
from music_assistant_models.enums import RepeatMode
from music_assistant_models.errors import (
    InvalidDataError,
    MusicAssistantError,
    ResourceTemporarilyUnavailable,
)
from music_assistant_models.media_items import Album, Track

from music_assistant.controllers.webserver.helpers.auth_middleware import ImpersonatedUser

from .audio_stream import AudioPipeline, resolve_served_duration
from .constants import (
    CONF_SHOW_STOP_NOTIFICATION,
    DEFAULT_SHOW_STOP_NOTIFICATION,
    MSX_PLAYER_ID_PREFIX,
    PLAYER_ID_SANITIZE_RE,
)
from .mappers import (
    append_device_param,
    container_uri,
    dump_msx,
    get_image_url,
    map_album_to_msx,
    map_artist_to_msx,
    map_playlist_to_msx,
    map_track_to_msx,
    map_tracks_to_msx_playlist,
    msx_list_page,
    playlist_tracks_from_media_items,
    sort_album_tracks,
)
from .models import MsxContent, MsxItem, MsxTemplate
from .party import PartyAdapter, PartyInfo
from .player import MSXPlayer
from .queue_handshake import (
    find_uri_in_active_queue,
    is_media_item_uri,
    prepare_msx_audio,
    queue_items_to_tracks,
)

if TYPE_CHECKING:
    from multidict import MultiMapping
    from music_assistant_models.media_items import ItemMapping, PlayableMediaItemType
    from music_assistant_models.player import PlayerMedia

    from .provider import MSXBridgeProvider

__all__ = [
    "STATIC_DIR",
    "MSXHTTPServer",
    "PartyInfo",
]

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

_KNOWN_EXTENSIONS = (".mp3", ".json", ".flac", ".aac")


def _int_param(query: MultiMapping[str], name: str, default: int, max_val: int = 10000) -> int:
    """Parse an integer query parameter safely, clamping to [0, max_val]."""
    try:
        return max(0, min(int(query.get(name, str(default))), max_val))
    except ValueError, TypeError:
        return default


def _non_negative_int_param(query: MultiMapping[str], name: str, default: int) -> int:
    """Parse a non-negative integer query parameter without an arbitrary ceiling."""
    try:
        return max(0, int(query.get(name, str(default))))
    except ValueError, TypeError:
        return default


def _is_audio_path(path: str) -> bool:
    """Check whether the path is one of the audio routes."""
    return path.startswith(("/stream/", "/msx/audio/"))


def _msx_execute_ok(action: str = "[]") -> web.Response:
    """Wrap a follow-up action for MSX ``execute:{URL}``."""
    return web.json_response(
        {"response": {"status": 200, "text": "OK", "message": None, "data": {"action": action}}}
    )


def _msx_execute_error(status: int, message: str) -> web.Response:
    """Return an MSX-visible execute error while keeping HTTP 200."""
    return web.json_response(
        {"response": {"status": status, "text": "Error", "message": message, "data": None}}
    )


def _strip_known_extension(value: str) -> str:
    """Strip only known audio/data extensions from a value."""
    for ext in _KNOWN_EXTENSIONS:
        if value.endswith(ext):
            return value[: -len(ext)]
    return value


def _is_finite_position(value: object) -> TypeGuard[int | float]:
    """Return whether a WebSocket position is a finite non-negative number."""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
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
        self.audio = AudioPipeline(provider)
        self.party = PartyAdapter(provider)
        self._active_stream_tasks = self.audio.active_stream_tasks
        self._active_stream_transports = self.audio.active_stream_transports
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
        self._client_prefixes.clear()
        for player_id in list(self._active_stream_tasks):
            self.cancel_streams_for_player(player_id)
        await self.party.stop()
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
            if client_prefix := self._client_prefixes.get(player_id):
                image_url = self.party.rewrite_play_image(image_url, client_prefix)
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
        self.audio.cancel_streams_for_player(player_id)

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
        self.app.router.add_get("/api/party", self._handle_party_status)
        self.app.router.add_get("/api/party/qr.svg", self._handle_party_qr)
        self.app.router.add_get("/api/party/qr.png", self._handle_party_qr)
        self.app.router.add_get("/api/party/qr-cover.png", self._handle_party_qr_cover)

        # Playback control — GET (MSX interaction plugin) + POST (dashboard).
        # Never wildcard: extra methods only widen the CSRF surface.
        self.app.router.add_post("/api/play", self._handle_play)
        for path, handler in (
            ("/api/pause/{player_id}", self._handle_pause),
            ("/api/stop/{player_id}", self._handle_stop),
            ("/api/quick-stop/{player_id}", self._handle_quick_stop),
            ("/api/play-context/{player_id}", self._handle_play_context),
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
        The MSX plugin (/msx/plugin.html) is served from the same origin, so
        browser playback-control POSTs from the status dashboard are same-origin.
        MSX TV app only makes GET requests. This matches MA's own webserver pattern.

        The audio routes are the exception and get no header at all: a media element
        plays a cross-origin source without CORS, so withholding it costs nothing
        and keeps a cross-origin fetch() from reading the audio.
        """
        if request.method == "OPTIONS":
            return web.Response(
                headers={
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                    "Access-Control-Allow-Headers": "*",
                }
            )
        if request.path.startswith(("/msx/playlist/", "/msx/queue-playlist/")) and (
            rejected := self._reject_cross_site(request)
        ):
            response: web.StreamResponse = rejected
        else:
            response = await handler(request)
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

        safe_host: str = html_escape(request.host)

        html = f"""<!DOCTYPE html>
<html>
<head><title>MSX Bridge</title>
<style>
body {{ font-family: system-ui, sans-serif; max-width: 800px; margin: 50px auto; padding: 20px; }}
.info {{ background: #e3f2fd; padding: 15px; border-radius: 5px; margin: 10px 0; }}
code {{ background: #f5f5f5; padding: 2px 6px; border-radius: 3px; word-break: break-all; }}
.player-row {{ display: flex; align-items: center; gap: 12px; margin: 8px 0; list-style: none; }}
.player-row form {{ margin: 0; }}
.btn {{ padding: 6px 12px; border-radius: 4px; border: 1px solid #1976d2;
  background: #1976d2; color: white; cursor: pointer; font-size: 14px; }}
.btn:hover {{ background: #1565c0; }}
</style>
</head>
<body>
<h1>MSX Music Assistant Bridge</h1>

<div class="info">
<h3>MSX Setup URL</h3>
<code>http://{safe_host}/msx/start.json</code>
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
        """Return MSX launcher page with the MSX Player."""
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
                    action=f"menu:request:interaction:init@{prefix}/msx/plugin.html?v=21",
                ),
            ],
        )
        return web.json_response(dump_msx(content))

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
        return web.json_response(dump_msx(content))

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
        except MusicAssistantError, TimeoutError:
            logger.exception("Failed to fetch albums")
            albums = []

        items = await asyncio.gather(
            *(map_album_to_msx(a, prefix, self.provider, device_param) for a in albums)
        )
        return web.json_response(
            dump_msx(
                msx_list_page(
                    "Albums",
                    items,
                    empty_title="No albums found",
                    layout="0,0,3,4",
                )
            )
        )

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
        except MusicAssistantError, TimeoutError:
            logger.exception("Failed to fetch artists")
            artists = []

        items = [map_artist_to_msx(a, prefix, self.provider, device_param) for a in artists]
        return web.json_response(
            dump_msx(
                msx_list_page(
                    "Artists",
                    items,
                    empty_title="No artists found",
                    layout="0,0,2,3",
                )
            )
        )

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
        except MusicAssistantError, TimeoutError:
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
        return web.json_response(dump_msx(content))

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
        except MusicAssistantError, TimeoutError:
            logger.exception("Failed to fetch tracks")
            tracks = []

        items = [
            map_track_to_msx(
                t,
                prefix,
                player_id,
                self.provider,
                device_param,
                context_uri=t.uri,
            )
            for t in tracks
            if t.uri
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
        return web.json_response(dump_msx(content))

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
        except MusicAssistantError, TimeoutError:
            logger.exception("Failed to fetch recently played tracks")
            tracks = []
        items = [
            map_track_to_msx(
                t,
                prefix,
                player_id,
                self.provider,
                device_param,
                context_uri=t.uri,
            )
            for t in tracks
            if t.uri
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
        return web.json_response(dump_msx(content))

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
        return web.json_response(dump_msx(content))

    async def _handle_msx_search_input(self, request: web.Request) -> web.Response:
        """Return search results for the MSX Input Plugin (search keyboard)."""
        player_id, device_param, _ = await self._ensure_player_for_request(request)
        prefix = self._get_prefix(request)
        query = request.query.get("q", "")
        if not query:
            content = MsxContent(
                headline="{ico:search} Search",
                hint="Type to search...",
                compress=True,
                template=MsxTemplate(
                    type="separate",
                    layout="0,0,2,4",
                    image_filler="default",
                ),
                items=[MsxItem(title="Start typing to search")],
            )
            return web.json_response(dump_msx(content))

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
            compress=True,
            template=MsxTemplate(
                type="separate",
                layout="0,0,2,4",
                image_filler="default",
            ),
            items=items if items else [MsxItem(title="No results found")],
        )
        return web.json_response(dump_msx(content))

    async def _handle_msx_search(self, request: web.Request) -> web.Response:
        """Return search results as an MSX content page."""
        player_id, device_param, _ = await self._ensure_player_for_request(request)
        prefix = self._get_prefix(request)
        query = request.query.get("q", "")
        if not query:
            return web.json_response(
                dump_msx(
                    MsxContent(
                        headline="Search",
                        items=[MsxItem(title="Please enter a search query")],
                    )
                )
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
        return web.json_response(dump_msx(content))

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
        return web.json_response(dump_msx(content))

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
            item.label = f"Album — {album.artist_str if isinstance(album, Album) else ''}"
            item.icon = "msx-white-soft:album"
            items.append(item)
        for track in results.tracks:
            if track.uri is None:
                continue
            item = map_track_to_msx(
                track,
                prefix,
                player_id,
                self.provider,
                device_param,
                context_uri=track.uri,
            )
            item.label = f"Track — {track.artist_str if isinstance(track, Track) else ''}"
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
            tracks = sort_album_tracks(
                await self.provider.mass.music.albums.tracks(item_id, provider)
            )
        except MusicAssistantError, TimeoutError:
            logger.exception("Failed to fetch tracks for album %s", item_id)
            tracks = []
        album_uri = container_uri("album", item_id, provider)
        items = [
            map_track_to_msx(
                t,
                prefix,
                player_id,
                self.provider,
                device_param,
                context_uri=album_uri,
                context_start=idx,
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
        return web.json_response(dump_msx(content))

    async def _handle_msx_artist_albums(self, request: web.Request) -> web.Response:
        """Return albums for an artist as an MSX content page."""
        _, device_param, _ = await self._ensure_player_for_request(request)
        prefix = self._get_prefix(request)
        item_id = request.match_info["item_id"]
        provider = request.query.get("provider", "library")
        try:
            albums = await self.provider.mass.music.artists.albums(item_id, provider)
        except MusicAssistantError, TimeoutError:
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
        return web.json_response(dump_msx(content))

    async def _handle_msx_playlist_tracks(self, request: web.Request) -> web.Response:
        """Return tracks for a playlist as an MSX content page."""
        player_id, device_param, _ = await self._ensure_player_for_request(request)
        prefix = self._get_prefix(request)
        item_id = request.match_info["item_id"]
        try:
            tracks = [
                t async for t in self.provider.mass.music.playlists.tracks(item_id, "library")
            ]
        except MusicAssistantError, TimeoutError:
            logger.exception("Failed to fetch tracks for playlist %s", item_id)
            tracks = []
        playlist_uri = container_uri("playlist", item_id)
        items = [
            map_track_to_msx(
                t,
                prefix,
                player_id,
                self.provider,
                device_param,
                context_uri=playlist_uri,
                context_start=idx,
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
        return web.json_response(dump_msx(content))

    # --- MSX Playlist Endpoints ---

    async def _handle_msx_album_playlist(self, request: web.Request) -> web.Response:
        """Return album tracks as an MSX playlist JSON."""
        player_id, device_param, _ = await self._ensure_player_for_request(request)
        prefix = self._get_prefix(request)
        item_id = request.match_info["item_id"]
        provider_name = request.query.get("provider", "library")
        start = _int_param(request.query, "start", 0)
        try:
            tracks = sort_album_tracks(
                await self.provider.mass.music.albums.tracks(item_id, provider_name)
            )
        except MusicAssistantError, TimeoutError:
            logger.exception("Failed to fetch tracks for album playlist %s", item_id)
            tracks = []
        playlist = map_tracks_to_msx_playlist(
            playlist_tracks_from_media_items(tracks),
            start,
            prefix,
            player_id,
            self.provider,
            device_param,
            qr_cover_base=await self._qr_cover_base(prefix),
        )
        return web.json_response(dump_msx(playlist))

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
        except MusicAssistantError, TimeoutError:
            logger.exception("Failed to fetch tracks for playlist playlist %s", item_id)
            tracks = []
        playlist = map_tracks_to_msx_playlist(
            playlist_tracks_from_media_items(tracks),
            start,
            prefix,
            player_id,
            self.provider,
            device_param,
            qr_cover_base=await self._qr_cover_base(prefix),
        )
        return web.json_response(dump_msx(playlist))

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
            playlist_tracks_from_media_items(tracks),
            start,
            prefix,
            player_id,
            self.provider,
            device_param,
            qr_cover_base=await self._qr_cover_base(prefix),
        )
        return web.json_response(dump_msx(playlist))

    async def _handle_msx_recently_played_playlist(self, request: web.Request) -> web.Response:
        """Return recently played tracks as an MSX playlist JSON."""
        player_id, device_param, _ = await self._ensure_player_for_request(request)
        prefix = self._get_prefix(request)
        start = _int_param(request.query, "start", 0)
        tracks = await self.provider.mass.music.tracks.library_items(
            limit=50, order_by="last_played", summary=False
        )
        playlist = map_tracks_to_msx_playlist(
            playlist_tracks_from_media_items(tracks),
            start,
            prefix,
            player_id,
            self.provider,
            device_param,
            qr_cover_base=await self._qr_cover_base(prefix),
        )
        return web.json_response(dump_msx(playlist))

    async def _handle_msx_search_playlist(self, request: web.Request) -> web.Response:
        """Return search track results as an MSX playlist JSON."""
        player_id, device_param, _ = await self._ensure_player_for_request(request)
        prefix = self._get_prefix(request)
        query = request.query.get("q", "")
        start = _int_param(request.query, "start", 0)
        if not query:
            return web.json_response(dump_msx(MsxContent(items=[])))
        limit = _int_param(request.query, "limit", 20)
        results = await self.provider.mass.music.search(query, limit=limit)
        playlist = map_tracks_to_msx_playlist(
            playlist_tracks_from_media_items(results.tracks),
            start,
            prefix,
            player_id,
            self.provider,
            device_param,
            qr_cover_base=await self._qr_cover_base(prefix),
        )
        return web.json_response(dump_msx(playlist))

    # --- MSX Queue Playlist ---

    async def _handle_queue_playlist(self, request: web.Request) -> web.Response:
        """Return the current MA queue as an MSX native playlist."""
        device_id = request.query.get("device_id")
        device_param = f"device_id={quote(device_id, safe='')}" if device_id else ""
        prefix = self._get_prefix(request)
        player_id = request.match_info["player_id"]
        queue_id = request.query.get("queue_id", player_id)
        queue = self.provider.mass.player_queues.get(queue_id)
        if "start" in request.query:
            start = _non_negative_int_param(request.query, "start", 0)
        else:
            start = queue.current_index or 0 if queue else 0

        queue_items = (
            self.provider.mass.player_queues.items(queue_id, limit=queue.items) if queue else []
        )
        tracks = queue_items_to_tracks(queue_items)

        playlist = map_tracks_to_msx_playlist(
            tracks,
            start,
            prefix,
            player_id,
            self.provider,
            device_param,
            qr_cover_base=await self._qr_cover_base(prefix),
        )
        return web.json_response(dump_msx(playlist))

    # --- MSX Audio Playback ---

    async def _handle_msx_audio(self, request: web.Request) -> web.StreamResponse:
        """Trigger playback via MA queue and stream audio to MSX."""
        player_id = _strip_known_extension(request.match_info["player_id"])

        uri = request.query.get("uri")
        if not uri:
            return web.Response(status=400, text="Invalid uri parameter")

        from_playlist = request.query.get("from_playlist") == "1"
        requested_queue_item_id = request.query.get("queue_item_id")

        player = self.provider.mass.players.get_player(player_id)
        if not player or not isinstance(player, MSXPlayer):
            return web.Response(status=404, text="Player not found")
        if rejected := self._reject_invalid_stream_token(request, player_id):
            return rejected
        try:
            prepared = await prepare_msx_audio(
                self.provider,
                player,
                uri,
                from_playlist=from_playlist,
                queue_item_id=requested_queue_item_id,
            )
        except InvalidDataError as err:
            return web.Response(status=400, text=str(err))
        except ResourceTemporarilyUnavailable as err:
            return web.Response(status=504, text=str(err))
        except MusicAssistantError, OSError, TimeoutError:
            logger.exception("Unable to prepare audio for MSX player %s", player_id)
            return web.Response(status=503, text="Unable to prepare audio")

        return await self._serve_audio_stream(
            request,
            player,
            prepared,
            duration=resolve_served_duration(self.provider.mass, prepared),
        )

    # --- Audio Streaming Infrastructure ---

    def _resolve_served_duration(self, media: PlayerMedia) -> int:
        """Return the length in seconds of the audio served for the given media."""
        return resolve_served_duration(self.provider.mass, media)

    async def _serve_audio_stream(
        self,
        request: web.Request,
        player: MSXPlayer,
        media: PlayerMedia,
        duration: int = 0,
    ) -> web.StreamResponse:
        """Serve this player's current media on this request."""
        return await self.audio.serve(request, player, media, duration)

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
        if self._reject_cross_site(request):
            raise web.HTTPForbidden(text="Cross-site WebSocket rejected")
        if origin := request.headers.get("Origin"):
            request_origin = f"{request.scheme}://{request.host}"
            if origin.rstrip("/").lower() != request_origin.rstrip("/").lower():
                raise web.HTTPForbidden(text="Cross-origin WebSocket rejected")

        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)

        player_id, _, player = await self._ensure_player_for_request(request)
        self._client_prefixes[player_id] = self._get_prefix(request)
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
                self._client_prefixes.pop(player_id, None)
                # Notify the player that its last WS client disconnected
                offline_player = self.provider.mass.players.get_player(player_id)
                if offline_player and isinstance(offline_player, MSXPlayer):
                    offline_player.on_ws_disconnected()
            logger.debug("WebSocket client disconnected for player %s", player_id)

        return ws

    async def _ws_send(
        self, ws: web.WebSocketResponse, text: str, player_id: str | None = None
    ) -> None:
        """Send text to WebSocket; on failure warn and remove the stale client."""
        try:
            await ws.send_str(text)
        except (aiohttp.ClientConnectionError, RuntimeError) as exc:
            logger.warning("WebSocket send failed (player=%s): %s", player_id, exc)
            if player_id:
                self._ws_clients.get(player_id, set()).discard(ws)

    async def _cmd_pause_no_echo(self, player_id: str) -> None:
        """Pause player without echoing back to MSX."""
        player = self.provider.mass.players.get_player(player_id)
        if not (player and isinstance(player, MSXPlayer)):
            return
        with player.suppress_ws_notify():
            await self.provider.mass.players.cmd_pause(player_id)

    async def _cmd_play_no_echo(self, player_id: str) -> None:
        """Resume player without echoing back to MSX."""
        player = self.provider.mass.players.get_player(player_id)
        if not (player and isinstance(player, MSXPlayer)):
            return
        with player.suppress_ws_notify():
            await self.provider.mass.players.cmd_play(player_id)

    def _handle_ws_message(self, player_id: str, data: str) -> None:
        """Process an inbound WebSocket message from MSX."""
        try:
            msg = json.loads(data)
        except json.JSONDecodeError, TypeError:
            logger.debug("Invalid WS message from %s: %s", player_id, data)
            return
        if not isinstance(msg, dict):
            logger.debug("Invalid WS message from %s: %s", player_id, data)
            return

        msg_type = msg.get("type")
        if msg_type == "position":
            position = msg.get("position")
            if _is_finite_position(position):
                player = self.provider.mass.players.get_player(player_id)
                if player and isinstance(player, MSXPlayer):
                    player.update_position(float(position))
                    self.provider.on_player_activity(player_id)
        elif msg_type == "pause":
            player = self.provider.mass.players.get_player(player_id)
            if player and isinstance(player, MSXPlayer):
                position = msg.get("position")
                if _is_finite_position(position):
                    player.update_position(float(position))
                self.provider.mass.create_task(self._cmd_pause_no_echo(player_id))
                self.provider.on_player_activity(player_id)
        elif msg_type == "resume":
            player = self.provider.mass.players.get_player(player_id)
            if player and isinstance(player, MSXPlayer):
                self.provider.mass.create_task(self._cmd_play_no_echo(player_id))
                self.provider.on_player_activity(player_id)
        elif msg_type == "seek":
            position = msg.get("position")
            player = self.provider.mass.players.get_player(player_id)
            if player and isinstance(player, MSXPlayer) and _is_finite_position(position):
                player.note_tv_seek(float(position))
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
                        "artist": album.artist_str,
                        "image": get_image_url(album, self.provider),
                        "uri": album.uri,
                    }
                    for album in albums
                ],
                "total": len(albums),
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
                "total": len(artists),
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
                        "artist": album.artist_str,
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
                "total": len(playlists),
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
                "total": len(tracks),
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
                        "artist": a.artist_str if isinstance(a, Album) else "",
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

    # --- Party Mode ---

    def _cached_party(self) -> PartyInfo | None:
        """Return the last cached party state without refreshing (sync contexts)."""
        return self.party.cached_party()

    async def _qr_cover_base(self, prefix: str) -> str | None:
        """Return the QR-cover endpoint base when a party is active, else None."""
        return await self.party.qr_cover_base(prefix)

    async def _get_active_party(self) -> PartyInfo | None:
        """Return details of the active party, or None when no party is active."""
        return await self.party.get_active_party()

    async def _handle_party_status(self, request: web.Request) -> web.Response:
        """Return party status for MSX party pages."""
        return await self.party.handle_status(request)

    async def _handle_party_qr(self, request: web.Request) -> web.Response:
        """Serve the guest join URL as a QR code image (SVG or PNG by route)."""
        return await self.party.handle_qr(request)

    async def _handle_party_qr_cover(self, request: web.Request) -> web.Response:
        """Serve a cover image with the party QR stamped into its corner (PNG)."""
        return await self.party.handle_qr_cover(
            request,
            extra_bases=[
                self.provider.mass.webserver.base_url,
                self.provider.mass.streams.base_url,
            ],
        )

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
        fetch_site = request.headers.get("Sec-Fetch-Site", "").lower()
        if fetch_site not in ("", "none", "same-origin"):
            return web.json_response({"error": "Cross-site request rejected"}, status=403)
        return None

    async def _handle_play_context(self, request: web.Request) -> web.Response:
        """Enqueue a container or track into the MA queue and start at the given index."""
        if rejected := self._reject_cross_site(request):
            return rejected
        player_id = _strip_known_extension(request.match_info["player_id"])
        player = self._get_msx_player(player_id)
        if player is None:
            return _msx_execute_error(404, "Unknown MSX player")
        uri = request.query.get("uri")
        if not isinstance(uri, str) or not uri or not await is_media_item_uri(uri):
            return _msx_execute_error(400, "Invalid uri")
        start = _non_negative_int_param(request.query, "start", 0)
        track_uri = request.query.get("track")
        if track_uri and not await is_media_item_uri(track_uri):
            track_uri = None
        self.provider.on_player_activity(player_id)
        try:
            async with ImpersonatedUser(self.provider.mass, None):
                with player.suppress_ws_notify():
                    player.expect_new_media()
                    await self.provider.mass.player_queues.play_media(player_id, uri)
                    await self._start_play_context(
                        player_id, player, track_uri=track_uri, start=start
                    )
        except MusicAssistantError, OSError, TimeoutError:
            logger.exception("Unable to start playback for MSX player %s", player_id)
            return _msx_execute_error(503, "Unable to start playback")
        return _msx_execute_ok(self._queue_playlist_action(request, player_id))

    async def _handle_play(self, request: web.Request) -> web.Response:
        """Start playback of a track."""
        if rejected := self._reject_cross_site(request):
            return rejected
        try:
            body = await request.json()
        except json.JSONDecodeError, UnicodeDecodeError, LookupError:
            return web.json_response({"error": "Invalid JSON body"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"error": "Invalid JSON body"}, status=400)

        track_uri = body.get("track_uri")
        player_id = body.get("player_id")
        # the body is untyped JSON, so the type matters as much as the presence
        if not isinstance(track_uri, str) or not isinstance(player_id, str):
            return web.json_response({"error": "Invalid track_uri or player_id"}, status=400)
        if not track_uri or not player_id:
            return web.json_response({"error": "Missing track_uri or player_id"}, status=400)
        if not await is_media_item_uri(track_uri):
            return web.json_response({"error": "Invalid track_uri"}, status=400)

        if self._get_msx_player(player_id) is None:
            return web.json_response({"error": "Unknown MSX player"}, status=404)

        async with ImpersonatedUser(self.provider.mass, None):
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
        player = self._get_msx_player(player_id)
        if player is None:
            return web.json_response({"error": "Unknown MSX player"}, status=404)
        self.provider.on_player_activity(player_id)
        before = self._queue_index(player_id)
        with player.suppress_ws_notify():
            await self.provider.mass.players.cmd_next_track(player_id)
        if not self._queue_advanced(player_id, before):
            return _msx_execute_ok()
        return _msx_execute_ok(self._queue_playlist_action(request, player_id))

    async def _handle_previous(self, request: web.Request) -> web.Response:
        """Skip to previous track."""
        if rejected := self._reject_cross_site(request):
            return rejected
        player_id = _strip_known_extension(request.match_info["player_id"])
        player = self._get_msx_player(player_id)
        if player is None:
            return web.json_response({"error": "Unknown MSX player"}, status=404)
        self.provider.on_player_activity(player_id)
        with player.suppress_ws_notify():
            await self.provider.mass.players.cmd_previous_track(player_id)
        return _msx_execute_ok(self._queue_playlist_action(request, player_id))

    # --- Helpers ---

    async def _start_play_context(
        self,
        player_id: str,
        player: MSXPlayer,
        *,
        track_uri: str | None,
        start: int,
    ) -> None:
        """Jump to the selected track after enqueuing a container."""
        if track_uri or start > 0:
            await player.wait_for_media(timeout=10.0)
        queue = self.provider.mass.player_queues.get_active_queue(player_id)
        if queue is None:
            return
        player.mark_queue_playback(queue.queue_id)
        items = list(self.provider.mass.player_queues.items(queue.queue_id, limit=queue.items))
        target_id: str | None = None
        if track_uri and 0 <= start < len(items):
            candidate = items[start]
            if candidate.media_item is not None and candidate.media_item.uri == track_uri:
                target_id = candidate.queue_item_id
        if target_id is None and track_uri:
            found = find_uri_in_active_queue(self.provider.mass, player_id, track_uri)
            if found:
                target_id = found[1]
        if target_id is None and start > 0 and start < len(items):
            target_id = items[start].queue_item_id
        current_id = player.current_media.queue_item_id if player.current_media else None
        if target_id is not None and target_id != current_id:
            await self.provider.mass.player_queues.play_index(queue.queue_id, target_id)

    def _queue_index(self, player_id: str) -> int | None:
        """Return the active queue's current index, if any."""
        queue = self.provider.mass.player_queues.get_active_queue(player_id)
        if queue is None:
            return None
        return queue.current_index

    def _queue_advanced(self, player_id: str, before: int | None) -> bool:
        """Return whether next/previous changed the item, or repeat-one restarted it."""
        queue = self.provider.mass.player_queues.get_active_queue(player_id)
        after = queue.current_index if queue is not None else None
        if before != after:
            return True
        return queue is not None and queue.repeat_mode == RepeatMode.ONE

    def _queue_playlist_action(self, request: web.Request, player_id: str) -> str:
        """Build a playlist: action rotated so the current MA item is index 0."""
        prefix = self._get_prefix(request)
        queue = self.provider.mass.player_queues.get_active_queue(player_id)
        queue_id = queue.queue_id if queue is not None else player_id
        start = queue.current_index or 0 if queue is not None else 0
        url = f"{prefix}/msx/queue-playlist/{player_id}.json?start={start}&queue_id={queue_id}"
        device_id = request.query.get("device_id")
        if device_id:
            url = f"{url}&device_id={quote(device_id, safe='')}"
        return f"playlist:{url}"

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
        remote_ip = request.remote
        # Web player clients pass source=web to distinguish from MSX TV players
        prefix_label = "WEB TV" if request.query.get("source") == "web" else "MSX TV"
        display_name = self.provider.player_display_name(
            player_id, prefix_label=prefix_label, remote_ip=remote_ip
        )
        player = await self.provider.get_or_register_player(
            player_id, display_name=display_name, ip_address=remote_ip
        )
        return player_id, device_param, player

    def _format_track(self, track: PlayableMediaItemType | ItemMapping) -> dict[str, Any]:
        """Format a track object for the API response."""
        return {
            "item_id": str(track.item_id),
            "name": track.name,
            "artist": track.artist_str if isinstance(track, Track) else "",
            "album": track.album.name if isinstance(track, Track) and track.album else "",
            "duration": track.duration if isinstance(track, Track) else 0,
            "image": self.provider.mass.metadata.get_image_url(track.image)
            if track.image
            else None,
            "uri": track.uri,
        }
