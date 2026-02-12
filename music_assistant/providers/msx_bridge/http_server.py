"""Embedded HTTP server for the MSX Bridge Provider."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import quote

from aiohttp import web
from music_assistant_models.enums import ContentType
from music_assistant_models.media_items import AudioFormat

from music_assistant.helpers.ffmpeg import get_ffmpeg_stream

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
    from .provider import MSXBridgeProvider

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


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
        self._setup_routes()

    def _setup_routes(self) -> None:
        """Register all HTTP routes."""
        # MSX bootstrap
        self.app.router.add_get("/", self._handle_root)
        self.app.router.add_get("/msx/start.json", self._handle_start_json)
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
            "/msx/playlist/recently-played.json", self._handle_msx_recently_played_playlist
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

        # Playback control
        self.app.router.add_post("/api/play", self._handle_play)
        self.app.router.add_route("*", "/api/pause/{player_id}", self._handle_pause)
        self.app.router.add_route("*", "/api/stop/{player_id}", self._handle_stop)
        self.app.router.add_route("*", "/api/quick-stop/{player_id}", self._handle_quick_stop)
        self.app.router.add_route("*", "/api/next/{player_id}", self._handle_next)
        self.app.router.add_route("*", "/api/previous/{player_id}", self._handle_previous)

    @web.middleware
    async def _cors_middleware(self, request: web.Request, handler: Any) -> web.StreamResponse:
        """Add CORS headers to all responses."""
        if request.method == "OPTIONS":
            return web.Response(
                headers={
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                    "Access-Control-Allow-Headers": "*",
                }
            )
        response: web.StreamResponse = await handler(request)
        response.headers["Access-Control-Allow-Origin"] = "*"
        return response

    async def start(self) -> None:
        """Start the HTTP server."""
        self._runner = web.AppRunner(self.app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "0.0.0.0", self.port, reuse_address=True)
        await site.start()
        logger.info("MSX Bridge HTTP server started on port %s", self.port)

    async def stop(self) -> None:
        """Stop the HTTP server."""
        for clients in self._ws_clients.values():
            for ws in clients:
                if not ws.closed:
                    await ws.close()
        self._ws_clients.clear()
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        logger.info("MSX Bridge HTTP server stopped")

    # --- MSX Bootstrap Routes ---

    async def _handle_root(self, request: web.Request) -> web.Response:
        """Serve status dashboard."""
        players = self.provider.players
        base = f"http://{request.host}"
        player_rows = []
        for p in players:
            row = f'<li class="player-row"><span>{p.display_name} — {p.playback_state.value}</span>'
            row += f'<form method="post" action="{base}/api/quick-stop/{p.player_id}" '
            row += 'style="display:inline">'
            row += '<button type="submit" class="btn">Quick stop</button></form></li>'
            player_rows.append(row)
        player_info = "".join(player_rows) if player_rows else ""
        html = f"""<!DOCTYPE html>
