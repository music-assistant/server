"""Embedded HTTP server for the Web Kiosk Provider."""

from __future__ import annotations

import json
import logging
import secrets
from html import escape as html_escape
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import quote, urlsplit, urlunsplit

from aiohttp import ClientError, WSMsgType, web
from music_assistant_models.enums import PlaybackState

from music_assistant.constants import SENDSPIN_SERVER_PORT

from .constants import (
    CONF_SHOW_STOP_NOTIFICATION,
    DEFAULT_SHOW_STOP_NOTIFICATION,
    PLAYER_ID_SANITIZE_RE,
    WEB_KIOSK_PLAYER_ID_PREFIX,
    normalize_sendspin_client_id,
)
from .party import PartyAdapter
from .player import WebKioskPlayer, media_timeline

if TYPE_CHECKING:
    from .provider import WebKioskProvider

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

_KNOWN_EXTENSIONS = (".mp3", ".json", ".flac", ".aac")


def _strip_known_extension(value: str) -> str:
    """Strip only known audio/data extensions from a value."""
    for ext in _KNOWN_EXTENSIONS:
        if value.endswith(ext):
            return value[: -len(ext)]
    return value


def _is_audio_path(path: str) -> bool:
    """Check whether the path is the audio route."""
    return path.startswith("/stream/")


def rewrite_stream_host(request: web.Request, url: str) -> str:
    """Point a stream URL at the host the client already uses to reach MA."""
    client_host = request.url.host
    if not client_host:
        return url
    parts = urlsplit(url)
    if ":" in client_host:
        client_host = f"[{client_host}]"
    netloc = f"{client_host}:{parts.port}" if parts.port else client_host
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


