#!/usr/bin/env python3
# -*- coding:utf-8 -*-

import asyncio
import os
import aiohttp
import inspect
import aiohttp_cors
from aiohttp import web
from functools import partial
import ssl
import concurrent
import threading
from .models.media_types import MediaItem, MediaType, media_type_from_string
from .utils import run_periodic, LOGGER, IS_HASSIO, run_async_background_task, get_ip, json_serializer
from .constants import CONF_KEY_PLAYERSETTINGS, CONF_KEY_MUSICPROVIDERS, CONF_KEY_PLAYERPROVIDERS

CONF_KEY = 'web'

if IS_HASSIO:
    # on hassio we use ingress
    CONFIG_ENTRIES = [('https_port', 8096, 'web_https_port'),
                      ('ssl_certificate', '', 'web_ssl_cert'),
                      ('ssl_key', '', 'web_ssl_key'),
                      ('cert_fqdn_host', '', 'cert_fqdn_host')]
else:
    CONFIG_ENTRIES = [('http_port', 8095, 'web_http_port'),
                      ('https_port', 8096, 'web_https_port'),
                      ('ssl_certificate', '', 'web_ssl_cert'),
                      ('ssl_key', '', 'web_ssl_key'),
                      ('cert_fqdn_host', '', 'cert_fqdn_host')]


class ClassRouteTableDef(web.RouteTableDef):
    def __repr__(self) -> str:
        return "<ClassRouteTableDef count={}>".format(len(self._items))

    def route(self, method: str, path: str, **kwargs):
        def inner(handler):
            handler.route_info = (method, path, kwargs)
            return handler

        return inner

    def add_class_routes(self, instance) -> None:
        def predicate(member) -> bool:
            return all((inspect.iscoroutinefunction(member),
                        hasattr(member, "route_info")))

        for _, handler in inspect.getmembers(instance, predicate):
            method, path, kwargs = handler.route_info
            super().route(method, path, **kwargs)(handler)


routes = ClassRouteTableDef()