<html>
<head><title>MSX Bridge</title>
<style>
body {{ font-family: system-ui, sans-serif; max-width: 800px; margin: 50px auto; padding: 20px; }}
.info {{ background: #e3f2fd; padding: 15px; border-radius: 5px; margin: 10px 0; }}
code {{ background: #f5f5f5; padding: 2px 6px; border-radius: 3px; }}
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
<code>http://{request.host}/msx/start.json</code>
</div>
<div class="info">
<h3>Players</h3>
<ul>{player_info or "<li>No players registered</li>"}</ul>
</div>
</body>
</html>"""
        return web.Response(text=html, content_type="text/html")

    async def _handle_start_json(self, request: web.Request) -> web.Response:
        """Return MSX start configuration."""
        host = request.host
        prefix = f"http://{host}"
        # Version in URL forces MSX to refetch plugin after menu changes (avoids cache)
        start_config = {
            "name": "Music Assistant",
            "version": "1.0.1",
            "parameter": f"menu:request:interaction:init@{prefix}/msx/plugin.html?v=3",
        }
        return web.json_response(start_config)

    def _serve_static(self, filename: str) -> Any:
        """Create a handler that serves a static file from the static directory."""
        path = STATIC_DIR / filename

        async def handler(_request: web.Request) -> web.FileResponse:
            return web.FileResponse(path)

        return handler

    async def _handle_msx_plugin_html(self, request: web.Request) -> web.Response:
        """Serve plugin.html with no-cache so MSX always gets latest menu order."""
        path = STATIC_DIR / "plugin.html"
        response = cast("web.Response", web.FileResponse(path))
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
        prefix = f"http://{request.host}"
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
        prefix = f"http://{request.host}"
        limit = int(request.query.get("limit", "50"))
        offset = int(request.query.get("offset", "0"))
        albums = await self.provider.mass.music.albums.library_items(limit=limit, offset=offset)

        items = await asyncio.gather(
            *(map_album_to_msx(a, prefix, self.provider, device_param) for a in albums)
        )
        content = MsxContent(
            headline="Albums",
            template=MsxTemplate(
                type="separate",
                layout="0,0,2,4",
                image_filler="default",
            ),
            items=items if items else [MsxItem(title="No albums found")],
        )
        return web.json_response(content.model_dump(by_alias=True, exclude_none=True))

    async def _handle_msx_artists(self, request: web.Request) -> web.Response:
        """Return artists as an MSX content page."""
        _, device_param, _ = await self._ensure_player_for_request(request)
        prefix = f"http://{request.host}"
        limit = int(request.query.get("limit", "50"))
        offset = int(request.query.get("offset", "0"))
        artists = await self.provider.mass.music.artists.library_items(limit=limit, offset=offset)

        items = [map_artist_to_msx(a, prefix, self.provider, device_param) for a in artists]
        content = MsxContent(
            headline="Artists",
            template=MsxTemplate(
                type="separate",
                layout="0,0,2,4",
                icon="msx-white-soft:person",
                image_filler="default",
            ),
            items=items if items else [MsxItem(title="No artists found")],
        )
        return web.json_response(content.model_dump(by_alias=True, exclude_none=True))

    async def _handle_msx_playlists(self, request: web.Request) -> web.Response:
        """Return playlists as an MSX content page."""
        _, device_param, _ = await self._ensure_player_for_request(request)
        prefix = f"http://{request.host}"
        limit = int(request.query.get("limit", "50"))
        offset = int(request.query.get("offset", "0"))
        playlists = await self.provider.mass.music.playlists.library_items(
            limit=limit, offset=offset
        )

        items = [map_playlist_to_msx(p, prefix, self.provider, device_param) for p in playlists]
        content = MsxContent(
            headline="Playlists",
            template=MsxTemplate(
                type="separate",
                layout="0,0,2,4",
                icon="msx-white-soft:playlist-play",
                image_filler="default",
            ),
            items=items if items else [MsxItem(title="No playlists found")],
        )
        return web.json_response(content.model_dump(by_alias=True, exclude_none=True))

    async def _handle_msx_tracks(self, request: web.Request) -> web.Response:
        """Return tracks as an MSX content page."""
        player_id, device_param, _ = await self._ensure_player_for_request(request)
        prefix = f"http://{request.host}"
        limit = int(request.query.get("limit", "50"))
        offset = int(request.query.get("offset", "0"))
        tracks = await self.provider.mass.music.tracks.library_items(limit=limit, offset=offset)

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
                type="separate",
                layout="0,0,2,4",
                icon="msx-white-soft:audiotrack",
                image_filler="default",
            ),
            items=items if items else [MsxItem(title="No tracks found")],
        )
        return web.json_response(content.model_dump(by_alias=True, exclude_none=True))

    async def _handle_msx_recently_played(self, request: web.Request) -> web.Response:
        """Return recently played tracks as an MSX content page."""
        player_id, device_param, _ = await self._ensure_player_for_request(request)
        prefix = f"http://{request.host}"
        tracks = await self.provider.mass.music.tracks.library_items(
            limit=50, order_by="last_played"
        )
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
                type="separate",
                layout="0,0,2,4",
                icon="msx-white-soft:history",
                image_filler="default",
            ),
            items=items if items else [MsxItem(title="No recently played tracks")],
        )
        return web.json_response(content.model_dump(by_alias=True, exclude_none=True))

    async def _handle_msx_search_page(self, request: web.Request) -> web.Response:
        """Return a content page whose page-level action launches the Input Plugin keyboard."""
        _, device_param, _ = await self._ensure_player_for_request(request)
        prefix = f"http://{request.host}"
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
        prefix = f"http://{request.host}"
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

        limit = int(request.query.get("limit", "20"))
        results = await self.provider.mass.music.search(query, limit=limit)
        items = []
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
        prefix = f"http://{request.host}"
        query = request.query.get("q", "")
        if not query:
            return web.json_response(
                MsxContent(
                    headline="Search",
                    items=[MsxItem(title="Please enter a search query")],
                ).model_dump(by_alias=True, exclude_none=True)
            )

        limit = int(request.query.get("limit", "20"))
        results = await self.provider.mass.music.search(query, limit=limit)
        items = []
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

    # --- MSX Detail Pages ---

    async def _handle_msx_album_tracks(self, request: web.Request) -> web.Response:
        """Return tracks for an album as an MSX content page."""
        player_id, device_param, _ = await self._ensure_player_for_request(request)
        prefix = f"http://{request.host}"
        item_id = request.match_info["item_id"]
        provider = request.query.get("provider", "library")
        try:
            tracks = await self.provider.mass.music.albums.tracks(item_id, provider)
        except Exception:
            logger.warning("Failed to fetch tracks for album %s", item_id)
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
                type="separate",
                layout="0,0,2,4",
                image_filler="default",
            ),
            items=items if items else [MsxItem(title="No tracks found")],
        )
        return web.json_response(content.model_dump(by_alias=True, exclude_none=True))

    async def _handle_msx_artist_albums(self, request: web.Request) -> web.Response:
        """Return albums for an artist as an MSX content page."""
        _, device_param, _ = await self._ensure_player_for_request(request)
        prefix = f"http://{request.host}"
        item_id = request.match_info["item_id"]
        try:
            albums = await self.provider.mass.music.artists.albums(item_id, "library")
        except Exception:
            logger.warning("Failed to fetch albums for artist %s", item_id)
            albums = []

        items = await asyncio.gather(
            *(map_album_to_msx(a, prefix, self.provider, device_param) for a in albums)
        )
        content = MsxContent(
            headline="Artist Albums",
            template=MsxTemplate(
                type="separate",
                layout="0,0,2,4",
                image_filler="default",
            ),
            items=items if items else [MsxItem(title="No albums found")],
        )
        return web.json_response(content.model_dump(by_alias=True, exclude_none=True))

    async def _handle_msx_playlist_tracks(self, request: web.Request) -> web.Response:
        """Return tracks for a playlist as an MSX content page."""
        player_id, device_param, _ = await self._ensure_player_for_request(request)
        prefix = f"http://{request.host}"
        item_id = request.match_info["item_id"]
        try:
            tracks = [
                t async for t in self.provider.mass.music.playlists.tracks(item_id, "library")
            ]
        except Exception:
            logger.warning("Failed to fetch tracks for playlist %s", item_id)
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
                type="separate",
                layout="0,0,2,4",
                icon="msx-white-soft:audiotrack",
                image_filler="default",
            ),
            items=items if items else [MsxItem(title="No tracks found")],
        )
        return web.json_response(content.model_dump(by_alias=True, exclude_none=True))

    # --- MSX Playlist Endpoints ---

    async def _handle_msx_album_playlist(self, request: web.Request) -> web.Response:
        """Return album tracks as an MSX playlist JSON."""
        player_id, device_param, _ = await self._ensure_player_for_request(request)
        prefix = f"http://{request.host}"
        item_id = request.match_info["item_id"]
        provider_name = request.query.get("provider", "library")
        start = int(request.query.get("start", "0"))
        try:
            tracks = await self.provider.mass.music.albums.tracks(item_id, provider_name)
        except Exception:
            logger.warning("Failed to fetch tracks for album playlist %s", item_id)
            tracks = []
        playlist = map_tracks_to_msx_playlist(
            tracks, start, prefix, player_id, self.provider, device_param
        )
        return web.json_response(playlist.model_dump(by_alias=True, exclude_none=True))

    async def _handle_msx_playlist_playlist(self, request: web.Request) -> web.Response:
        """Return playlist tracks as an MSX playlist JSON."""
        player_id, device_param, _ = await self._ensure_player_for_request(request)
        prefix = f"http://{request.host}"
        item_id = request.match_info["item_id"]
        start = int(request.query.get("start", "0"))
        try:
            tracks = [
                t async for t in self.provider.mass.music.playlists.tracks(item_id, "library")
            ]
        except Exception:
            logger.warning("Failed to fetch tracks for playlist playlist %s", item_id)
            tracks = []
        playlist = map_tracks_to_msx_playlist(
            tracks, start, prefix, player_id, self.provider, device_param
        )
        return web.json_response(playlist.model_dump(by_alias=True, exclude_none=True))

    async def _handle_msx_tracks_playlist(self, request: web.Request) -> web.Response:
        """Return library tracks as an MSX playlist JSON."""
        player_id, device_param, _ = await self._ensure_player_for_request(request)
        prefix = f"http://{request.host}"
        limit = int(request.query.get("limit", "50"))
        offset = int(request.query.get("offset", "0"))
        start = int(request.query.get("start", "0"))
        tracks = await self.provider.mass.music.tracks.library_items(limit=limit, offset=offset)
        playlist = map_tracks_to_msx_playlist(
            list(tracks), start, prefix, player_id, self.provider, device_param
        )
        return web.json_response(playlist.model_dump(by_alias=True, exclude_none=True))

    async def _handle_msx_recently_played_playlist(self, request: web.Request) -> web.Response:
        """Return recently played tracks as an MSX playlist JSON."""
        player_id, device_param, _ = await self._ensure_player_for_request(request)
        prefix = f"http://{request.host}"
        start = int(request.query.get("start", "0"))
        tracks = await self.provider.mass.music.tracks.library_items(
            limit=50, order_by="last_played"
        )
        playlist = map_tracks_to_msx_playlist(
            list(tracks), start, prefix, player_id, self.provider, device_param
        )
        return web.json_response(playlist.model_dump(by_alias=True, exclude_none=True))

    async def _handle_msx_search_playlist(self, request: web.Request) -> web.Response:
        """Return search track results as an MSX playlist JSON."""
        player_id, device_param, _ = await self._ensure_player_for_request(request)
        prefix = f"http://{request.host}"
        query = request.query.get("q", "")
        start = int(request.query.get("start", "0"))
        if not query:
            return web.json_response(
                MsxContent(items=[]).model_dump(by_alias=True, exclude_none=True)
            )
        limit = int(request.query.get("limit", "20"))
        results = await self.provider.mass.music.search(query, limit=limit)
        playlist = map_tracks_to_msx_playlist(
            list(results.tracks), start, prefix, player_id, self.provider, device_param
        )
        return web.json_response(playlist.model_dump(by_alias=True, exclude_none=True))

    # --- MSX Queue Playlist ---

    async def _handle_queue_playlist(self, request: web.Request) -> web.Response:
        """Return the current MA queue as an MSX native playlist."""
        _, device_param, _ = await self._ensure_player_for_request(request)
        prefix = f"http://{request.host}"
        player_id = request.match_info["player_id"]
        start = int(request.query.get("start", "0"))

        try:
            queue_items = self.provider.mass.player_queues.items(player_id)
        except Exception:
            logger.warning("Failed to fetch queue items for %s", player_id)
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
            tracks, start, prefix, player_id, self.provider, device_param
        )
        return web.json_response(playlist.model_dump(by_alias=True, exclude_none=True))

    # --- MSX Audio Playback ---

    async def _handle_msx_audio(self, request: web.Request) -> web.StreamResponse:
        """Trigger playback via MA queue and stream audio to MSX."""
        player_id = request.match_info["player_id"]
        # Strip any extensions MSX might have appended (.mp3, .json, etc)
        if "." in player_id:
            player_id = player_id.rsplit(".", 1)[0]

        uri = request.query.get("uri")
        if not uri:
            return web.Response(status=400, text="Missing uri parameter")

        from_playlist = request.query.get("from_playlist") == "1"

        self.provider.on_player_activity(player_id)
        player = self.provider.mass.players.get(player_id)
        if not player or not isinstance(player, MSXPlayer):
            return web.Response(status=404, text="Player not found")

        # Suppress WS broadcast when called from MSX playlist to avoid conflicts
        if from_playlist:
            player._skip_ws_notify = True

        await self.provider.mass.player_queues.play_media(player_id, uri)

        # Reset skip flag after play_media
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
        )

    @staticmethod
    def _build_audio_params(
        output_format_str: str,
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
        headers: dict[str, str] = {
            "Content-Type": mime_type,
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Accept-Ranges": "none",
            "Transfer-Encoding": "chunked",
        }
        return pcm_format, out_format, headers

    async def _serve_audio_stream(
        self,
        request: web.Request,
        player: MSXPlayer,
        media: Any,
    ) -> web.StreamResponse:
        """Unified method to stream audio from MA to MSX via ffmpeg.

        Pre-buffers audio data before sending HTTP headers so MSX receives
        the response and initial audio burst simultaneously, preventing
        stutter/restart from an empty initial buffer.
        """
        player_id = player.player_id
        pcm_format, out_format, headers = self._build_audio_params(
            player.output_format,
        )
        audio_source = self.provider.mass.streams.get_stream(
            media,
            pcm_format,
            force_flow_mode=False,
        )

        logger.debug(
            "Serving audio %s: format=%s",
            player_id,
            player.output_format,
        )

        response = web.StreamResponse(status=200, headers=headers)
        stream_task: asyncio.Task[None] = asyncio.create_task(
            self._stream_with_prebuffer(
                request, response, player, headers, audio_source, pcm_format, out_format
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
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            logger.debug("Client disconnected from stream %s", player_id)
        except asyncio.CancelledError:
            logger.debug("Stream cancelled for player %s", player_id)
            raise
        finally:
            if producer_task and not producer_task.done():
                producer_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await producer_task
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
        """WebSocket for push playback — clients subscribe by player_id.

        Uses the same player_id derivation (device_id or IP) as content and
        stream endpoints so broadcast_stop reaches the correct client.
        Registers the player in MA on connect so the player appears when MSX starts.
        """
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)

        player_id, _, _ = await self._ensure_player_for_request(request)
        if player_id not in self._ws_clients:
            self._ws_clients[player_id] = set()
        self._ws_clients[player_id].add(ws)
        logger.info(
            "WebSocket connected: player_id=%s, clients_for_player=%d, all_players=%s",
            player_id,
            len(self._ws_clients[player_id]),
            list(self._ws_clients.keys()),
        )

        try:
            async for _msg in ws:
                pass
        finally:
            self._ws_clients.get(player_id, set()).discard(ws)
            if not self._ws_clients.get(player_id):
                self._ws_clients.pop(player_id, None)
            logger.debug("WebSocket client disconnected for player %s", player_id)

        return ws

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
        play_path = f"/stream/{player_id}"

        payload: dict[str, Any] = {"type": "play", "path": play_path}
        if title:
            payload["title"] = title
        if artist:
            payload["artist"] = artist
        if image_url:
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
                self.provider.mass.create_task(self._ws_send(ws, msg))

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
        payload: dict[str, Any] = {"type": "playlist", "url": playlist_url}
        msg = json.dumps(payload)
        for ws in list(clients):
            if not ws.closed:
                self.provider.mass.create_task(self._ws_send(ws, msg))

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
                self.provider.mass.create_task(self._ws_send(ws, msg))

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
                self.provider.mass.create_task(self._ws_send(ws, msg))

    async def _ws_send(self, ws: web.WebSocketResponse, text: str) -> None:
        """Send text to WebSocket, ignore errors."""
        try:
            await ws.send_str(text)
        except Exception as exc:
            logger.debug("WebSocket send failed: %s", exc)

    # --- Stream Proxy ---

    async def _handle_stream(self, request: web.Request) -> web.StreamResponse:
        """Stream audio from MA to the TV using internal API."""
        player_id = request.match_info["player_id"]
        # Strip any extensions MSX might have appended (.mp3, .json, etc)
        if "." in player_id:
            player_id = player_id.rsplit(".", 1)[0]

        self.provider.on_player_activity(player_id)
        player = self.provider.mass.players.get(player_id)
        if not player or not isinstance(player, MSXPlayer):
            return web.Response(status=404, text="Player not found")

        media = player.current_media
        if not media:
            return web.Response(status=404, text="No active stream")

        return await self._serve_audio_stream(
            request,
            player,
            media,
        )

    # --- Library API Routes ---

    async def _handle_albums(self, request: web.Request) -> web.Response:
        """List albums."""
        limit = int(request.query.get("limit", "50"))
        offset = int(request.query.get("offset", "0"))
        albums = await self.provider.mass.music.albums.library_items(limit=limit, offset=offset)
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
        limit = int(request.query.get("limit", "50"))
        offset = int(request.query.get("offset", "0"))
        artists = await self.provider.mass.music.artists.library_items(limit=limit, offset=offset)
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
        limit = int(request.query.get("limit", "50"))
        offset = int(request.query.get("offset", "0"))
        playlists = await self.provider.mass.music.playlists.library_items(
            limit=limit, offset=offset
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
        limit = int(request.query.get("limit", "50"))
        offset = int(request.query.get("offset", "0"))
        tracks = await self.provider.mass.music.tracks.library_items(limit=limit, offset=offset)
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
        limit = int(request.query.get("limit", "20"))
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
        limit = int(request.query.get("limit", "20"))
        tracks = await self.provider.mass.music.tracks.library_items(
            limit=limit, order_by="last_played"
        )
        return web.json_response(
            {
                "items": [self._format_track(track) for track in tracks],
            }
        )

    async def _handle_play(self, request: web.Request) -> web.Response:
        """Start playback of a track."""
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON body"}, status=400)

        track_uri = body.get("track_uri")
        player_id = body.get("player_id")
        if not track_uri or not player_id:
            return web.json_response({"error": "Missing track_uri or player_id"}, status=400)

        await self.provider.mass.player_queues.play_media(player_id, track_uri)
        return web.json_response({"status": "ok"})

    async def _handle_pause(self, request: web.Request) -> web.Response:
        """Pause playback."""
        player_id = request.match_info["player_id"]
        self.provider.on_player_activity(player_id)
        await self.provider.mass.players.cmd_pause(player_id)
        return web.json_response({"status": "ok"})

    async def _handle_stop(self, request: web.Request) -> web.Response:
        """Stop playback."""
        player_id = request.match_info["player_id"]
        self.provider.on_player_activity(player_id)
        await self.provider.mass.players.cmd_stop(player_id)
        return web.json_response({"status": "ok"})

    async def _handle_quick_stop(self, request: web.Request) -> web.Response:
        """Stop playback on MSX immediately (same signal as Disable)."""
        player_id = request.match_info["player_id"]
        self.provider.on_player_activity(player_id)
        await self.provider.mass.players.cmd_stop(player_id)
        self.provider.notify_play_stopped(player_id)
        accept = request.headers.get("Accept", "")
        if "text/html" in accept:
            return web.Response(status=303, headers={"Location": "/"})
        return web.json_response({"status": "ok"})

    async def _handle_next(self, request: web.Request) -> web.Response:
        """Skip to next track."""
        player_id = request.match_info["player_id"]
        self.provider.on_player_activity(player_id)
        await self.provider.mass.players.cmd_next_track(player_id)
        return web.json_response({"status": "ok"})

    async def _handle_previous(self, request: web.Request) -> web.Response:
        """Skip to previous track."""
        player_id = request.match_info["player_id"]
        self.provider.on_player_activity(player_id)
        await self.provider.mass.players.cmd_previous_track(player_id)
        return web.json_response({"status": "ok"})

    # --- Helpers ---

    def _get_player_id_and_device_param(self, request: web.Request) -> tuple[str, str]:
        """
        Extract player_id and device_id query param from request.

        Returns (player_id, device_param) where device_param is e.g. "device_id=xxx"
        or "" if using IP fallback.
        """
        device_id = request.query.get("device_id")
        if device_id:
            sanitized = PLAYER_ID_SANITIZE_RE.sub("_", device_id).strip("_") or "device"
            player_id = f"{MSX_PLAYER_ID_PREFIX}{sanitized}"
            param = f"device_id={quote(device_id, safe='')}"
        else:
            remote = request.remote
            ip = remote if remote else "0_0_0_0"
            sanitized = PLAYER_ID_SANITIZE_RE.sub("_", ip.replace(".", "_")).strip("_") or "ip"
            player_id = f"{MSX_PLAYER_ID_PREFIX}{sanitized}"
            param = ""
        return player_id, param

    def _append_device_param(self, url: str, device_param: str) -> str:
        """Append device_id to URL if present."""
        return append_device_param(url, device_param)

    async def _ensure_player_for_request(
        self, request: web.Request
    ) -> tuple[str, str, MSXPlayer | None]:
        """
        Get or register player for this request.

        Returns (player_id, device_param, player).
        Player may be None if registration failed.
        """
        player_id, device_param = self._get_player_id_and_device_param(request)
        player = await self.provider.get_or_register_player(player_id)
        return player_id, device_param, player

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
