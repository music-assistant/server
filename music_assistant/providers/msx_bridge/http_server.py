"""Embedded HTTP server for the MSX Bridge Provider."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any
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
)
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
        self._setup_routes()

    def _setup_routes(self) -> None:
        """Register all HTTP routes."""
        # MSX bootstrap
        self.app.router.add_get("/", self._handle_root)
        self.app.router.add_get("/msx/start.json", self._handle_start_json)
        self.app.router.add_get("/msx/plugin.html", self._serve_static("plugin.html"))
        self.app.router.add_get(
            "/msx/tvx-plugin-module.min.js",
            self._serve_static("tvx-plugin-module.min.js"),
        )
        self.app.router.add_get("/msx/tvx-plugin.min.js", self._serve_static("tvx-plugin.min.js"))
        self.app.router.add_get("/msx/input.html", self._serve_static("input.html"))
        self.app.router.add_get("/msx/input.js", self._serve_static("input.js"))

        # MSX content pages (native MSX JSON navigation)
        self.app.router.add_get("/msx/menu.json", self._handle_msx_menu)
        self.app.router.add_get("/msx/albums.json", self._handle_msx_albums)
        self.app.router.add_get("/msx/artists.json", self._handle_msx_artists)
        self.app.router.add_get("/msx/playlists.json", self._handle_msx_playlists)
        self.app.router.add_get("/msx/tracks.json", self._handle_msx_tracks)
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

        # MSX audio playback
        self.app.router.add_get("/msx/audio/{player_id}", self._handle_msx_audio)

        # Health
        self.app.router.add_get("/health", self._handle_health)

        # WebSocket for push playback (MA -> MSX)
        self.app.router.add_get("/ws", self._handle_ws)

        # Stream proxy
        self.app.router.add_get("/stream/{player_id}", self._handle_stream)

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
        self.app.router.add_post("/api/pause/{player_id}", self._handle_pause)
        self.app.router.add_post("/api/stop/{player_id}", self._handle_stop)
        self.app.router.add_post("/api/next/{player_id}", self._handle_next)
        self.app.router.add_post("/api/previous/{player_id}", self._handle_previous)

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
        site = web.TCPSite(self._runner, "0.0.0.0", self.port)
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
        player_info = "".join(
            f"<li>{p.display_name} — {p.playback_state.value}</li>" for p in players
        )
        html = f"""<!DOCTYPE html>