class WebKioskHTTPServer:
    """HTTP server that serves the kiosk SPA, push WebSocket, and stream redirect."""

    def __init__(self, provider: WebKioskProvider, port: int) -> None:
        """Initialize the HTTP server."""
        self.provider = provider
        self.port = port
        self.app = web.Application(middlewares=[self._cors_middleware])
        self._runner: web.AppRunner | None = None
        self._ws_clients: dict[str, set[web.WebSocketResponse]] = {}
        self._client_prefixes: dict[str, str] = {}
        self.party = PartyAdapter(provider)
        self._setup_routes()

    async def start(self) -> None:
        """Start the HTTP server."""
        self._runner = web.AppRunner(self.app)
        await self._runner.setup()
        # 0.0.0.0 is required: kiosk devices on LAN must reach this server by host IP.
        site = web.TCPSite(
            self._runner,
            "0.0.0.0",
            self.port,
            reuse_address=True,
            reuse_port=True,
        )
        await site.start()
        logger.info("Web Kiosk HTTP server started on port %s", self.port)

    async def stop(self) -> None:
        """Stop the HTTP server."""
        for clients in list(self._ws_clients.values()):
            for ws in list(clients):
                if not ws.closed:
                    await ws.close()
        self._ws_clients.clear()
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        logger.info("Web Kiosk HTTP server stopped")

    def broadcast_play(
        self,
        player_id: str,
        *,
        title: str | None = None,
        artist: str | None = None,
        image_url: str | None = None,
        duration: float | None = None,
        start: float = 0,
        wave_seq: int | None = None,
    ) -> None:
        """Notify subscribed WebSocket clients to start playback with metadata."""
        clients = self._ws_clients.get(player_id, set())
        if not clients:
            logger.debug("broadcast_play: no WebSocket clients for player_id=%s", player_id)
            return
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
            payload["image_url"] = image_url
        if duration is not None:
            payload["duration"] = duration
        # Where audio.currentTime 0 sits in the song. Non-zero after a seek,
        # because the stream Music Assistant serves then starts at that point.
        payload["start"] = start
        if wave_seq is not None:
            payload["wave_seq"] = wave_seq
        msg = json.dumps(payload)
        for ws in list(clients):
            if not ws.closed:
                self.provider.mass.create_task(self._ws_send(ws, msg, player_id))

    def broadcast_wave(self, player_id: str, seq: int, bins: list[float] | None) -> None:
        """Send the stored energy curve for the track that just started."""
        clients = self._ws_clients.get(player_id, set())
        if not clients:
            return
        msg = json.dumps({"type": "wave", "seq": seq, "bins": bins or []})
        for ws in list(clients):
            if not ws.closed:
                self.provider.mass.create_task(self._ws_send(ws, msg, player_id))

    def broadcast_lyrics(self, player_id: str, seq: int, lines: list[dict[str, Any]]) -> None:
        """Send lyric lines for the track that just started."""
        clients = self._ws_clients.get(player_id, set())
        if not clients:
            return
        msg = json.dumps({"type": "lyrics", "seq": seq, "lines": lines})
        for ws in list(clients):
            if not ws.closed:
                self.provider.mass.create_task(self._ws_send(ws, msg, player_id))

    def broadcast_sendspin(self, player_id: str, url: str) -> None:
        """Notify WebSocket clients to open the kiosk in Sendspin mode."""
        clients = self._ws_clients.get(player_id, set())
        if not clients:
            return
        msg = json.dumps({"type": "sendspin", "url": url, "player_id": player_id})
        for ws in list(clients):
            if not ws.closed:
                self.provider.mass.create_task(self._ws_send(ws, msg, player_id))

    def broadcast_pause(self, player_id: str) -> None:
        """Notify subscribed WebSocket clients to pause playback."""
        clients = self._ws_clients.get(player_id, set())
        if not clients:
            return
        msg = json.dumps({"type": "pause"})
        for ws in list(clients):
            if not ws.closed:
                self.provider.mass.create_task(self._ws_send(ws, msg, player_id))

    def broadcast_resume(self, player_id: str) -> None:
        """Notify subscribed WebSocket clients to resume playback."""
        clients = self._ws_clients.get(player_id, set())
        if not clients:
            return
        msg = json.dumps({"type": "resume"})
        for ws in list(clients):
            if not ws.closed:
                self.provider.mass.create_task(self._ws_send(ws, msg, player_id))

    def broadcast_stop(self, player_id: str) -> None:
        """Notify subscribed WebSocket clients to stop playback."""
        clients = self._ws_clients.get(player_id, set())
        if not clients:
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
                self.provider.mass.create_task(self._ws_send(ws, msg, player_id))

    def broadcast_seek(self, player_id: str, position_seconds: int) -> None:
        """Notify subscribed WebSocket clients to seek to a position."""
        clients = self._ws_clients.get(player_id, set())
        if not clients:
            return
        msg = json.dumps({"type": "seek", "position": position_seconds})
        for ws in list(clients):
            if not ws.closed:
                self.provider.mass.create_task(self._ws_send(ws, msg, player_id))

    def broadcast_volume(self, player_id: str, volume_level: int) -> None:
        """Notify subscribed WebSocket clients of a volume change."""
        clients = self._ws_clients.get(player_id, set())
        if not clients:
            return
        msg = json.dumps({"type": "volume", "level": volume_level})
        for ws in list(clients):
            if not ws.closed:
                self.provider.mass.create_task(self._ws_send(ws, msg, player_id))

    def _setup_routes(self) -> None:
        """Register all HTTP routes."""
        self.app.router.add_get("/", self._handle_root)
        self.app.router.add_get("/web", self._handle_web_app)
        self.app.router.add_static("/web/", STATIC_DIR / "web")
        self.app.router.add_get("/health", self._handle_health)
        self.app.router.add_get("/ws", self._handle_ws)
        self.app.router.add_get("/stream/{player_id}", self._handle_stream)
        self.app.router.add_get("/stream/{player_id}.mp3", self._handle_stream)
        self.app.router.add_get("/api/party", self._handle_party_info)
        self.app.router.add_get("/api/party/qr.svg", self._handle_party_qr)
        self.app.router.add_get("/api/party/qr.png", self._handle_party_qr)
        # Same-origin stand-in for the MA webserver. The SPA is on this port and
        # the MA API is on another, so a browser will not call MA directly.
        self.app.router.add_post("/api", self._handle_ma_api)
        self.app.router.add_get("/imageproxy/{image_id:.*}", self._handle_imageproxy)

    @web.middleware
    async def _cors_middleware(self, request: web.Request, handler: Any) -> web.StreamResponse:
        """
        Add CORS headers to all responses.

        Wildcard CORS is intentional: this server runs on LAN. The kiosk SPA
        (/web) is served from the same origin, so playback-control calls are
        same-origin. The audio route gets no header at all: a media element
        plays a cross-origin source without CORS, and withholding it keeps a
        cross-origin fetch() from reading the audio.
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
        if request.path == "/web" or request.path.startswith("/web/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    async def _handle_root(self, request: web.Request) -> web.Response:
        """Serve the status dashboard with a kiosk URL builder."""
        prefix = self._get_prefix(request)
        players = (
            "".join(
                f"<li>{html_escape(p.display_name)} — {html_escape(p.playback_state.value)}</li>"
                for p in self.provider.players
            )
            or "<li>No players registered</li>"
        )
        hostname = request.url.host or request.host.split(":")[0]
        host_addr = f"[{hostname}]" if ":" in hostname else hostname
        sendspin_url = f"http://{host_addr}:{SENDSPIN_SERVER_PORT}"
        html = f"""<!DOCTYPE html>
