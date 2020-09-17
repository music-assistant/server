"""The web module handles serving the frontend and the rest/websocket api's."""
import asyncio
import datetime
import functools
import inspect
import ipaddress
import json
import logging
import os
import ssl

import aiohttp
import aiohttp_cors
import jwt
from aiohttp import web
from aiohttp_jwt import JWTMiddleware, login_required
from music_assistant.constants import (
    CONF_KEY_BASE,
    CONF_KEY_PLAYERSETTINGS,
    CONF_KEY_PROVIDERS,
)
from music_assistant.constants import __version__ as MASS_VERSION
from music_assistant.models.media_types import MediaType
from music_assistant.models.player_queue import QueueOption
from music_assistant.utils import get_external_ip, get_hostname, get_ip, json_serializer

LOGGER = logging.getLogger("mass")


class ClassRouteTableDef(web.RouteTableDef):
    """Helper class to add class based routing tables."""

    def __repr__(self) -> str:
        """Print the class contents."""
        return "<ClassRouteTableDef count={}>".format(len(self._items))

    def route(self, method: str, path: str, **kwargs):
        """Return the route."""
        # pylint: disable=missing-function-docstring
        def inner(handler):
            handler.route_info = (method, path, kwargs)
            return handler

        return inner

    def add_class_routes(self, instance) -> None:
        """Add class routes."""
        # pylint: disable=missing-function-docstring
        def predicate(member) -> bool:
            return all(
                (inspect.iscoroutinefunction(member), hasattr(member, "route_info"))
            )

        for _, handler in inspect.getmembers(instance, predicate):
            method, path, kwargs = handler.route_info
            super().route(method, path, **kwargs)(handler)


# pylint: disable=invalid-name
routes = ClassRouteTableDef()
# pylint: enable=invalid-name


def require_local_subnet(func):
    """Return decorator to specify web method as available locally only."""

    @functools.wraps(func)
    async def wrapped(*args, **kwargs):
        request = args[-1]

        if isinstance(request, web.View):
            request = request.request

        if not isinstance(request, web.BaseRequest):  # pragma: no cover
            raise RuntimeError(
                "Incorrect usage of decorator." "Expect web.BaseRequest as an argument"
            )

        if not ipaddress.ip_address(request.remote).is_private:
            raise web.HTTPUnauthorized(reason="Not remote available")

        return await func(*args, **kwargs)

    return wrapped