<html>
<head><title>MSX Bridge</title>
<style>
body {{ font-family: system-ui, sans-serif; max-width: 800px; margin: 50px auto; padding: 20px; }}
.info {{ background: #e3f2fd; padding: 15px; border-radius: 5px; margin: 10px 0; }}
code {{ background: #f5f5f5; padding: 2px 6px; border-radius: 3px; }}
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
        start_config = {
            "name": "Music Assistant",
            "version": "1.0.0",
            "parameter": f"menu:request:interaction:init@{prefix}/msx/plugin.html",
        }
        return web.json_response(start_config)

    def _serve_static(self, filename: str) -> Any:
        """Create a handler that serves a static file from the static directory."""
        path = STATIC_DIR / filename

        async def handler(_request: web.Request) -> web.FileResponse:
            return web.FileResponse(path)

        return handler

    # --- MSX Content Pages (native MSX JSON) ---

    async def _handle_msx_menu(self, request: web.Request) -> web.Response:
        """Return the main library menu as an MSX content page."""
        _, device_param, _ = await self._ensure_player_for_request(request)
        host = request.host
        prefix = f"http://{host}"
        items = [
            ("Search", "search", f"{prefix}/msx/search-page.json"),
            ("Albums", "msx-white-soft:album", f"{prefix}/msx/albums.json"),
            ("Artists", "msx-white-soft:person", f"{prefix}/msx/artists.json"),
            (
                "Playlists",
                "msx-white-soft:playlist-play",
                f"{prefix}/msx/playlists.json",
            ),
            ("Tracks", "msx-white-soft:audiotrack", f"{prefix}/msx/tracks.json"),
        ]
        return web.json_response(
            {
                "type": "list",
                "headline": "Music Assistant",
                "template": {
                    "type": "separate",
                    "layout": "0,0,2,4",
                    "icon": "msx-white-soft:music-note",
                    "action": "content:{context:content}",
                },
                "items": [
                    {
                        "label": label,
                        "icon": icon,
                        "content": self._append_device_param(url, device_param),
                    }
                    for label, icon, url in items
                ],
            }
        )

    async def _handle_msx_albums(self, request: web.Request) -> web.Response:
        """Return albums as an MSX content page."""
        _, device_param, _ = await self._ensure_player_for_request(request)
        prefix = f"http://{request.host}"
        limit = int(request.query.get("limit", "50"))
        offset = int(request.query.get("offset", "0"))
        albums = list(
            await self.provider.mass.music.albums.library_items(limit=limit, offset=offset)
        )

        # Resolve images: albums from library often lack metadata images,
        # so fall back to the first track's image (which has the album cover).
        async def resolve_image(album: Any) -> str | None:
            url = self._get_image_url(album)
            if url:
                return url
            return await self._get_album_image_fallback(album)

        images = await asyncio.gather(*(resolve_image(a) for a in albums))
        items = []
        for album, image in zip(albums, images, strict=True):
            url = f"{prefix}/msx/albums/{album.item_id}/tracks.json?provider={album.provider}"
            items.append(
                {
                    "title": album.name,
                    "label": getattr(album, "artist_str", ""),
                    "image": image,
                    "action": f"content:{self._append_device_param(url, device_param)}",
                }
            )
        return web.json_response(
            {
                "type": "list",
                "headline": "Albums",
                "template": {
                    "type": "separate",
                    "layout": "0,0,2,4",
                    "imageFiller": "default",
                },
                "items": items if items else [{"title": "No albums found"}],
            }
        )

    async def _handle_msx_artists(self, request: web.Request) -> web.Response:
        """Return artists as an MSX content page."""
        _, device_param, _ = await self._ensure_player_for_request(request)
        prefix = f"http://{request.host}"
        limit = int(request.query.get("limit", "50"))
        offset = int(request.query.get("offset", "0"))
        artists = await self.provider.mass.music.artists.library_items(limit=limit, offset=offset)
        items = []
        for artist in artists:
            url = f"{prefix}/msx/artists/{artist.item_id}/albums.json"
            items.append(
                {
                    "title": artist.name,
                    "image": self._get_image_url(artist),
                    "action": f"content:{self._append_device_param(url, device_param)}",
                }
            )
        return web.json_response(
            {
                "type": "list",
                "headline": "Artists",
                "template": {
                    "type": "separate",
                    "layout": "0,0,2,4",
                    "icon": "msx-white-soft:person",
                    "imageFiller": "default",
                },
                "items": items if items else [{"title": "No artists found"}],
            }
        )

    async def _handle_msx_playlists(self, request: web.Request) -> web.Response:
        """Return playlists as an MSX content page."""
        _, device_param, _ = await self._ensure_player_for_request(request)
        prefix = f"http://{request.host}"
        limit = int(request.query.get("limit", "50"))
        offset = int(request.query.get("offset", "0"))
        playlists = await self.provider.mass.music.playlists.library_items(
            limit=limit, offset=offset
        )
        items = []
        for playlist in playlists:
            url = f"{prefix}/msx/playlists/{playlist.item_id}/tracks.json"
            items.append(
                {
                    "title": playlist.name,
                    "image": self._get_image_url(playlist),
                    "action": f"content:{self._append_device_param(url, device_param)}",
                }
            )
        return web.json_response(
            {
                "type": "list",
                "headline": "Playlists",
                "template": {
                    "type": "separate",
                    "layout": "0,0,2,4",
                    "icon": "msx-white-soft:playlist-play",
                    "imageFiller": "default",
                },
                "items": items if items else [{"title": "No playlists found"}],
            }
        )

    async def _handle_msx_tracks(self, request: web.Request) -> web.Response:
        """Return tracks as an MSX content page."""
        player_id, device_param, _ = await self._ensure_player_for_request(request)
        prefix = f"http://{request.host}"
        limit = int(request.query.get("limit", "50"))
        offset = int(request.query.get("offset", "0"))
        tracks = await self.provider.mass.music.tracks.library_items(limit=limit, offset=offset)
        items = [self._format_msx_track(track, prefix, player_id, device_param) for track in tracks]
        return web.json_response(
            {
                "type": "list",
                "headline": "Tracks",
                "template": {
                    "type": "separate",
                    "layout": "0,0,2,4",
                    "icon": "msx-white-soft:audiotrack",
                    "imageFiller": "default",
                },
                "items": items if items else [{"title": "No tracks found"}],
            }
        )

    async def _handle_msx_search_page(self, request: web.Request) -> web.Response:
        """Return a content page whose page-level action launches the Input Plugin keyboard."""
        _, device_param, _ = await self._ensure_player_for_request(request)
        prefix = f"http://{request.host}"
        search_url = self._append_device_param(
            f"{prefix}/msx/search-input.json?q={{INPUT}}", device_param
        )
        action = (
            f"content:request:interaction:"
            f"{search_url}"
            f"|search:3|en|Search Music||||Search..."
            f"@{prefix}/msx/input.html"
        )
        return web.json_response(
            {
                "type": "list",
                "headline": "Search",
                "action": action,
                "template": {
                    "type": "separate",
                    "layout": "0,0,2,4",
                },
                "items": [
                    {
                        "title": "Search Music",
                        "titleFooter": "Press OK to open keyboard",
                        "icon": "search",
                        "action": action,
                    }
                ],
            }
        )

    async def _handle_msx_search_input(self, request: web.Request) -> web.Response:
        """Return search results for the MSX Input Plugin (search keyboard)."""
        player_id, device_param, _ = await self._ensure_player_for_request(request)
        prefix = f"http://{request.host}"
        query = request.query.get("q", "")
        if not query:
            return web.json_response(
                {
                    "headline": "{ico:search} Search",
                    "hint": "Type to search...",
                    "template": {
                        "type": "separate",
                        "layout": "0,0,2,4",
                        "imageFiller": "default",
                    },
                    "items": [{"title": "Start typing to search"}],
                }
            )
        limit = int(request.query.get("limit", "20"))
        results = await self.provider.mass.music.search(query, limit=limit)
        items = []
        for artist in results.artists:
            url = f"{prefix}/msx/artists/{artist.item_id}/albums.json"
            items.append(
                {
                    "title": artist.name,
                    "label": "Artist",
                    "icon": "msx-white-soft:person",
                    "image": self._get_image_url(artist),
                    "action": f"content:{self._append_device_param(url, device_param)}",
                }
            )
        for album in results.albums:
            url = f"{prefix}/msx/albums/{album.item_id}/tracks.json?provider={album.provider}"
            items.append(
                {
                    "title": album.name,
                    "label": f"Album — {getattr(album, 'artist_str', '')}",
                    "icon": "msx-white-soft:album",
                    "image": self._get_image_url(album),
                    "action": f"content:{self._append_device_param(url, device_param)}",
                }
            )
        for track in results.tracks:
            item = self._format_msx_track(track, prefix, player_id, device_param)
            item["label"] = f"Track — {getattr(track, 'artist_str', '')}"
            item["icon"] = "msx-white-soft:audiotrack"
            items.append(item)
        return web.json_response(
            {
                "headline": f'{{ico:search}} "{query}"',
                "hint": f"Found {len(items)} items",
                "template": {
                    "type": "separate",
                    "layout": "0,0,2,4",
                    "imageFiller": "default",
                },
                "items": items if items else [{"title": "No results found"}],
            }
        )

    async def _handle_msx_search(self, request: web.Request) -> web.Response:
        """Return search results as an MSX content page."""
        player_id, device_param, _ = await self._ensure_player_for_request(request)
        prefix = f"http://{request.host}"
        query = request.query.get("q", "")
        if not query:
            return web.json_response(
                {
                    "type": "list",
                    "headline": "Search",
                    "items": [{"title": "Please enter a search query"}],
                }
            )
        limit = int(request.query.get("limit", "20"))
        results = await self.provider.mass.music.search(query, limit=limit)
        items = []
        for artist in results.artists:
            url = f"{prefix}/msx/artists/{artist.item_id}/albums.json"
            items.append(
                {
                    "title": artist.name,
                    "label": "Artist",
                    "icon": "msx-white-soft:person",
                    "image": self._get_image_url(artist),
                    "action": f"content:{self._append_device_param(url, device_param)}",
                }
            )
        for album in results.albums:
            url = f"{prefix}/msx/albums/{album.item_id}/tracks.json?provider={album.provider}"
            items.append(
                {
                    "title": album.name,
                    "label": f"Album — {getattr(album, 'artist_str', '')}",
                    "icon": "msx-white-soft:album",
                    "image": self._get_image_url(album),
                    "action": f"content:{self._append_device_param(url, device_param)}",
                }
            )
        for track in results.tracks:
            item = self._format_msx_track(track, prefix, player_id, device_param)
            item["label"] = f"Track — {getattr(track, 'artist_str', '')}"
            item["icon"] = "msx-white-soft:audiotrack"
            items.append(item)
        return web.json_response(
            {
                "type": "list",
                "headline": f"Search: {query}",
                "template": {
                    "type": "separate",
                    "layout": "0,0,2,4",
                    "imageFiller": "default",
                },
                "items": items if items else [{"title": "No results found"}],
            }
        )

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
        items = [self._format_msx_track(track, prefix, player_id, device_param) for track in tracks]
        return web.json_response(
            {
                "type": "list",
                "headline": "Album Tracks",
                "template": {
                    "type": "separate",
                    "layout": "0,0,2,4",
                    "imageFiller": "default",
                },
                "items": items if items else [{"title": "No tracks found"}],
            }
        )

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
        items = []
        for album in albums:
            url = f"{prefix}/msx/albums/{album.item_id}/tracks.json?provider={album.provider}"
            items.append(
                {
                    "title": album.name,
                    "label": getattr(album, "artist_str", ""),
                    "image": self._get_image_url(album),
                    "action": f"content:{self._append_device_param(url, device_param)}",
                }
            )
        return web.json_response(
            {
                "type": "list",
                "headline": "Artist Albums",
                "template": {
                    "type": "separate",
                    "layout": "0,0,2,4",
                    "imageFiller": "default",
                },
                "items": items if items else [{"title": "No albums found"}],
            }
        )

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
        items = [self._format_msx_track(track, prefix, player_id, device_param) for track in tracks]
        return web.json_response(
            {
                "type": "list",
                "headline": "Playlist Tracks",
                "template": {
                    "type": "separate",
                    "layout": "0,0,2,4",
                    "icon": "msx-white-soft:audiotrack",
                    "imageFiller": "default",
                },
                "items": items if items else [{"title": "No tracks found"}],
            }
        )

    # --- MSX Audio Playback ---

    async def _handle_msx_audio(self, request: web.Request) -> web.StreamResponse:
        """Trigger playback via MA queue and stream audio to MSX."""
        player_id = request.match_info["player_id"]
        uri = request.query.get("uri")
        if not uri:
            return web.Response(status=400, text="Missing uri parameter")

        self.provider.on_player_activity(player_id)
        player = self.provider.mass.players.get(player_id)
        if not player:
            return web.Response(status=404, text="Player not found")

        if not isinstance(player, MSXPlayer):
            return web.Response(status=400, text="Not an MSX player")

        # Resolve track URI to get duration metadata before playback
        track_duration: int = 0
        try:
            media_item = await self.provider.mass.music.get_item_by_uri(uri)
            track_duration = getattr(media_item, "duration", 0) or 0
        except Exception:
            logger.warning("Could not resolve track metadata for URI: %s", uri)

        # Trigger playback through MA queue system
        await self.provider.mass.player_queues.play_media(player_id, uri)

        # Wait for play_media() to set the PlayerMedia on our player
        media = None
        for _ in range(100):  # Poll up to 10 seconds
            media = player.current_media
            if media:
                break
            await asyncio.sleep(0.1)

        if not media:
            return web.Response(status=504, text="Playback setup timeout")

        # Use resolved track duration (always available) over media.duration (may be None)
        duration = track_duration or media.duration or 0

        # Get raw PCM audio via MA's internal streaming API (like sendspin)
        pcm_format = AudioFormat(
            content_type=ContentType.PCM_S16LE,
            sample_rate=44100,
            bit_depth=16,
            channels=2,
        )
        audio_source = self.provider.mass.streams.get_stream(media, pcm_format)

        # Encode PCM → output format via ffmpeg
        output_format_str = player.output_format
        content_type_map = {
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

        # Calculate Content-Length from duration and bitrate for CBR formats
        # MP3: 320 kbps = 40,000 bytes/sec, AAC: 256 kbps = 32,000 bytes/sec
        bitrate_map = {
            "mp3": 40_000,
            "aac": 32_000,
        }
        bytes_per_sec = bitrate_map.get(output_format_str, 0)

        headers: dict[str, str] = {"Content-Type": mime_type}
        if duration and bytes_per_sec:
            headers["Content-Length"] = str(int(duration * bytes_per_sec))
        headers["Accept-Ranges"] = "none"

        logger.debug(
            "MSX audio %s: format=%s, track_duration=%s, media_duration=%s, Content-Length=%s",
            player_id,
            output_format_str,
            track_duration,
            media.duration,
            headers.get("Content-Length", "NOT SET"),
        )

        response = web.StreamResponse(status=200, headers=headers)
        await response.prepare(request)

        try:
            async for chunk in get_ffmpeg_stream(
                audio_input=audio_source,
                input_format=pcm_format,
                output_format=out_format,
            ):
                await response.write(chunk)
        except ConnectionResetError:
            logger.debug("Client disconnected from audio stream %s", player_id)
        except Exception:
            logger.exception("Audio stream error for player %s", player_id)

        return response

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
        """WebSocket for push playback — clients subscribe by player_id."""
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)

        player_id, _ = self._get_player_id_and_device_param(request)
        if player_id not in self._ws_clients:
            self._ws_clients[player_id] = set()
        self._ws_clients[player_id].add(ws)
        logger.debug("WebSocket client connected for player %s", player_id)

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
    ) -> None:
        """Notify subscribed WebSocket clients to start playback with metadata."""
        clients = self._ws_clients.get(player_id, set())
        if not clients:
            logger.debug("No WebSocket clients for player %s, skip broadcast", player_id)
            return
        payload: dict[str, Any] = {"type": "play", "path": f"/stream/{player_id}"}
        if title:
            payload["title"] = title
        if artist:
            payload["artist"] = artist
        if image_url:
            payload["image_url"] = image_url
        if duration is not None:
            payload["duration"] = duration
        msg = json.dumps(payload)
        for ws in list(clients):
            if not ws.closed:
                self.provider.mass.create_task(self._ws_send(ws, msg))

    def broadcast_stop(self, player_id: str) -> None:
        """Notify subscribed WebSocket clients to stop playback."""
        clients = self._ws_clients.get(player_id, set())
        if not clients:
            logger.warning(
                "No WebSocket clients for player %s (have: %s), skip stop broadcast",
                player_id,
                list(self._ws_clients.keys()),
            )
            return
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
        self.provider.on_player_activity(player_id)
        player = self.provider.mass.players.get(player_id)
        if not player:
            return web.Response(status=404, text="Player not found")

        if not isinstance(player, MSXPlayer):
            return web.Response(status=400, text="Not an MSX player")

        media = player.current_media
        if not media:
            return web.Response(status=404, text="No active stream")

        # Resolve duration from queue item (flow_mode leaves media.duration unset)
        duration = media.duration or 0
        if media.source_id and media.queue_item_id:
            queue_item = self.provider.mass.player_queues.get_item(
                media.source_id, media.queue_item_id
            )
            if queue_item:
                if queue_item.media_item:
                    duration = getattr(queue_item.media_item, "duration", None) or duration
                if not duration and queue_item.duration:
                    duration = queue_item.duration

        # Get raw PCM audio via MA's internal streaming API
        pcm_format = AudioFormat(
            content_type=ContentType.PCM_S16LE,
            sample_rate=44100,
            bit_depth=16,
            channels=2,
        )
        audio_source = self.provider.mass.streams.get_stream(media, pcm_format)

        # Encode PCM → output format via ffmpeg
        output_format_str = player.output_format
        content_type_map = {
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

        # Content-Length needed for MSX to play full track (otherwise ~90s cutoff)
        bitrate_map = {"mp3": 40_000, "aac": 32_000}
        bytes_per_sec = bitrate_map.get(output_format_str, 0)
        headers: dict[str, str] = {
            "Content-Type": mime_type,
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        }
        if duration and bytes_per_sec:
            headers["Content-Length"] = str(int(duration * bytes_per_sec))
        headers["Accept-Ranges"] = "none"

        response = web.StreamResponse(status=200, headers=headers)
        await response.prepare(request)

        try:
            async for chunk in get_ffmpeg_stream(
                audio_input=audio_source,
                input_format=pcm_format,
                output_format=out_format,
            ):
                await response.write(chunk)
        except ConnectionResetError:
            logger.debug("Client disconnected from stream %s", player_id)
        except Exception:
            logger.exception("Stream error for player %s", player_id)

        return response

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
                        "image": self._get_image_url(album),
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
                        "image": self._get_image_url(artist),
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
                        "image": self._get_image_url(album),
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
                        "image": self._get_image_url(playlist),
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
                        "image": self._get_image_url(a),
                        "uri": a.uri,
                    }
                    for a in results.artists
                ],
                "albums": [
                    {
                        "item_id": str(a.item_id),
                        "name": a.name,
                        "artist": getattr(a, "artist_str", ""),
                        "image": self._get_image_url(a),
                        "uri": a.uri,
                    }
                    for a in results.albums
                ],
                "tracks": [self._format_track(t) for t in results.tracks],
                "playlists": [
                    {
                        "item_id": str(p.item_id),
                        "name": p.name,
                        "image": self._get_image_url(p),
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
        if not device_param:
            return url
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}{device_param}"

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
            "image": self._get_image_url(track),
            "uri": track.uri,
        }

    def _format_msx_track(
        self, track: Any, prefix: str, player_id: str, device_param: str = ""
    ) -> dict[str, Any]:
        """Format a track as an MSX content item with playback action."""
        duration = getattr(track, "duration", 0) or 0
        duration_str = f"{duration // 60}:{duration % 60:02d}" if duration else ""
        label = getattr(track, "artist_str", "")
        if duration_str:
            label = f"{label} · {duration_str}" if label else duration_str
        image_url = self._get_image_url(track)
        audio_url = f"{prefix}/msx/audio/{player_id}?uri={quote(track.uri, safe='')}"
        if device_param:
            audio_url = f"{audio_url}&{device_param}"
        return {
            "title": track.name,
            "label": label,
            "playerLabel": track.name,
            "image": image_url,
            "background": image_url,
            "action": f"audio:{audio_url}",
        }

    def _get_image_url(self, item: Any) -> str | None:
        """Get an image URL for a media item."""
        if hasattr(item, "image") and item.image:
            return self.provider.mass.metadata.get_image_url(item.image)
        return None

    async def _get_album_image_fallback(self, album: Any) -> str | None:
        """Get album image from its first track (albums often lack metadata images)."""
        try:
            tracks = await self.provider.mass.music.albums.tracks(album.item_id, "library")
            for track in tracks:
                if hasattr(track, "image") and track.image:
                    return self.provider.mass.metadata.get_image_url(track.image)
        except Exception as exc:
            logger.debug("Album image fallback failed: %s", exc)
        return None