class Web():
    """ webserver and json/websocket api """
    runner = None

    def __init__(self, mass):
        self.mass = mass
        # load/create/update config
        config = self.mass.config.create_module_config(CONF_KEY,
                                                       CONFIG_ENTRIES)
        self.local_ip = get_ip()
        self.config = config
        if IS_HASSIO:
            # retrieve ingress http port
            import requests
            url = 'http://hassio/addons/self/info'
            headers = { "X-HASSIO-KEY":os.environ["HASSIO_TOKEN"] }
            response = requests.get(url, headers=headers).json()
            self.http_port = response["data"]["ingress_port"]
        else:
            # use settings from config
            self.http_port = config['http_port']
        enable_ssl = config['ssl_certificate'] and config['ssl_key']
        if config['ssl_certificate'] and not os.path.isfile(
                config['ssl_certificate']):
            enable_ssl = False
            LOGGER.warning("SSL certificate file not found: %s",
                           config['ssl_certificate'])
        if config['ssl_key'] and not os.path.isfile(config['ssl_key']):
            enable_ssl = False
            LOGGER.warning("SSL certificate key file not found: %s",
                           config['ssl_key'])
        self.https_port = config['https_port']
        self._enable_ssl = enable_ssl

    async def setup(self):
        """ perform async setup """
        routes.add_class_routes(self)
        app = web.Application()
        app.add_routes(routes)
        app.add_routes([
            web.get('/stream/{player_id}',
                    self.mass.http_streamer.stream,
                    allow_head=False),
            web.get('/stream/{player_id}/{queue_item_id}',
                    self.mass.http_streamer.stream,
                    allow_head=False),
            web.get('/', self.index),
            web.get('/jsonrpc.js', self.json_rpc),
            web.post('/jsonrpc.js', self.json_rpc),
            web.get('/ws', self.websocket_handler)
        ])
        webdir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              'web/')
        app.router.add_static("/", webdir)

        # Add CORS support to all routes
        cors = aiohttp_cors.setup(
            app,
            defaults={
                "*":
                aiohttp_cors.ResourceOptions(
                    allow_credentials=True,
                    expose_headers="*",
                    allow_headers="*",
                    allow_methods=["POST", "PUT", "DELETE"])
            })
        for route in list(app.router.routes()):
            cors.add(route)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        http_site = web.TCPSite(self.runner, '0.0.0.0', self.http_port)
        await http_site.start()
        LOGGER.info("Started HTTP webserver on port %s", self.http_port)
        if self._enable_ssl:
            ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
            ssl_context.load_cert_chain(self.config['ssl_certificate'],
                                        self.config['ssl_key'])
            https_site = web.TCPSite(self.runner,
                                     '0.0.0.0',
                                     self.config['https_port'],
                                     ssl_context=ssl_context)
            await https_site.start()
            LOGGER.info("Started HTTPS webserver on port %s",
                        self.config['https_port'])

    async def index(self, request):
        index_file = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  'web/index.html')
        return web.FileResponse(index_file)

    @routes.get('/api/library/artists')
    async def library_artists(self, request):
        """Get all library artists."""
        orderby = request.query.get('orderby', 'name')
        provider_filter = request.rel_url.query.get('provider')
        iterator = self.mass.music.library_artists(
            orderby=orderby, provider_filter=provider_filter)
        return await self.__stream_json(request, iterator)

    @routes.get('/api/library/albums')
    async def library_albums(self, request):
        """Get all library albums."""
        orderby = request.query.get('orderby', 'name')
        provider_filter = request.rel_url.query.get('provider')
        iterator = self.mass.music.library_albums(
            orderby=orderby, provider_filter=provider_filter)
        return await self.__stream_json(request, iterator)

    @routes.get('/api/library/tracks')
    async def library_tracks(self, request):
        """Get all library tracks."""
        orderby = request.query.get('orderby', 'name')
        provider_filter = request.rel_url.query.get('provider')
        iterator = self.mass.music.library_tracks(
            orderby=orderby, provider_filter=provider_filter)
        return await self.__stream_json(request, iterator)

    @routes.get('/api/library/radios')
    async def library_radios(self, request):
        """Get all library radios."""
        orderby = request.query.get('orderby', 'name')
        provider_filter = request.rel_url.query.get('provider')
        iterator = self.mass.music.library_radios(
            orderby=orderby, provider_filter=provider_filter)
        return await self.__stream_json(request, iterator)

    @routes.get('/api/library/playlists')
    async def library_playlists(self, request):
        """Get all library playlists."""
        orderby = request.query.get('orderby', 'name')
        provider_filter = request.rel_url.query.get('provider')
        iterator = self.mass.music.library_playlists(
            orderby=orderby, provider_filter=provider_filter)
        return await self.__stream_json(request, iterator)

    @routes.put('/api/library')
    async def library_add(self, request):
        """Add item(s) to the library"""
        body = await request.json()
        media_items = await self.__media_items_from_body(body)
        result = await self.mass.music.library_add(media_items)
        return web.json_response(result, dumps=json_serializer)

    @routes.delete('/api/library')
    async def library_remove(self, request):
        """R remove item(s) from the library"""
        body = await request.json()
        media_items = await self.__media_items_from_body(body)
        result = await self.mass.music.library_remove(media_items)
        return web.json_response(result, dumps=json_serializer)

    @routes.get('/api/artists/{item_id}')
    async def artist(self, request):
        """ get full artist details"""
        item_id = request.match_info.get('item_id')
        provider = request.rel_url.query.get('provider')
        lazy = request.rel_url.query.get('lazy', 'true') != 'false'
        if (item_id is None or provider is None):
            return web.Response(text='invalid item or provider', status=501)
        result = await self.mass.music.artist(item_id, provider, lazy=lazy)
        return web.json_response(result, dumps=json_serializer)

    @routes.get('/api/albums/{item_id}')
    async def album(self, request):
        """ get full album details"""
        item_id = request.match_info.get('item_id')
        provider = request.rel_url.query.get('provider')
        lazy = request.rel_url.query.get('lazy', 'true') != 'false'
        if (item_id is None or provider is None):
            return web.Response(text='invalid item or provider', status=501)
        result = await self.mass.music.album(item_id, provider, lazy=lazy)
        return web.json_response(result, dumps=json_serializer)

    @routes.get('/api/tracks/{item_id}')
    async def track(self, request):
        """ get full track details"""
        item_id = request.match_info.get('item_id')
        provider = request.rel_url.query.get('provider')
        lazy = request.rel_url.query.get('lazy', 'true') != 'false'
        if (item_id is None or provider is None):
            return web.Response(text='invalid item or provider', status=501)
        result = await self.mass.music.track(item_id, provider, lazy=lazy)
        return web.json_response(result, dumps=json_serializer)

    @routes.get('/api/playlists/{item_id}')
    async def playlist(self, request):
        """ get full playlist details"""
        item_id = request.match_info.get('item_id')
        provider = request.rel_url.query.get('provider')
        if (item_id is None or provider is None):
            return web.Response(text='invalid item or provider', status=501)
        result = await self.mass.music.playlist(item_id, provider)
        return web.json_response(result, dumps=json_serializer)

    @routes.get('/api/radios/{item_id}')
    async def radio(self, request):
        """ get full radio details"""
        item_id = request.match_info.get('item_id')
        provider = request.rel_url.query.get('provider')
        if (item_id is None or provider is None):
            return web.Response(text='invalid item_id or provider', status=501)
        result = await self.mass.music.radio(item_id, provider)
        return web.json_response(result, dumps=json_serializer)

    @routes.get('/api/{media_type}/{media_id}/thumb')
    async def get_image(self, request):
        """ get (resized) thumb image """
        media_type_str = request.match_info.get('media_type')
        media_type = media_type_from_string(media_type_str)
        media_id = request.match_info.get('media_id')
        provider = request.rel_url.query.get('provider')
        if (media_id is None or provider is None):
            return web.Response(text='invalid media_id or provider',
                                status=501)
        size = int(request.rel_url.query.get('size', 0))
        img_file = await self.mass.music.get_image_thumb(
            media_id, media_type, provider, size)
        if not img_file or not os.path.isfile(img_file):
            return web.Response(status=404)
        headers = {
            'Cache-Control': 'max-age=86400, public',
            'Pragma': 'public'
        }
        return web.FileResponse(img_file, headers=headers)

    @routes.get('/api/artists/{item_id}/toptracks')
    async def artist_toptracks(self, request):
        """ get top tracks for given artist """
        item_id = request.match_info.get('item_id')
        provider = request.rel_url.query.get('provider')
        if (item_id is None or provider is None):
            return web.Response(text='invalid item_id or provider', status=501)
        iterator = self.mass.music.artist_toptracks(item_id, provider)
        return await self.__stream_json(request, iterator)

    @routes.get('/api/artists/{item_id}/albums')
    async def artist_albums(self, request):
        """ get (all) albums for given artist """
        item_id = request.match_info.get('item_id')
        provider = request.rel_url.query.get('provider')
        if (item_id is None or provider is None):
            return web.Response(text='invalid item_id or provider', status=501)
        iterator = self.mass.music.artist_albums(item_id, provider)
        return await self.__stream_json(request, iterator)

    @routes.get('/api/playlists/{item_id}/tracks')
    async def playlist_tracks(self, request):
        """ get playlist tracks from provider"""
        item_id = request.match_info.get('item_id')
        provider = request.rel_url.query.get('provider')
        if (item_id is None or provider is None):
            return web.Response(text='invalid item_id or provider', status=501)
        iterator = self.mass.music.playlist_tracks(item_id, provider)
        return await self.__stream_json(request, iterator)

    @routes.put('/api/playlists/{item_id}/tracks')
    async def add_playlist_tracks(self, request):
        """Add tracks to (editable) playlist."""
        item_id = request.match_info.get('item_id')
        body = await request.json()
        tracks = await self.__media_items_from_body(body)
        result = await self.mass.music.add_playlist_tracks(item_id, tracks)
        return web.json_response(result, dumps=json_serializer)

    @routes.delete('/api/playlists/{item_id}/tracks')
    async def remove_playlist_tracks(self, request):
        """Remove tracks from (editable) playlist."""
        item_id = request.match_info.get('item_id')
        body = await request.json()
        tracks = await self.__media_items_from_body(body)
        result = await self.mass.music.remove_playlist_tracks(item_id, tracks)
        return web.json_response(result, dumps=json_serializer)

    @routes.get('/api/albums/{item_id}/tracks')
    async def album_tracks(self, request):
        """ get album tracks from provider"""
        item_id = request.match_info.get('item_id')
        provider = request.rel_url.query.get('provider')
        if (item_id is None or provider is None):
            return web.Response(text='invalid item_id or provider', status=501)
        iterator = self.mass.music.album_tracks(item_id, provider)
        return await self.__stream_json(request, iterator)

    @routes.get('/api/search')
    async def search(self, request):
        """ search database or providers """
        searchquery = request.rel_url.query.get('query')
        media_types_query = request.rel_url.query.get('media_types')
        limit = request.rel_url.query.get('limit', 5)
        online = request.rel_url.query.get('online', False)
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
        result = await self.mass.music.search(searchquery,
                                              media_types,
                                              limit=limit,
                                              online=online)
        return web.json_response(result, dumps=json_serializer)

    @routes.get('/api/players')
    async def players(self, request):
        """ get all players """
        players = list(self.mass.players.players)
        players.sort(key=lambda x: x.name, reverse=False)
        return web.json_response(players, dumps=json_serializer)

    @routes.post('/api/players/{player_id}/cmd/{cmd}')
    async def player_command(self, request):
        """ issue player command"""
        result = False
        player_id = request.match_info.get('player_id')
        player = await self.mass.players.get_player(player_id)
        if not player:
            return web.Response(text='invalid player', status=404)
        cmd = request.match_info.get('cmd')
        cmd_args = await request.json()
        player_cmd = getattr(player, cmd, None)
        if player_cmd and cmd_args is not None:
            result = await player_cmd(cmd_args)
        elif player_cmd:
            result = await player_cmd()
        else:
            return web.Response(text='invalid command', status=501)
        return web.json_response(result, dumps=json_serializer)

    @routes.post('/api/players/{player_id}/play_media/{queue_opt}')
    async def player_play_media(self, request):
        """ issue player play_media command"""
        player_id = request.match_info.get('player_id')
        player = await self.mass.players.get_player(player_id)
        if not player:
            return web.Response(status=404)
        queue_opt = request.match_info.get('queue_opt', 'play')
        body = await request.json()
        media_items = await self.__media_items_from_body(body)
        result = await self.mass.players.play_media(player_id, media_items,
                                                    queue_opt)
        return web.json_response(result, dumps=json_serializer)

    @routes.get('/api/players/{player_id}/queue/items/{queue_item}')
    async def player_queue_item(self, request):
        """ return item (by index or queue item id) from the player's queue """
        player_id = request.match_info.get('player_id')
        item_id = request.match_info.get('queue_item')
        player = await self.mass.players.get_player(player_id)
        try:
            item_id = int(item_id)
            queue_item = await player.queue.get_item(item_id)
        except ValueError:
            queue_item = await player.queue.by_item_id(item_id)
        return web.json_response(queue_item, dumps=json_serializer)

    @routes.get('/api/players/{player_id}/queue/items')
    async def player_queue_items(self, request):
        """ return the items in the player's queue """
        player_id = request.match_info.get('player_id')
        player = await self.mass.players.get_player(player_id)

        async def queue_tracks_iter():
            for item in player.queue.items:
                yield item

        return await self.__stream_json(request, queue_tracks_iter())

    @routes.get('/api/players/{player_id}/queue')
    async def player_queue(self, request):
        """ return the player queue details """
        player_id = request.match_info.get('player_id')
        player = await self.mass.players.get_player(player_id)
        return web.json_response(player.queue, dumps=json_serializer)

    @routes.put('/api/players/{player_id}/queue/{cmd}')
    async def player_queue_cmd(self, request):
        """ change the player queue details """
        player_id = request.match_info.get('player_id')
        player = await self.mass.players.get_player(player_id)
        cmd = request.match_info.get('cmd')
        cmd_args = await request.json()
        if cmd == 'repeat_enabled':
            player.queue.repeat_enabled = cmd_args
        elif cmd == 'shuffle_enabled':
            player.queue.shuffle_enabled = cmd_args
        elif cmd == 'clear':
            await player.queue.clear()
        elif cmd == 'index':
            await player.queue.play_index(cmd_args)
        elif cmd == 'move_up':
            await player.queue.move_item(cmd_args, -1)
        elif cmd == 'move_down':
            await player.queue.move_item(cmd_args, 1)
        elif cmd == 'next':
            await player.queue.move_item(cmd_args, 0)
        return web.json_response(player.queue, dumps=json_serializer)

    @routes.get('/api/players/{player_id}')
    async def player(self, request):
        """ get single player """
        player_id = request.match_info.get('player_id')
        player = await self.mass.players.get_player(player_id)
        if not player:
            return web.Response(text='invalid player', status=404)
        return web.json_response(player, dumps=json_serializer)

    @routes.get('/api/config')
    async def get_config(self, request):
        """ get the config """
        return web.json_response(self.mass.config)

    @routes.put('/api/config/{key}/{subkey}')
    async def put_config(self, request):
        """ save (partial) config """
        conf_key = request.match_info.get('key')
        conf_subkey = request.match_info.get('subkey')
        new_values = await request.json()
        LOGGER.debug(
            f'save config called for {conf_key}/{conf_subkey} - new value: {new_values}'
        )
        cur_values = self.mass.config[conf_key][conf_subkey]
        result = {
            "success": True,
            "restart_required": False,
            "settings_changed": False
        }
        if cur_values != new_values:
            # config changed
            result["settings_changed"] = True
            self.mass.config[conf_key][conf_subkey] = new_values
            if conf_key == CONF_KEY_PLAYERSETTINGS:
                # player settings: force update of player
                self.mass.event_loop.create_task(
                    self.mass.players.trigger_update(conf_subkey))
            elif conf_key == CONF_KEY_MUSICPROVIDERS:
                # (re)load music provider module
                self.mass.event_loop.create_task(
                    self.mass.music.load_modules(conf_subkey))
            elif conf_key == CONF_KEY_PLAYERPROVIDERS:
                # (re)load player provider module
                self.mass.event_loop.create_task(
                    self.mass.players.load_modules(conf_subkey))
            else:
                # other settings need restart
                result["restart_required"] = True
            self.mass.config.save()
        return web.json_response(result)

    async def websocket_handler(self, request):
        """ websockets handler """
        cb_id = None
        ws = None
        try:
            ws = web.WebSocketResponse()
            await ws.prepare(request)

            # register callback for internal events
            async def send_event(msg, msg_details):
                ws_msg = {"message": msg, "message_details": msg_details}
                try:
                    await ws.send_json(ws_msg)
                except (AssertionError, asyncio.CancelledError):
                    await self.mass.remove_event_listener(cb_id)

            cb_id = await self.mass.add_event_listener(send_event)
            # process incoming messages
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.ERROR:
                    LOGGER.debug('ws connection closed with exception %s' %
                                 ws.exception())
                elif msg.type != aiohttp.WSMsgType.TEXT:
                    LOGGER.warning(msg.data)
                else:
                    data = msg.json()
                    # echo the websocket message on event bus
                    # can be picked up by other modules, e.g. the webplayer
                    await self.mass.signal_event(data['message'],
                                                 data['message_details'])
        except (Exception, AssertionError, asyncio.CancelledError) as exc:
            LOGGER.warning("Websocket disconnected - %s" % str(exc))
        finally:
            await self.mass.remove_event_listener(cb_id)
        LOGGER.debug('websocket connection closed')
        return ws

    async def json_rpc(self, request):
        """ 
            implement LMS jsonrpc interface 
            for some compatability with tools that talk to lms
            only support for basic commands
        """
        data = await request.json()
        LOGGER.debug("jsonrpc: %s" % data)
        params = data['params']
        player_id = params[0]
        cmds = params[1]
        cmd_str = " ".join(cmds)
        player = await self.mass.players.get_player(player_id)
        if not player:
            return web.Response(status=404)
        if cmd_str == 'play':
            await player.play()
        elif cmd_str == 'pause':
            await player.pause()
        elif cmd_str == 'stop':
            await player.stop()
        elif cmd_str == 'next':
            await player.next()
        elif cmd_str == 'previous':
            await player.previous()
        elif 'power' in cmd_str:
            args = cmds[1] if len(cmds) > 1 else None
            await player.power(args)
        elif cmd_str == 'playlist index +1':
            await player.next()
        elif cmd_str == 'playlist index -1':
            await player.previous()
        elif 'mixer volume' in cmd_str and '+' in cmds[2]:
            volume_level = player.volume_level + int(cmds[2].split('+')[1])
            await player.volume_set(volume_level)
        elif 'mixer volume' in cmd_str and '-' in cmds[2]:
            volume_level = player.volume_level - int(cmds[2].split('-')[1])
            await player.volume_set(volume_level)
        elif 'mixer volume' in cmd_str:
            await player.volume_set(cmds[2])
        elif cmd_str == 'mixer muting 1':
            await player.volume_mute(True)
        elif cmd_str == 'mixer muting 0':
            await player.volume_mute(False)
        elif cmd_str == 'button volup':
            await player.volume_up()
        elif cmd_str == 'button voldown':
            await player.volume_down()
        elif cmd_str == 'button power':
            await player.power_toggle()
        else:
            return web.Response(text='command not supported')
        return web.Response(text='success')

    async def __media_items_from_body(self, data):
        """Helper to turn posted body data into media items."""
        if not isinstance(data, list):
            data = [data]
        media_items = []
        for item in data:
            media_item = await self.mass.music.item(item['item_id'],
                                                    item['media_type'],
                                                    item['provider'],
                                                    lazy=True)
            media_items.append(media_item)
        return media_items

    async def __stream_json(self, request, iterator):
        """ stream items from async iterator as json object """
        resp = web.StreamResponse(status=200,
                                  reason='OK',
                                  headers={'Content-Type': 'application/json'})
        await resp.prepare(request)
        # write json open tag
        json_response = '{ "items": ['
        await resp.write(json_response.encode('utf-8'))
        count = 0
        async for item in iterator:
            # write each item into the items object of the json
            if count:
                json_response = ',' + json_serializer(item)
            else:
                json_response = json_serializer(item)
            await resp.write(json_response.encode('utf-8'))
            count += 1
        # write json close tag
        json_response = '], "count": %s }' % count
        await resp.write((json_response).encode('utf-8'))
        await resp.write_eof()
        return resp
