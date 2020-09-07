"""The web module handles serving the frontend and the rest/websocket api's"""
import asyncio
import inspect
import json
import logging
import os
import ssl

import aiohttp
from aiohttp import web
from music_assistant.constants import (
    CONF_KEY_BASE,
    CONF_KEY_PLAYERSETTINGS,
    CONF_KEY_PROVIDERS,
)
from music_assistant.models.media_types import MediaType, media_type_from_string
from music_assistant.utils import (
    EnhancedJSONEncoder,
    get_ip,
    json_serializer,
)

import aiohttp_cors

LOGGER = logging.getLogger("mass")


class ClassRouteTableDef(web.RouteTableDef):
    """Helper class to add class based routing tables."""

    def __repr__(self) -> str:
        return "<ClassRouteTableDef count={}>".format(len(self._items))

    def route(self, method: str, path: str, **kwargs):
        def inner(handler):
            handler.route_info = (method, path, kwargs)
            return handler

        return inner

    def add_class_routes(self, instance) -> None:
        def predicate(member) -> bool:
            return all((inspect.iscoroutinefunction(member), hasattr(member, "route_info")))

        for _, handler in inspect.getmembers(instance, predicate):
            method, path, kwargs = handler.route_info
            super().route(method, path, **kwargs)(handler)


routes = ClassRouteTableDef()