<html>
<head><title>Web Kiosk</title>
<style>
body {{ font-family: system-ui, sans-serif; max-width: 760px; margin: 40px auto; padding: 20px; }}
.info {{ background: #1a1a20; color: #e8e8ee; padding: 16px; border-radius: 10px; margin: 12px 0; }}
code, input[readonly] {{ font-family: ui-monospace, monospace; word-break: break-all; }}
.row {{ display: flex; align-items: center; gap: 12px; margin: 8px 0; }}
label {{ cursor: pointer; margin-right: 14px; }}
input[readonly] {{ flex: 1; padding: 8px; border-radius: 6px; border: 1px solid #444; background: #101014; color: #e8e8ee; }}
input[type=text], input[type=password] {{ padding: 8px; border-radius: 6px; border: 1px solid #444; background: #101014; color: #e8e8ee; width: 100%; }}
.btn {{ padding: 8px 14px; border-radius: 6px; border: 1px solid #4f8cff; background: #4f8cff; color: white; cursor: pointer; }}
a {{ color: #4f8cff; }}
small {{ color: #9a9aa6; display: block; margin-top: 4px; }}
</style>
</head>
<body>
<h1>Web Kiosk</h1>

<div class="info">
<h3>Kiosk URL Builder</h3>
<div class="row">
<label><input type="radio" name="mode" value="html5" checked> HTML5</label>
<label><input type="radio" name="mode" value="sendspin"> Sendspin</label>
<label><input type="checkbox" id="controls" checked> Controls</label>
<label><input type="checkbox" id="party" checked> Party QR</label>
<label><input type="checkbox" id="viz" checked> Visualizer</label>
<label><input type="checkbox" id="lyrics" checked> Lyrics</label>
</div>
<div class="row">
<input type="text" id="ma_url" placeholder="MA server URL (e.g. http://{host_addr}:8095)">
</div>
<div class="row">
<input type="password" id="token" placeholder="MA auth token (optional)">
</div>
<div class="row">
<input type="text" id="device_id" placeholder="Device id (optional, stored in the browser)">
</div>
<div class="row">
<input readonly id="url">
<button class="btn" id="copy">Copy</button>
<button class="btn" id="open" style="background:#2f6f3f;border-color:#2f6f3f">Open</button>
</div>
<small>Choose music in Music Assistant. This page only plays it. Transport and queue commands need an auth token. The party code is shown when guest access is on. The Sendspin server is assumed at {html_escape(sendspin_url)}.</small>
</div>

<div class="info">
<h3>Players</h3>
<ul>{players}</ul>
</div>

<script>
(function () {{
    var url = document.getElementById('url');
    var copy = document.getElementById('copy');
    var open = document.getElementById('open');
    var base = {json.dumps(prefix)};

    function build() {{
        var mode = document.querySelector('input[name="mode"]:checked').value;
        var params = ['kiosk=1'];
        if (mode === 'sendspin') params.push('sendspin=1');
        var toggles = {{controls: 'controls', party: 'party', viz: 'viz', lyrics: 'lyrics'}};
        for (var key in toggles) {{
            if (!document.getElementById(key).checked) params.push(toggles[key] + '=0');
        }}
        var maUrl = document.getElementById('ma_url').value.trim();
        if (maUrl) params.push('ma_url=' + encodeURIComponent(maUrl));
        var token = document.getElementById('token').value.trim();
        if (token) params.push('token=' + encodeURIComponent(token));
        var device = document.getElementById('device_id').value.trim();
        if (device) params.push('device_id=' + encodeURIComponent(device));
        return base + '/web?' + params.join('&');
    }}

    function refresh() {{ url.value = build(); }}
    document.addEventListener('input', refresh);
    document.addEventListener('change', refresh);
    copy.onclick = function () {{
        url.select();
        navigator.clipboard.writeText(url.value);
    }};
    open.onclick = function () {{ window.open(url.value, '_blank'); }};
    refresh();
}})();
</script>
</body>
</html>"""
        return web.Response(text=html, content_type="text/html")

    async def _handle_web_app(self, _request: web.Request) -> web.Response:
        """Serve the web kiosk SPA."""
        response = cast("web.Response", web.FileResponse(STATIC_DIR / "web" / "index.html"))
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return response

    async def _handle_health(self, _request: web.Request) -> web.Response:
        """Health check endpoint."""
        return web.json_response(
            {
                "status": "ok",
                "provider": "web_kiosk",
                "players": len(self.provider.players),
            }
        )

    async def _handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        """
        WebSocket for push playback — clients subscribe by player_id.

        Uses the same player_id derivation (device_id or IP) as the stream
        endpoint so broadcast_stop reaches the correct client. Registers the
        player in MA on connect so the player appears when the kiosk starts.
        """
        if self._reject_cross_site(request):
            raise web.HTTPForbidden
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)

        player_id, _, player = await self._ensure_player_for_request(request)
        # Count this socket before any await. A reload's old socket can close
        # during that await, and it must still see a live client.
        if player_id not in self._ws_clients:
            self._ws_clients[player_id] = set()
        self._ws_clients[player_id].add(ws)
        self.provider.cancel_player_close(player_id)
        announced = normalize_sendspin_client_id(request.query.get("sendspin_client_id"))
        if player is not None and announced:
            await self.provider.note_sendspin_client_id(player, announced)
        logger.info(
            "WebSocket connected: player_id=%s, clients_for_player=%d",
            player_id,
            len(self._ws_clients[player_id]),
        )
        if player and isinstance(player, WebKioskPlayer):
            player.on_ws_connected()
        # Tell the kiosk its server-derived player_id so it can target MA
        # JSON-RPC playback commands before the first "play" push arrives.
        await self._ws_send(ws, json.dumps({"type": "welcome", "player_id": player_id}))
        # A reconnecting/reloaded kiosk needs the current play state, otherwise it
        # stays silent until the next playback command.
        await self._send_snapshot(ws, player)

        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    self._handle_ws_message(player_id, msg.data)
        finally:
            self._ws_clients.get(player_id, set()).discard(ws)
            if not self._ws_clients.get(player_id):
                self._ws_clients.pop(player_id, None)
                offline_player = self.provider.mass.players.get_player(player_id)
                if offline_player and isinstance(offline_player, WebKioskPlayer):
                    offline_player.on_ws_disconnected()
                # Gone for good only if nothing reconnects. A reload does.
                self.provider.schedule_player_close(player_id)
            logger.debug("WebSocket client disconnected for player %s", player_id)

        return ws

    def client_count(self, player_id: str) -> int:
        """Return how many browser sockets are open for this player."""
        return len(self._ws_clients.get(player_id, ()))

    async def _send_snapshot(
        self, ws: web.WebSocketResponse, player: WebKioskPlayer | None
    ) -> None:
        """Push the current play state to a freshly connected kiosk."""
        if player is None or player.current_media is None:
            return
        media = player.current_media
        play_path = (
            f"/stream/{player.player_id}?token={self.provider.get_stream_token(player.player_id)}"
        )
        song_length, stream_start = media_timeline(media)
        wave_seq = self.provider.next_wave_seq(player.player_id)
        payload: dict[str, Any] = {
            "type": "play",
            "path": play_path,
            "player_id": player.player_id,
            "title": media.title,
            "artist": media.artist,
            "image_url": media.image_url,
            "duration": song_length,
            "start": stream_start,
            "wave_seq": wave_seq,
        }
        await self._ws_send(ws, json.dumps(payload))
        self.provider.spawn_wave(player.player_id, wave_seq)
        self.provider.spawn_lyrics(player.player_id, wave_seq)
        # Position inside the served audio, including while paused. The song
        # clock on screen is start + this value. A paused reconnect used to
        # skip it and open the track at 0:00.
        if player.elapsed_time is not None:
            await self._ws_send(
                ws, json.dumps({"type": "seek", "position": int(player.elapsed_time)})
            )
        if player.playback_state == PlaybackState.PAUSED:
            await self._ws_send(ws, json.dumps({"type": "pause"}))
        # The slider starts at 100. Send the real level so a reconnect does
        # not jump the volume on the first drag.
        if player.volume_level is not None:
            await self._ws_send(
                ws, json.dumps({"type": "volume", "level": int(player.volume_level)})
            )

    async def _handle_stream(self, request: web.Request) -> web.StreamResponse:
        """Redirect the kiosk audio element to the MA Streamserver URL."""
        player_id = _strip_known_extension(request.match_info["player_id"])

        player = self._get_kiosk_player(player_id)
        if player is None:
            return web.Response(status=404, text="Player not found")
        if rejected := self._reject_invalid_stream_token(request, player_id):
            return rejected
        self.provider.on_player_activity(player_id)

        media = player.current_media
        if not media:
            return web.Response(status=404, text="No active stream")

        stream_url = await self.provider.get_ma_stream_url(player_id, media)
        if not stream_url:
            return web.Response(status=502, text="Unable to resolve stream URL")
        raise web.HTTPFound(location=rewrite_stream_host(request, stream_url))

    async def _handle_party_info(self, request: web.Request) -> web.Response:
        """Return the active party caption. The join URL stays inside the QR image."""
        party = await self.party.get_active_party()
        if party is None:
            return web.json_response({"active": False})
        return web.json_response(
            {
                "active": True,
                "name": party.name,
                "qr_text": party.qr_text,
                "version": party.qr_version,
            }
        )

    async def _handle_party_qr(self, request: web.Request) -> web.Response:
        """Serve the guest join URL as a QR code image (SVG or PNG by route)."""
        return await self.party.handle_qr(request)

    async def _handle_ma_api(self, request: web.Request) -> web.Response:
        """Forward a JSON-RPC call to the Music Assistant webserver on this host."""
        base = self._ma_internal_base()
        if not base:
            return web.Response(status=503, text="Music Assistant API is unavailable")
        return await self._forward_to_mass(request, "POST", f"{base}/api", await request.read())

    async def _handle_imageproxy(self, request: web.Request) -> web.Response:
        """Forward a cover-art request to the Music Assistant image proxy."""
        base = self._ma_internal_base()
        if not base:
            return web.Response(status=503, text="Music Assistant API is unavailable")
        path = request.path
        if not path.startswith("/imageproxy/") or ".." in path:
            return web.Response(status=400, text="Invalid image path")
        url = f"{base}{path}"
        if request.query_string:
            url = f"{url}?{request.query_string}"
        return await self._forward_to_mass(request, "GET", url, None)

    def _ma_internal_base(self) -> str | None:
        """Return a URL this process can use to reach the MA webserver."""
        webserver = getattr(self.provider.mass, "webserver", None)
        if webserver is None:
            return None
        for attr in ("internal_base_url", "base_url"):
            value = getattr(webserver, attr, None)
            if isinstance(value, str) and value.startswith("http"):
                return value.rstrip("/")
        return None

    async def _forward_to_mass(
        self, request: web.Request, method: str, url: str, body: bytes | None
    ) -> web.Response:
        """Perform one request against the local MA webserver and return its body."""
        headers: dict[str, str] = {}
        if auth := request.headers.get("Authorization"):
            headers["Authorization"] = auth
        if method == "POST":
            headers["Content-Type"] = request.headers.get("Content-Type", "application/json")
        try:
            async with self.provider.mass.http_session.request(
                method, url, data=body, headers=headers, allow_redirects=False
            ) as resp:
                payload = await resp.read()
                response_headers: dict[str, str] = {}
                if content_type := resp.headers.get("Content-Type"):
                    response_headers["Content-Type"] = content_type
                if location := resp.headers.get("Location"):
                    response_headers["Location"] = location
                return web.Response(status=resp.status, body=payload, headers=response_headers)
        except ClientError, TimeoutError, OSError:
            logger.exception("Music Assistant proxy failed for %s", url.split("?", 1)[0])
            return web.Response(status=502, text="Music Assistant is unreachable")

    async def _ws_send(
        self, ws: web.WebSocketResponse, text: str, player_id: str | None = None
    ) -> None:
        """Send text to WebSocket; on failure warn and remove the stale client."""
        try:
            await ws.send_str(text)
        except (ConnectionError, RuntimeError) as exc:
            logger.warning("WebSocket send failed (player=%s): %s", player_id, exc)
            if player_id:
                self._ws_clients.get(player_id, set()).discard(ws)

    def _handle_ws_message(self, player_id: str, data: str) -> None:
        """Process an inbound WebSocket message from the kiosk."""
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
                if player and isinstance(player, WebKioskPlayer):
                    player.update_position(float(position))
                    self.provider.on_player_activity(player_id)
        elif msg_type == "seek":
            position = msg.get("position")
            player = self.provider.mass.players.get_player(player_id)
            if (
                player
                and isinstance(player, WebKioskPlayer)
                and position is not None
                and isinstance(position, (int, float))
            ):
                player.note_seek(float(position))
                self.provider.on_player_activity(player_id)
        elif msg_type == "ended":
            self.provider.track_ended(player_id)
            self.provider.on_player_activity(player_id)
        else:
            logger.debug("Unknown WS message type from %s: %s", player_id, msg_type)

    def _reject_invalid_stream_token(
        self, request: web.Request, player_id: str
    ) -> web.Response | None:
        """
        Reject an audio request that does not carry the player's own stream token.

        A kiosk cannot send an auth header, so the token travels in the URL the
        provider itself generated. This stops a request that was never handed out.
        """
        expected = self.provider.get_stream_token(player_id)
        if not secrets.compare_digest(request.query.get("token", ""), expected):
            return web.Response(status=403, text="Invalid or missing stream token")
        return None

    @staticmethod
    def _reject_cross_site(request: web.Request) -> web.Response | None:
        """
        Reject browser cross-site requests to state-changing endpoints (CSRF guard).

        Legitimate callers are same-origin (kiosk SPA) or non-browser clients that
        omit the header entirely — both pass.
        """
        if request.headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
            return web.json_response({"error": "Cross-site request rejected"}, status=403)
        return None

    def _get_kiosk_player(self, player_id: str) -> WebKioskPlayer | None:
        """Return the WebKioskPlayer for player_id if it belongs to this provider, else None."""
        player = self.provider.mass.players.get_player(player_id, raise_unavailable=False)
        if isinstance(player, WebKioskPlayer) and player.provider == self.provider:
            return player
        return None

    def _get_prefix(self, request: web.Request) -> str:
        """
        Build URL prefix for content, using our known port.

        Uses aiohttp's parsed URL host (IPv6-safe, no port) and substitutes
        self.port. Note: host is still derived from the Host header; a crafted
        header can influence the returned host, but the server binds to 0.0.0.0
        so there is no single canonical IP to validate against.
        """
        host: str = request.url.host or request.host.split(":")[0]
        host_addr = f"[{host}]" if ":" in host else host
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
            player_id = f"{WEB_KIOSK_PLAYER_ID_PREFIX}{sanitized}"
            param = f"device_id={quote(device_id, safe='')}"
        else:
            ip = remote_ip if remote_ip != "unknown" else "0_0_0_0"
            sanitized = PLAYER_ID_SANITIZE_RE.sub("_", ip.replace(".", "_")).strip("_") or "ip"
            player_id = f"{WEB_KIOSK_PLAYER_ID_PREFIX}{sanitized}"
            param = ""
        return player_id, param

    async def _ensure_player_for_request(
        self, request: web.Request
    ) -> tuple[str, str, WebKioskPlayer | None]:
        """
        Get or register player for this request.

        Returns (player_id, device_param, player).
        Player may be None if registration failed.
        """
        player_id, device_param = self._get_player_id_and_device_param(request)
        # Remember how this client reaches us — WS pushes have no request context
        self._client_prefixes[player_id] = self._get_prefix(request)
        remote_ip = request.remote
        display_name = self.provider.player_display_name(player_id, remote_ip=remote_ip)
        player = await self.provider.get_or_register_player(
            player_id, display_name=display_name, ip_address=remote_ip
        )
        return player_id, device_param, player