class Web:
    """Webserver and json/websocket api."""

    def __init__(self, mass):
        """Initialize class."""
        self.mass = mass
        # load/create/update config
        self._local_ip = get_ip()
        self.config = mass.config.base["web"]
        self.runner = None

        enable_ssl = self.config["ssl_certificate"] and self.config["ssl_key"]
        if self.config["ssl_certificate"] and not os.path.isfile(
            self.config["ssl_certificate"]
        ):
            enable_ssl = False
            LOGGER.warning(
                "SSL certificate file not found: %s", self.config["ssl_certificate"]
            )
        if self.config["ssl_key"] and not os.path.isfile(self.config["ssl_key"]):
            enable_ssl = False
            LOGGER.warning(
                "SSL certificate key file not found: %s", self.config["ssl_key"]
            )
        if not self.config.get("external_url"):
            enable_ssl = False
        self._enable_ssl = enable_ssl
        self._jwt_shared_secret = f"mass_{self._local_ip}_{self.http_port}"

    async def async_setup(self):
        """Perform async setup."""
        routes.add_class_routes(self)
        jwt_middleware = JWTMiddleware(
            self._jwt_shared_secret, request_property="user", credentials_required=False
        )
        app = web.Application(middlewares=[jwt_middleware])
        # add routes
        app.add_routes(routes)
        app.add_routes(
            [
                web.get(
                    "/stream/{player_id}",
                    self.mass.http_streamer.async_stream,
                    allow_head=False,
                ),
                web.get(
                    "/stream/{player_id}/{queue_item_id}",
                    self.mass.http_streamer.async_stream,
                    allow_head=False,
                ),
                web.get(
                    "/stream_media/{media_type}/{provider}/{item_id}",
                    self.mass.http_streamer.async_stream_media_item,
                    allow_head=False,
                ),
                web.get("/", self.async_index),
                web.post("/login", self.async_login),
                web.get("/jsonrpc.js", self.async_json_rpc),
                web.post("/jsonrpc.js", self.async_json_rpc),
                web.get("/ws", self.async_websocket_handler),
                web.get("/info", self.async_info),
            ]
        )
        webdir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web/")
        if os.path.isdir(webdir):
            app.router.add_static("/", webdir, append_version=True)
        else:
            # The (minified) build of the frontend(app) is included in the pypi releases
            LOGGER.warning("Loaded without frontend support.")

        # Add CORS support to all routes
        cors = aiohttp_cors.setup(
            app,
            defaults={
                "*": aiohttp_cors.ResourceOptions(
                    allow_credentials=True,
                    expose_headers="*",
                    allow_headers="*",
                    allow_methods=["POST", "PUT", "DELETE", "GET"],
                )
            },
        )
        for route in list(app.router.routes()):
            cors.add(route)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        http_site = web.TCPSite(self.runner, "0.0.0.0", self.http_port)
        await http_site.start()
        LOGGER.info("Started HTTP webserver on port %s", self.http_port)
        if self._enable_ssl:
            ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
            ssl_context.load_cert_chain(
                self.config["ssl_certificate"], self.config["ssl_key"]
            )
            https_site = web.TCPSite(
                self.runner, "0.0.0.0", self.https_port, ssl_context=ssl_context
            )
            await https_site.start()
            LOGGER.info(
                "Started HTTPS webserver on port %s - serving at FQDN %s",
                self.https_port,
                self.external_url,
            )

    @property
    def internal_ip(self):
        """Return the local IP address for this Music Assistant instance."""
        return self._local_ip

    @property
    def http_port(self):
        """Return the HTTP port for this Music Assistant instance."""
        return self.config.get("http_port", 8095)

    @property
    def https_port(self):
        """Return the HTTPS port for this Music Assistant instance."""
        return self.config.get("https_port", 8096)

    @property
    def internal_url(self):
        """Return the internal URL for this Music Assistant instance."""
        return f"http://{self._local_ip}:{self.http_port}"

    @property
    def external_url(self):
        """Return the internal URL for this Music Assistant instance."""
        if self._enable_ssl and self.config.get("external_url"):
            return self.config["external_url"]
        return f"http://{get_external_ip()}:{self.http_port}"

    @property
    def discovery_info(self):
        """Return (discovery) info about this instance."""
        return {
            "id": f"{get_hostname()}",
            "external_url": self.external_url,
            "internal_url": self.internal_url,
            "host": self.internal_ip,
            "http_port": self.http_port,
            "https_port": self.https_port,
            "ssl_enabled": self._enable_ssl,
            "version": MASS_VERSION,
        }

    @routes.post("/api/login")
    async def async_login(self, request):
        """Handle the retrieval of a JWT token."""
        form = await request.json()
        username = form.get("username")
        password = form.get("password")
        token_info = await self.__async_get_token(username, password)
        if token_info:
            return web.json_response(token_info, dumps=json_serializer)
        return web.HTTPUnauthorized(body="Invalid username and/or password provided!")

    @routes.get("/api/info")
    async def async_info(self, request):
        # pylint: disable=unused-argument
        """Return (discovery) info about this instance."""
        return web.json_response(self.discovery_info, dumps=json_serializer)

    async def async_index(self, request):
        """Get the index page, redirect if we do not have a web directory."""
        # pylint: disable=unused-argument
        webdir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web/")
        if not os.path.isdir(webdir):
            raise web.HTTPFound("https://music-assistant.github.io/app")
        return web.FileResponse(os.path.join(webdir, "index.html"))

    @login_required
    @routes.get("/api/library/artists")
    async def async_library_artists(self, request):
        """Get all library artists."""
        orderby = request.query.get("orderby", "name")
        provider_filter = request.rel_url.query.get("provider")
        iterator = self.mass.music_manager.async_get_library_artists(
            orderby=orderby, provider_filter=provider_filter
        )
        return await self.__async_stream_json(request, iterator)

    @login_required
    @routes.get("/api/library/albums")
    async def async_library_albums(self, request):
        """Get all library albums."""
        orderby = request.query.get("orderby", "name")
        provider_filter = request.rel_url.query.get("provider")
        iterator = self.mass.music_manager.async_get_library_albums(
            orderby=orderby, provider_filter=provider_filter
        )
        return await self.__async_stream_json(request, iterator)

    @login_required
    @routes.get("/api/library/tracks")
    async def async_library_tracks(self, request):
        """Get all library tracks."""
        orderby = request.query.get("orderby", "name")
        provider_filter = request.rel_url.query.get("provider")
        iterator = self.mass.music_manager.async_get_library_tracks(
            orderby=orderby, provider_filter=provider_filter
        )
        return await self.__async_stream_json(request, iterator)

    @login_required
    @routes.get("/api/library/radios")
    async def async_library_radios(self, request):
        """Get all library radios."""
        orderby = request.query.get("orderby", "name")
        provider_filter = request.rel_url.query.get("provider")
        iterator = self.mass.music_manager.async_get_library_radios(
            orderby=orderby, provider_filter=provider_filter
        )
        return await self.__async_stream_json(request, iterator)

    @login_required
    @routes.get("/api/library/playlists")
    async def async_library_playlists(self, request):
        """Get all library playlists."""
        orderby = request.query.get("orderby", "name")
        provider_filter = request.rel_url.query.get("provider")
        iterator = self.mass.music_manager.async_get_library_playlists(
            orderby=orderby, provider_filter=provider_filter
        )
        return await self.__async_stream_json(request, iterator)

    @login_required
    @routes.put("/api/library")
    async def async_library_add(self, request):
        """Add item(s) to the library."""
        body = await request.json()
        media_items = await self.__async_media_items_from_body(body)
        result = await self.mass.music_manager.async_library_add(media_items)
        return web.json_response(result, dumps=json_serializer)

    @login_required
    @routes.delete("/api/library")
    async def async_library_remove(self, request):
        """Remove item(s) from the library."""
        body = await request.json()
        media_items = await self.__async_media_items_from_body(body)
        result = await self.mass.music_manager.async_library_remove(media_items)
        return web.json_response(result, dumps=json_serializer)

    @login_required
    @routes.get("/api/artists/{item_id}")
    async def async_artist(self, request):
        """Get full artist details."""
        item_id = request.match_info.get("item_id")
        provider = request.rel_url.query.get("provider")
        lazy = request.rel_url.query.get("lazy", "true") != "false"
        if item_id is None or provider is None:
            return web.Response(text="invalid item or provider", status=501)
        result = await self.mass.music_manager.async_get_artist(
            item_id, provider, lazy=lazy
        )
        return web.json_response(result, dumps=json_serializer)

    @login_required
    @routes.get("/api/albums/{item_id}")
    async def async_album(self, request):
        """Get full album details."""
        item_id = request.match_info.get("item_id")
        provider = request.rel_url.query.get("provider")
        lazy = request.rel_url.query.get("lazy", "true") != "false"
        if item_id is None or provider is None:
            return web.Response(text="invalid item or provider", status=501)
        result = await self.mass.music_manager.async_get_album(
            item_id, provider, lazy=lazy
        )
        return web.json_response(result, dumps=json_serializer)

    @login_required
    @routes.get("/api/tracks/{item_id}")
    async def async_track(self, request):
        """Get full track details."""
        item_id = request.match_info.get("item_id")
        provider = request.rel_url.query.get("provider")
        lazy = request.rel_url.query.get("lazy", "true") != "false"
        if item_id is None or provider is None:
            return web.Response(text="invalid item or provider", status=501)
        result = await self.mass.music_manager.async_get_track(
            item_id, provider, lazy=lazy
        )
        return web.json_response(result, dumps=json_serializer)

    @login_required
    @routes.get("/api/playlists/{item_id}")
    async def async_playlist(self, request):
        """Get full playlist details."""
        item_id = request.match_info.get("item_id")
        provider = request.rel_url.query.get("provider")
        if item_id is None or provider is None:
            return web.Response(text="invalid item or provider", status=501)
        result = await self.mass.music_manager.async_get_playlist(item_id, provider)
        return web.json_response(result, dumps=json_serializer)

    @login_required
    @routes.get("/api/radios/{item_id}")
    async def async_radio(self, request):
        """Get full radio details."""
        item_id = request.match_info.get("item_id")
        provider = request.rel_url.query.get("provider")
        if item_id is None or provider is None:
            return web.Response(text="invalid item_id or provider", status=501)
        result = await self.mass.music_manager.async_get_radio(item_id, provider)
        return web.json_response(result, dumps=json_serializer)

    @routes.get("/api/{media_type}/{media_id}/thumb")
    async def async_get_image(self, request):
        """Get (resized) thumb image."""
        media_type_str = request.match_info.get("media_type")
        media_type = MediaType.from_string(media_type_str)
        media_id = request.match_info.get("media_id")
        provider = request.rel_url.query.get("provider")
        if media_id is None or provider is None:
            return web.Response(text="invalid media_id or provider", status=501)
        size = int(request.rel_url.query.get("size", 0))
        img_file = await self.mass.music_manager.async_get_image_thumb(
            media_id, provider, media_type, size
        )
        if not img_file or not os.path.isfile(img_file):
            return web.Response(status=404)
        headers = {"Cache-Control": "max-age=86400, public", "Pragma": "public"}
        return web.FileResponse(img_file, headers=headers)

    @login_required
    @routes.get("/api/artists/{item_id}/toptracks")
    async def async_artist_toptracks(self, request):
        """Get top tracks for given artist."""
        item_id = request.match_info.get("item_id")
        provider = request.rel_url.query.get("provider")
        if item_id is None or provider is None:
            return web.Response(text="invalid item_id or provider", status=501)
        iterator = self.mass.music_manager.async_get_artist_toptracks(item_id, provider)
        return await self.__async_stream_json(request, iterator)

    @login_required
    @routes.get("/api/artists/{item_id}/albums")
    async def async_artist_albums(self, request):
        """Get (all) albums for given artist."""
        item_id = request.match_info.get("item_id")
        provider = request.rel_url.query.get("provider")
        if item_id is None or provider is None:
            return web.Response(text="invalid item_id or provider", status=501)
        iterator = self.mass.music_manager.async_get_artist_albums(item_id, provider)
        return await self.__async_stream_json(request, iterator)

    @login_required
    @routes.get("/api/playlists/{item_id}/tracks")
    async def async_playlist_tracks(self, request):
        """Get playlist tracks from provider."""
        item_id = request.match_info.get("item_id")
        provider = request.rel_url.query.get("provider")
        if item_id is None or provider is None:
            return web.Response(text="invalid item_id or provider", status=501)
        iterator = self.mass.music_manager.async_get_playlist_tracks(item_id, provider)
        return await self.__async_stream_json(request, iterator)

    @login_required
    @routes.put("/api/playlists/{item_id}/tracks")
    async def async_add_playlist_tracks(self, request):
        """Add tracks to (editable) playlist."""
        item_id = request.match_info.get("item_id")
        body = await request.json()
        tracks = await self.__async_media_items_from_body(body)
        result = await self.mass.music_manager.async_add_playlist_tracks(
            item_id, tracks
        )
        return web.json_response(result, dumps=json_serializer)

    @login_required
    @routes.delete("/api/playlists/{item_id}/tracks")
    async def async_remove_playlist_tracks(self, request):
        """Remove tracks from (editable) playlist."""
        item_id = request.match_info.get("item_id")
        body = await request.json()
        tracks = await self.__async_media_items_from_body(body)
        result = await self.mass.music_manager.async_remove_playlist_tracks(
            item_id, tracks
        )
        return web.json_response(result, dumps=json_serializer)

    @login_required
    @routes.get("/api/albums/{item_id}/tracks")
    async def async_album_tracks(self, request):
        """Get album tracks from provider."""
        item_id = request.match_info.get("item_id")
        provider = request.rel_url.query.get("provider")
        if item_id is None or provider is None:
            return web.Response(text="invalid item_id or provider", status=501)
        iterator = self.mass.music_manager.async_get_album_tracks(item_id, provider)
        return await self.__async_stream_json(request, iterator)

    @login_required
    @routes.get("/api/albums/{item_id}/versions")
    async def async_album_versions(self, request):
        """Get all versions of an album."""
        item_id = request.match_info.get("item_id")
        provider = request.rel_url.query.get("provider")
        if item_id is None or provider is None:
            return web.Response(text="invalid item_id or provider", status=501)
        iterator = self.mass.music_manager.async_get_album_versions(item_id, provider)
        return await self.__async_stream_json(request, iterator)

    @login_required
    @routes.get("/api/tracks/{item_id}/versions")
    async def async_track_versions(self, request):
        """Get all versions of an track."""
        item_id = request.match_info.get("item_id")
        provider = request.rel_url.query.get("provider")
        if item_id is None or provider is None:
            return web.Response(text="invalid item_id or provider", status=501)
        iterator = self.mass.music_manager.async_get_track_versions(item_id, provider)
        return await self.__async_stream_json(request, iterator)

    @login_required
    @routes.get("/api/search")
    async def async_search(self, request):
        """Search database and/or providers."""
        searchquery = request.rel_url.query.get("query")
        media_types_query = request.rel_url.query.get("media_types")
        limit = request.rel_url.query.get("limit", 5)
        media_types = []
        if not media_types_query or "artists" in media_types_query:
            media_types.append(MediaType.Artist)
        if not media_types_query or "albums" in media_types_query:
            media_types.append(MediaType.Album)
        if not media_types_query or "tracks" in media_types_query:
            media_types.append(MediaType.Track)
        if not media_types_query or "playlists" in media_types_query:
            media_types.append(MediaType.Playlist)
        if not media_types_query or "radios" in media_types_query:
            media_types.append(MediaType.Radio)
        # get results from database
        result = await self.mass.music_manager.async_global_search(
            searchquery, media_types, limit=limit
        )
        return web.json_response(result, dumps=json_serializer)

    @login_required
    @routes.get("/api/players")
    async def async_players(self, request):
        # pylint: disable=unused-argument
        """Get all players."""
        players = self.mass.player_manager.players
        players.sort(key=lambda x: str(x.name), reverse=False)
        return web.json_response(players, dumps=json_serializer)

    @login_required
    @routes.post("/api/players/{player_id}/cmd/{cmd}")
    async def async_player_command(self, request):
        """Issue player command."""
        success = False
        player_id = request.match_info.get("player_id")
        cmd = request.match_info.get("cmd")
        try:
            cmd_args = await request.json()
        except json.decoder.JSONDecodeError:
            cmd_args = None
        player_cmd = getattr(self.mass.player_manager, f"async_cmd_{cmd}", None)
        if player_cmd and cmd_args is not None:
            success = await player_cmd(player_id, cmd_args)
        elif player_cmd:
            success = await player_cmd(player_id)
        else:
            return web.Response(text="invalid command", status=501)
        result = {"success": success in [True, None]}
        return web.json_response(result, dumps=json_serializer)

    @login_required
    @routes.post("/api/players/{player_id}/play_media/{queue_opt}")
    async def async_player_play_media(self, request):
        """Issue player play media command."""
        player_id = request.match_info.get("player_id")
        player = self.mass.player_manager.get_player(player_id)
        if not player:
            return web.Response(status=404)
        queue_opt = QueueOption(request.match_info.get("queue_opt", "play"))
        body = await request.json()
        media_items = await self.__async_media_items_from_body(body)
        success = await self.mass.player_manager.async_play_media(
            player_id, media_items, queue_opt
        )
        result = {"success": success in [True, None]}
        return web.json_response(result, dumps=json_serializer)

    @login_required
    @routes.get("/api/players/{player_id}/queue/items/{queue_item}")
    async def async_player_queue_item(self, request):
        """Return item (by index or queue item id) from the player's queue."""
        player_id = request.match_info.get("player_id")
        item_id = request.match_info.get("queue_item")
        player_queue = self.mass.player_manager.get_player_queue(player_id)
        try:
            item_id = int(item_id)
            queue_item = player_queue.get_item(item_id)
        except ValueError:
            queue_item = player_queue.by_item_id(item_id)
        return web.json_response(queue_item, dumps=json_serializer)

    @login_required
    @routes.get("/api/players/{player_id}/queue/items")
    async def async_player_queue_items(self, request):
        """Return the items in the player's queue."""
        player_id = request.match_info.get("player_id")
        player_queue = self.mass.player_manager.get_player_queue(player_id)

        async def async_queue_tracks_iter():
            for item in player_queue.items:
                yield item

        return await self.__async_stream_json(request, async_queue_tracks_iter())

    @login_required
    @routes.get("/api/players/{player_id}/queue")
    async def async_player_queue(self, request):
        """Return the player queue details."""
        player_id = request.match_info.get("player_id")
        player_queue = self.mass.player_manager.get_player_queue(player_id)
        return web.json_response(player_queue, dumps=json_serializer)

    @login_required
    @routes.put("/api/players/{player_id}/queue/{cmd}")
    async def async_player_queue_cmd(self, request):
        """Change the player queue details."""
        player_id = request.match_info.get("player_id")
        player_queue = self.mass.player_manager.get_player_queue(player_id)
        cmd = request.match_info.get("cmd")
        try:
            cmd_args = await request.json()
        except json.decoder.JSONDecodeError:
            cmd_args = None
        if cmd == "repeat_enabled":
            player_queue.repeat_enabled = cmd_args
        elif cmd == "shuffle_enabled":
            player_queue.shuffle_enabled = cmd_args
        elif cmd == "clear":
            await player_queue.async_clear()
        elif cmd == "index":
            await player_queue.async_play_index(cmd_args)
        elif cmd == "move_up":
            await player_queue.async_move_item(cmd_args, -1)
        elif cmd == "move_down":
            await player_queue.async_move_item(cmd_args, 1)
        elif cmd == "next":
            await player_queue.async_move_item(cmd_args, 0)
        return web.json_response(player_queue.to_dict(), dumps=json_serializer)

    @login_required
    @routes.get("/api/players/{player_id}")
    async def async_player(self, request):
        """Get single player."""
        player_id = request.match_info.get("player_id")
        player = self.mass.player_manager.get_player(player_id)
        if not player:
            return web.Response(text="invalid player", status=404)
        return web.json_response(player, dumps=json_serializer)

    @login_required
    @routes.get("/api/config")
    async def async_get_config(self, request):
        # pylint: disable=unused-argument
        """Get the full config."""
        conf = {
            CONF_KEY_BASE: self.mass.config.base,
            CONF_KEY_PROVIDERS: self.mass.config.providers,
            CONF_KEY_PLAYERSETTINGS: self.mass.config.player_settings,
        }
        return web.json_response(conf, dumps=json_serializer)

    @login_required
    @routes.get("/api/config/{base}")
    async def async_get_config_item(self, request):
        """Get the config by base type."""
        conf_base = request.match_info.get("base")
        conf = self.mass.config[conf_base]
        return web.json_response(conf, dumps=json_serializer)

    @login_required
    @routes.put("/api/config/{base}/{key}/{entry_key}")
    async def async_put_config(self, request):
        """Save the given config item."""
        conf_key = request.match_info.get("key")
        conf_base = request.match_info.get("base")
        entry_key = request.match_info.get("entry_key")
        try:
            new_value = await request.json()
        except json.decoder.JSONDecodeError:
            new_value = (
                self.mass.config[conf_base][conf_key].get_entry(entry_key).default_value
            )
        self.mass.config[conf_base][conf_key][entry_key] = new_value
        return web.json_response(True)

    async def async_websocket_handler(self, request):
        """Handle websockets connection."""
        ws_response = None
        authenticated = False
        remove_callbacks = []
        try:
            ws_response = web.WebSocketResponse()
            await ws_response.prepare(request)

            # callback for internal events
            async def async_send_message(msg, msg_details=None):
                ws_msg = {"message": msg, "message_details": msg_details}
                await ws_response.send_json(ws_msg, dumps=json_serializer)

            # process incoming messages
            async for msg in ws_response:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    # not sure when/if this happens but log it anyway
                    LOGGER.warning(msg.data)
                    continue
                try:
                    data = msg.json()
                except json.decoder.JSONDecodeError:
                    await async_send_message(
                        "error",
                        'commands must be issued in json format \
                            {"message": "command", "message_details":" optional details"}',
                    )
                    continue
                msg = data.get("message")
                msg_details = data.get("message_details")
                if not authenticated and not msg == "login":
                    # make sure client is authenticated
                    await async_send_message("error", "authentication required")
                elif msg == "login" and msg_details:
                    # authenticate with token
                    try:
                        token_info = jwt.decode(msg_details, self._jwt_shared_secret)
                    except jwt.InvalidTokenError as exc:
                        LOGGER.exception(exc, exc_info=exc)
                        error_msg = "Invalid authorization token, " + str(exc)
                        await async_send_message("error", error_msg)
                    else:
                        authenticated = True
                        await async_send_message("login", token_info)
                elif msg == "add_event_listener":
                    remove_callbacks.append(
                        self.mass.add_event_listener(async_send_message, msg_details)
                    )
                    await async_send_message("event listener subscribed", msg_details)
                elif msg == "player_command":
                    player_id = msg_details.get("player_id")
                    cmd = msg_details.get("cmd")
                    cmd_args = msg_details.get("cmd_args")
                    player_cmd = getattr(
                        self.mass.player_manager, f"async_cmd_{cmd}", None
                    )
                    if player_cmd and cmd_args is not None:
                        result = await player_cmd(player_id, cmd_args)
                    elif player_cmd:
                        result = await player_cmd(player_id)
                    msg_details = {"cmd": cmd, "result": result}
                    await async_send_message("player_command_result", msg_details)
                else:
                    # simply echo the message on the eventbus
                    self.mass.signal_event(msg, msg_details)

        except (AssertionError, asyncio.CancelledError) as exc:
            LOGGER.warning("Websocket disconnected - %s", str(exc))
        finally:
            for callback in remove_callbacks:
                callback()
        LOGGER.debug("websocket connection closed")
        return ws_response

    @require_local_subnet
    async def async_json_rpc(self, request):
        """
        Implement LMS jsonrpc interface.

        for some compatability with tools that talk to lms
        only support for basic commands
        """
        # pylint: disable=too-many-branches
        data = await request.json()
        LOGGER.debug("jsonrpc: %s", data)
        params = data["params"]
        player_id = params[0]
        cmds = params[1]
        cmd_str = " ".join(cmds)
        if cmd_str == "play":
            await self.mass.player_manager.async_cmd_play(player_id)
        elif cmd_str == "pause":
            await self.mass.player_manager.async_cmd_pause(player_id)
        elif cmd_str == "stop":
            await self.mass.player_manager.async_cmd_stop(player_id)
        elif cmd_str == "next":
            await self.mass.player_manager.async_cmd_next(player_id)
        elif cmd_str == "previous":
            await self.mass.player_manager.async_cmd_previous(player_id)
        elif "power" in cmd_str:
            powered = cmds[1] if len(cmds) > 1 else False
            if powered:
                await self.mass.player_manager.async_cmd_power_on(player_id)
            else:
                await self.mass.player_manager.async_cmd_power_off(player_id)
        elif cmd_str == "playlist index +1":
            await self.mass.player_manager.async_cmd_next(player_id)
        elif cmd_str == "playlist index -1":
            await self.mass.player_manager.async_cmd_previous(player_id)
        elif "mixer volume" in cmd_str and "+" in cmds[2]:
            player = self.mass.player_manager.get_player(player_id)
            volume_level = player.volume_level + int(cmds[2].split("+")[1])
            await self.mass.player_manager.async_cmd_volume_set(player_id, volume_level)
        elif "mixer volume" in cmd_str and "-" in cmds[2]:
            player = self.mass.player_manager.get_player(player_id)
            volume_level = player.volume_level - int(cmds[2].split("-")[1])
            await self.mass.player_manager.async_cmd_volume_set(player_id, volume_level)
        elif "mixer volume" in cmd_str:
            await self.mass.player_manager.async_cmd_volume_set(player_id, cmds[2])
        elif cmd_str == "mixer muting 1":
            await self.mass.player_manager.async_cmd_volume_mute(player_id, True)
        elif cmd_str == "mixer muting 0":
            await self.mass.player_manager.async_cmd_volume_mute(player_id, False)
        elif cmd_str == "button volup":
            await self.mass.player_manager.async_cmd_volume_up(player_id)
        elif cmd_str == "button voldown":
            await self.mass.player_manager.async_cmd_volume_down(player_id)
        elif cmd_str == "button power":
            await self.mass.player_manager.async_cmd_power_toggle(player_id)
        else:
            return web.Response(text="command not supported")
        return web.Response(text="success")

    async def __async_media_items_from_body(self, data):
        """Convert posted body data into media items."""
        if not isinstance(data, list):
            data = [data]
        media_items = []
        for item in data:
            media_item = await self.mass.music_manager.async_get_item(
                item["item_id"],
                item["provider"],
                MediaType.from_string(item["media_type"]),
                lazy=True,
            )
            media_items.append(media_item)
        return media_items

    async def __async_stream_json(self, request, iterator):
        """Stream items from async iterator as json object."""
        resp = web.StreamResponse(
            status=200, reason="OK", headers={"Content-Type": "application/json"}
        )
        await resp.prepare(request)
        # write json open tag
        json_response = '{ "items": ['
        await resp.write(json_response.encode("utf-8"))
        count = 0
        async for item in iterator:
            # write each item into the items object of the json
            if count:
                json_response = "," + json_serializer(item)
            else:
                json_response = json_serializer(item)
            await resp.write(json_response.encode("utf-8"))
            count += 1
        # write json close tag
        json_response = '], "count": %s }' % count
        await resp.write((json_response).encode("utf-8"))
        await resp.write_eof()
        return resp

    async def __async_get_token(self, username, password):
        """Validate given credentials and return JWT token."""
        verified = self.mass.config.validate_credentials(username, password)
        if verified:
            token_expires = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
            scopes = ["user:admin"]  # scopes not yet implemented
            token = jwt.encode(
                {"username": username, "scopes": scopes, "exp": token_expires},
                self._jwt_shared_secret,
            )
            return {
                "user": username,
                "token": token.decode(),
                "expires": token_expires,
                "scopes": scopes,
            }
        return None