class Web:
    """webserver and json/websocket api"""

    def __init__(self, mass):
        self.mass = mass
        # load/create/update config
        self.local_ip = get_ip()
        self.config = mass.config.base["web"]
        self.runner = None

        self.http_port = self.config["http_port"]
        enable_ssl = self.config["ssl_certificate"] and self.config["ssl_key"]
        if self.config["ssl_certificate"] and not os.path.isfile(self.config["ssl_certificate"]):
            enable_ssl = False
            LOGGER.warning("SSL certificate file not found: %s", self.config["ssl_certificate"])
        if self.config["ssl_key"] and not os.path.isfile(self.config["ssl_key"]):
            enable_ssl = False
            LOGGER.warning("SSL certificate key file not found: %s", self.config["ssl_key"])
        self.https_port = self.config["https_port"]
        self._enable_ssl = enable_ssl

    async def async_setup(self):
        """perform async setup"""
        routes.add_class_routes(self)
        app = web.Application()
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
                web.get("/", self.async_index),
                web.get("/jsonrpc.js", self.async_json_rpc),
                web.post("/jsonrpc.js", self.async_json_rpc),
                web.get("/ws", self.async_websocket_handler),
            ]
        )
        webdir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web/")
        app.router.add_static("/", webdir, append_version=True)

        # Add CORS support to all routes
        cors = aiohttp_cors.setup(
            app,
            defaults={
                "*": aiohttp_cors.ResourceOptions(
                    allow_credentials=True,
                    expose_headers="*",
                    allow_headers="*",
                    allow_methods=["POST", "PUT", "DELETE"],
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
            ssl_context.load_cert_chain(self.config["ssl_certificate"], self.config["ssl_key"])
            https_site = web.TCPSite(
                self.runner,
                "0.0.0.0",
                self.config["https_port"],
                ssl_context=ssl_context,
            )
            await https_site.start()
            LOGGER.info("Started HTTPS webserver on port %s", self.config["https_port"])

    async def async_index(self, request):
        # pylint: disable=unused-argument
        index_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web/index.html")
        return web.FileResponse(index_file)

    @routes.get("/api/library/artists")
    async def async_library_artists(self, request):
        """Get all library artists."""
        orderby = request.query.get("orderby", "name")
        provider_filter = request.rel_url.query.get("provider")
        iterator = self.mass.music_manager.async_get_library_artists(
            orderby=orderby, provider_filter=provider_filter
        )
        return await self.__async_stream_json(request, iterator)

    @routes.get("/api/library/albums")
    async def async_library_albums(self, request):
        """Get all library albums."""
        orderby = request.query.get("orderby", "name")
        provider_filter = request.rel_url.query.get("provider")
        iterator = self.mass.music_manager.async_get_library_albums(
            orderby=orderby, provider_filter=provider_filter
        )
        return await self.__async_stream_json(request, iterator)

    @routes.get("/api/library/tracks")
    async def async_library_tracks(self, request):
        """Get all library tracks."""
        orderby = request.query.get("orderby", "name")
        provider_filter = request.rel_url.query.get("provider")
        iterator = self.mass.music_manager.async_get_library_tracks(
            orderby=orderby, provider_filter=provider_filter
        )
        return await self.__async_stream_json(request, iterator)

    @routes.get("/api/library/radios")
    async def async_library_radios(self, request):
        """Get all library radios."""
        orderby = request.query.get("orderby", "name")
        provider_filter = request.rel_url.query.get("provider")
        iterator = self.mass.music_manager.async_get_library_radios(
            orderby=orderby, provider_filter=provider_filter
        )
        return await self.__async_stream_json(request, iterator)

    @routes.get("/api/library/playlists")
    async def async_library_playlists(self, request):
        """Get all library playlists."""
        orderby = request.query.get("orderby", "name")
        provider_filter = request.rel_url.query.get("provider")
        iterator = self.mass.music_manager.async_get_library_playlists(
            orderby=orderby, provider_filter=provider_filter
        )
        return await self.__async_stream_json(request, iterator)

    @routes.put("/api/library")
    async def async_library_add(self, request):
        """Add item(s) to the library"""
        body = await request.json()
        media_items = await self.__async_media_items_from_body(body)
        result = await self.mass.music_manager.async_library_add(media_items)
        return web.json_response(result, dumps=json_serializer)

    @routes.delete("/api/library")
    async def async_library_remove(self, request):
        """R remove item(s) from the library"""
        body = await request.json()
        media_items = await self.__async_media_items_from_body(body)
        result = await self.mass.music_manager.async_library_remove(media_items)
        return web.json_response(result, dumps=json_serializer)

    @routes.get("/api/artists/{item_id}")
    async def async_artist(self, request):
        """get full artist details"""
        item_id = request.match_info.get("item_id")
        provider = request.rel_url.query.get("provider")
        lazy = request.rel_url.query.get("lazy", "false") != "false"
        if item_id is None or provider is None:
            return web.Response(text="invalid item or provider", status=501)
        result = await self.mass.music_manager.async_get_artist(item_id, provider, lazy=lazy)
        return web.json_response(result, dumps=json_serializer)

    @routes.get("/api/albums/{item_id}")
    async def async_album(self, request):
        """get full album details"""
        item_id = request.match_info.get("item_id")
        provider = request.rel_url.query.get("provider")
        lazy = request.rel_url.query.get("lazy", "false") != "false"
        if item_id is None or provider is None:
            return web.Response(text="invalid item or provider", status=501)
        result = await self.mass.music_manager.async_get_album(item_id, provider, lazy=lazy)
        return web.json_response(result, dumps=json_serializer)

    @routes.get("/api/tracks/{item_id}")
    async def async_track(self, request):
        """get full track details"""
        item_id = request.match_info.get("item_id")
        provider = request.rel_url.query.get("provider")
        lazy = request.rel_url.query.get("lazy", "false") != "false"
        if item_id is None or provider is None:
            return web.Response(text="invalid item or provider", status=501)
        result = await self.mass.music_manager.async_get_track(
            item_id, provider, lazy=lazy, refresh=True
        )
        return web.json_response(result, dumps=json_serializer)

    @routes.get("/api/playlists/{item_id}")
    async def async_playlist(self, request):
        """get full playlist details"""
        item_id = request.match_info.get("item_id")
        provider = request.rel_url.query.get("provider")
        if item_id is None or provider is None:
            return web.Response(text="invalid item or provider", status=501)
        result = await self.mass.music_manager.async_get_playlist(item_id, provider)
        return web.json_response(result, dumps=json_serializer)

    @routes.get("/api/radios/{item_id}")
    async def async_radio(self, request):
        """get full radio details"""
        item_id = request.match_info.get("item_id")
        provider = request.rel_url.query.get("provider")
        if item_id is None or provider is None:
            return web.Response(text="invalid item_id or provider", status=501)
        result = await self.mass.music_manager.async_get_radio(item_id, provider)
        return web.json_response(result, dumps=json_serializer)

    @routes.get("/api/{media_type}/{media_id}/thumb")
    async def async_get_image(self, request):
        """get (resized) thumb image"""
        media_type_str = request.match_info.get("media_type")
        media_type = media_type_from_string(media_type_str)
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

    @routes.get("/api/artists/{item_id}/toptracks")
    async def async_artist_toptracks(self, request):
        """get top tracks for given artist"""
        item_id = request.match_info.get("item_id")
        provider = request.rel_url.query.get("provider")
        if item_id is None or provider is None:
            return web.Response(text="invalid item_id or provider", status=501)
        iterator = self.mass.music_manager.async_get_artist_toptracks(item_id, provider)
        return await self.__async_stream_json(request, iterator)

    @routes.get("/api/artists/{item_id}/albums")
    async def async_artist_albums(self, request):
        """get (all) albums for given artist"""
        item_id = request.match_info.get("item_id")
        provider = request.rel_url.query.get("provider")
        if item_id is None or provider is None:
            return web.Response(text="invalid item_id or provider", status=501)
        iterator = self.mass.music_manager.async_get_artist_albums(item_id, provider)
        return await self.__async_stream_json(request, iterator)

    @routes.get("/api/playlists/{item_id}/tracks")
    async def async_playlist_tracks(self, request):
        """get playlist tracks from provider"""
        item_id = request.match_info.get("item_id")
        provider = request.rel_url.query.get("provider")
        if item_id is None or provider is None:
            return web.Response(text="invalid item_id or provider", status=501)
        iterator = self.mass.music_manager.async_get_playlist_tracks(item_id, provider)
        return await self.__async_stream_json(request, iterator)

    @routes.put("/api/playlists/{item_id}/tracks")
    async def async_add_playlist_tracks(self, request):
        """Add tracks to (editable) playlist."""
        item_id = request.match_info.get("item_id")
        body = await request.json()
        tracks = await self.__async_media_items_from_body(body)
        result = await self.mass.music_manager.async_add_playlist_tracks(item_id, tracks)
        return web.json_response(result, dumps=json_serializer)

    @routes.delete("/api/playlists/{item_id}/tracks")
    async def async_remove_playlist_tracks(self, request):
        """Remove tracks from (editable) playlist."""
        item_id = request.match_info.get("item_id")
        body = await request.json()
        tracks = await self.__async_media_items_from_body(body)
        result = await self.mass.music_manager.async_remove_playlist_tracks(item_id, tracks)
        return web.json_response(result, dumps=json_serializer)

    @routes.get("/api/albums/{item_id}/tracks")
    async def async_album_tracks(self, request):
        """Get album tracks from provider."""
        item_id = request.match_info.get("item_id")
        provider = request.rel_url.query.get("provider")
        if item_id is None or provider is None:
            return web.Response(text="invalid item_id or provider", status=501)
        iterator = self.mass.music_manager.async_get_album_tracks(item_id, provider)
        return await self.__async_stream_json(request, iterator)

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

    @routes.get("/api/players")
    async def async_players(self, request):
        # pylint: disable=unused-argument
        """get all players"""
        players = self.mass.player_manager.players
        players.sort(key=lambda x: str(x.name), reverse=False)
        return web.json_response(players, dumps=json_serializer)

    @routes.post("/api/players/{player_id}/cmd/{cmd}")
    async def async_player_command(self, request):
        """issue player command"""
        result = False
        player_id = request.match_info.get("player_id")
        cmd = request.match_info.get("cmd")
        cmd_args = await request.json()
        player_cmd = getattr(self.mass.player_manager, f"async_cmd_{cmd}", None)
        if player_cmd and cmd_args is not None:
            result = await player_cmd(player_id, cmd_args)
        elif player_cmd:
            result = await player_cmd(player_id)
        else:
            return web.Response(text="invalid command", status=501)
        return web.json_response(result, dumps=json_serializer)

    @routes.post("/api/players/{player_id}/play_media/{queue_opt}")
    async def async_player_play_media(self, request):
        """issue player play_media command"""
        player_id = request.match_info.get("player_id")
        player = self.mass.player_manager.get_player(player_id)
        if not player:
            return web.Response(status=404)
        queue_opt = request.match_info.get("queue_opt", "play")
        body = await request.json()
        media_items = await self.__async_media_items_from_body(body)
        result = await self.mass.player_manager.async_play_media(player_id, media_items, queue_opt)
        return web.json_response(result, dumps=json_serializer)

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

    @routes.get("/api/players/{player_id}/queue/items")
    async def async_player_queue_items(self, request):
        """Return the items in the player's queue."""
        player_id = request.match_info.get("player_id")
        player_queue = self.mass.player_manager.get_player_queue(player_id)

        async def async_queue_tracks_iter():
            for item in player_queue.items:
                yield item

        return await self.__async_stream_json(request, async_queue_tracks_iter())

    @routes.get("/api/players/{player_id}/queue")
    async def async_player_queue(self, request):
        """return the player queue details"""
        player_id = request.match_info.get("player_id")
        player_queue = self.mass.player_manager.get_player_queue(player_id)
        return web.json_response(player_queue, dumps=json_serializer)

    @routes.put("/api/players/{player_id}/queue/{cmd}")
    async def async_player_queue_cmd(self, request):
        """change the player queue details"""
        player_id = request.match_info.get("player_id")
        player_queue = self.mass.player_manager.get_player_queue(player_id)
        cmd = request.match_info.get("cmd")
        cmd_args = await request.json()
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
        return web.json_response(player_queue, dumps=json_serializer)

    @routes.get("/api/players/{player_id}")
    async def async_player(self, request):
        """get single player."""
        player_id = request.match_info.get("player_id")
        player = self.mass.player_manager.get_player(player_id)
        if not player:
            return web.Response(text="invalid player", status=404)
        return web.json_response(player, dumps=json_serializer)

    @routes.get("/api/config")
    async def async_get_config(self, request):
        # pylint: disable=unused-argument
        """get the config"""
        conf = {
            CONF_KEY_BASE: self.mass.config.base,
            CONF_KEY_PROVIDERS: self.mass.config.providers,
            CONF_KEY_PLAYERSETTINGS: self.mass.config.player_settings,
        }
        return web.json_response(conf, dumps=json_serializer)

    @routes.get("/api/config/{base}")
    async def async_get_config_item(self, request):
        """Get the config."""
        conf_base = request.match_info.get("base")
        conf = self.mass.config[conf_base]
        return web.json_response(conf, dumps=json_serializer)

    @routes.put("/api/config/{base}/{key}/{entry_key}")
    async def async_put_config(self, request):
        """save (partial) config"""
        conf_key = request.match_info.get("key")
        conf_base = request.match_info.get("base")
        entry_key = request.match_info.get("entry_key")
        try:
            new_value = await request.json()
        except json.decoder.JSONDecodeError:
            new_value = self.mass.config[conf_base][conf_key].get_entry(entry_key).default_value
        self.mass.config[conf_base][conf_key][entry_key] = new_value
        return web.json_response(True)

    async def async_websocket_handler(self, request):
        """websockets handler"""
        ws = None
        try:
            ws = web.WebSocketResponse()
            await ws.prepare(request)

            # register callback for internal events
            async def async_send_event(msg, msg_details):
                ws_msg = {"message": msg, "message_details": msg_details}
                try:
                    await ws.send_json(ws_msg, dumps=json_serializer)
                except (AssertionError, asyncio.CancelledError):
                    remove_callback()

            remove_callback = self.mass.add_event_listener(async_send_event)
            # process incoming messages
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.ERROR:
                    LOGGER.debug("ws connection closed with exception %s", ws.exception())
                elif msg.type != aiohttp.WSMsgType.TEXT:
                    LOGGER.warning(msg.data)
                else:
                    data = msg.json()
                    # echo the websocket message on event bus
                    # can be picked up by other modules, e.g. the webplayer
                    self.mass.signal_event(data["message"], data["message_details"])
        except (AssertionError, asyncio.CancelledError) as exc:
            LOGGER.warning("Websocket disconnected - %s", str(exc))
        finally:
            remove_callback()
        LOGGER.debug("websocket connection closed")
        return ws

    async def async_json_rpc(self, request):
        """
        implement LMS jsonrpc interface
        for some compatability with tools that talk to lms
        only support for basic commands
        """
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
        """Helper to turn posted body data into media items."""
        if not isinstance(data, list):
            data = [data]
        media_items = []
        for item in data:
            media_item = await self.mass.music_manager.async_get_item(
                item["item_id"], item["provider"], item["media_type"], lazy=True
            )
            media_items.append(media_item)
        return media_items

    async def __async_stream_json(self, request, iterator):
        """stream items from async iterator as json object"""
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
                json_response = "," + json.dumps(item, cls=EnhancedJSONEncoder)
            else:
                json_response = json.dumps(item, cls=EnhancedJSONEncoder)
            await resp.write(json_response.encode("utf-8"))
            count += 1
        # write json close tag
        json_response = '], "count": %s }' % count
        await resp.write((json_response).encode("utf-8"))
        await resp.write_eof()
        return resp
