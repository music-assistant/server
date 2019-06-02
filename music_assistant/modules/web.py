#!/usr/bin/env python3
# -*- coding:utf-8 -*-

import asyncio
import os
from utils import run_periodic, LOGGER, run_async_background_task
import json
import aiohttp
from aiohttp import web
from models import MediaType, media_type_from_string
from functools import partial
json_serializer = partial(json.dumps, default=lambda x: x.__dict__)
import ssl
import concurrent
import threading

def setup(mass):
    ''' setup the module and read/apply config'''
    create_config_entries(mass.config)
    conf = mass.config['base']['web']
    if conf['ssl_certificate'] and os.path.isfile(conf['ssl_certificate']):
        ssl_cert = conf['ssl_certificate']
    else:
        ssl_cert = ''
    if conf['ssl_key'] and os.path.isfile(conf['ssl_key']):
        ssl_key = conf['ssl_key']
    else:
        ssl_key = ''
    hostname = conf['hostname']
    http_port = conf['http_port']
    https_port = conf['https_port']
    return Web(mass, http_port, https_port, ssl_cert, ssl_key, hostname)

def create_config_entries(config):
    ''' get the config entries for this module (list with key/value pairs)'''
    config_entries = [
        ('http_port', 8095, 'web_http_port'),
        ('https_port', 8096, 'web_https_port'),
        ('ssl_certificate', '', 'web_ssl_cert'), 
        ('ssl_key', '', 'web_ssl_key'),
        ('cert_fqdn_host', '', 'cert_fqdn_host')
        ]
    if not config['base'].get('web'):
        config['base']['web'] = {}
    config['base']['web']['__desc__'] = config_entries
    for key, def_value, desc in config_entries:
        if not key in config['base']['web']:
            config['base']['web'][key] = def_value

class Web():
    ''' webserver and json/websocket api '''
    
    def __init__(self, mass, http_port, https_port, ssl_cert, ssl_key, cert_fqdn_host):
        self.mass = mass
        self._http_port = http_port
        self._https_port = https_port
        self._ssl_cert = ssl_cert
        self._ssl_key = ssl_key
        self._cert_fqdn_host = cert_fqdn_host
        self.http_session = aiohttp.ClientSession()
        mass.event_loop.create_task(self.setup_web())

    def stop(self):
        asyncio.create_task(self.runner.cleanup())
        asyncio.create_task(self.http_session.close())

    async def setup_web(self):
        app = web.Application()
        app.add_routes([web.get('/jsonrpc.js', self.json_rpc)])
        app.add_routes([web.post('/jsonrpc.js', self.json_rpc)])
        app.add_routes([web.get('/ws', self.websocket_handler)])
        app.add_routes([web.get('/stream_track', self.mass.http_streamer.stream_track)])
        app.add_routes([web.get('/stream_radio', self.mass.http_streamer.stream_radio)])
        app.add_routes([web.get('/api/search', self.search)])
        app.add_routes([web.get('/api/config', self.get_config)])
        app.add_routes([web.post('/api/config', self.save_config)])
        app.add_routes([web.get('/api/players', self.players)])
        app.add_routes([web.get('/api/players/{player_id}/queue', self.player_queue)])
        app.add_routes([web.get('/api/players/{player_id}/cmd/{cmd}', self.player_command)])
        app.add_routes([web.get('/api/players/{player_id}/cmd/{cmd}/{cmd_args}', self.player_command)])
        app.add_routes([web.get('/api/players/{player_id}/play_media/{media_type}/{media_id}', self.play_media)])
        app.add_routes([web.get('/api/players/{player_id}/play_media/{media_type}/{media_id}/{queue_opt}', self.play_media)])
        app.add_routes([web.get('/api/playlists/{playlist_id}/tracks', self.playlist_tracks)])
        app.add_routes([web.get('/api/artists/{artist_id}/toptracks', self.artist_toptracks)])
        app.add_routes([web.get('/api/artists/{artist_id}/albums', self.artist_albums)])
        app.add_routes([web.get('/api/albums/{album_id}/tracks', self.album_tracks)])
        app.add_routes([web.get('/api/{media_type}', self.get_items)])
        app.add_routes([web.get('/api/{media_type}/{media_id}/{action}', self.get_item)])
        app.add_routes([web.get('/api/{media_type}/{media_id}', self.get_item)])
        app.add_routes([web.get('/', self.index)])
        app.router.add_static("/", "./web")  
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        http_site = web.TCPSite(self.runner, '0.0.0.0', self._http_port)
        await http_site.start()
        if self._ssl_cert and self._ssl_key:
            ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
            ssl_context.load_cert_chain(self._ssl_cert, self._ssl_key)
            https_site = web.TCPSite(self.runner, '0.0.0.0', self._https_port, ssl_context=ssl_context)
            await https_site.start()

    async def get_items(self, request):
        ''' get multiple library items'''
        media_type_str = request.match_info.get('media_type')
        media_type = media_type_from_string(media_type_str)
        limit = int(request.query.get('limit', 50))
        offset = int(request.query.get('offset', 0))
        orderby = request.query.get('orderby', 'name')
        provider_filter = request.rel_url.query.get('provider')
        result = await self.mass.music.library_items(media_type, 
                    limit=limit, offset=offset, 
                    orderby=orderby, provider_filter=provider_filter)
        return web.json_response(result, dumps=json_serializer)

    async def get_item(self, request):
        ''' get item full details'''
        media_type_str = request.match_info.get('media_type')
        media_type = media_type_from_string(media_type_str)
        media_id = request.match_info.get('media_id')
        action = request.match_info.get('action','')
        lazy = request.rel_url.query.get('lazy', '') != 'false'
        provider = request.rel_url.query.get('provider')
        if action:
            result = await self.mass.music.item_action(media_id, media_type, provider, action)
        else:
            result = await self.mass.music.item(media_id, media_type, provider, lazy=lazy)
        return web.json_response(result, dumps=json_serializer)

    async def artist_toptracks(self, request):
        ''' get top tracks for given artist '''
        artist_id = request.match_info.get('artist_id')
        provider = request.rel_url.query.get('provider')
        result = await self.mass.music.artist_toptracks(artist_id, provider)
        return web.json_response(result, dumps=json_serializer)

    async def artist_albums(self, request):
        ''' get (all) albums for given artist '''
        artist_id = request.match_info.get('artist_id')
        provider = request.rel_url.query.get('provider')
        result = await self.mass.music.artist_albums(artist_id, provider)
        return web.json_response(result, dumps=json_serializer)

    async def playlist_tracks(self, request):
        ''' get playlist tracks from provider'''
        playlist_id = request.match_info.get('playlist_id')
        limit = int(request.query.get('limit', 50))
        offset = int(request.query.get('offset', 0))
        provider = request.rel_url.query.get('provider')
        result = await self.mass.music.playlist_tracks(playlist_id, provider, offset=offset, limit=limit)
        return web.json_response(result, dumps=json_serializer)

    async def album_tracks(self, request):
        ''' get album tracks from provider'''
        album_id = request.match_info.get('album_id')
        provider = request.rel_url.query.get('provider')
        result = await self.mass.music.album_tracks(album_id, provider)
        return web.json_response(result, dumps=json_serializer)

    async def search(self, request):
        ''' search database or providers '''
        searchquery = request.rel_url.query.get('query')
        media_types_query = request.rel_url.query.get('media_types')
        limit = request.rel_url.query.get('media_id', 5)
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
        result = await self.mass.music.search(searchquery, media_types, limit=limit, online=online)
        return web.json_response(result, dumps=json_serializer)

    async def players(self, request):
        ''' get all players '''
        players = await self.mass.player.players()
        return web.json_response(players, dumps=json_serializer)

    async def player_command(self, request):
        ''' issue player command'''
        player_id = request.match_info.get('player_id')
        cmd = request.match_info.get('cmd')
        cmd_args = request.match_info.get('cmd_args')
        result = await self.mass.player.player_command(player_id, cmd, cmd_args)
        return web.json_response(result, dumps=json_serializer) 
    
    async def play_media(self, request):
        ''' issue player play_media command'''
        player_id = request.match_info.get('player_id')
        media_type_str = request.match_info.get('media_type')
        media_type = media_type_from_string(media_type_str)
        media_id = request.match_info.get('media_id')
        queue_opt = request.match_info.get('queue_opt','')
        provider = request.rel_url.query.get('provider')
        media_item = await self.mass.music.item(media_id, media_type, provider, lazy=True)
        result = await self.mass.player.play_media(player_id, media_item, queue_opt)
        return web.json_response(result, dumps=json_serializer) 
    
    async def player_queue(self, request):
        ''' return the items in the player's queue '''
        player_id = request.match_info.get('player_id')
        limit = int(request.query.get('limit', 50))
        offset = int(request.query.get('offset', 0))
        result = await self.mass.player.player_queue(player_id, offset, limit)
        return web.json_response(result, dumps=json_serializer) 
    
    async def index(self, request):  
        return web.FileResponse("./web/index.html")

    async def websocket_handler(self, request):
        ''' websockets handler '''
        cb_id = None
        ws = None
        try:
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            # register callback for internal events
            async def send_event(msg, msg_details):
                ws_msg = {"message": msg, "message_details": msg_details }
                await ws.send_json(ws_msg, dumps=json_serializer)
            cb_id = await self.mass.add_event_listener(send_event)
            # process incoming messages
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                # for now we only use WS for (simple) player commands
                if msg.data == 'players':
                    players = await self.mass.player.players()
                    ws_msg = {'message': 'players', 'message_details': players}
                    await ws.send_json(ws_msg, dumps=json_serializer)
                elif msg.data.startswith('players') and '/cmd/' in msg.data:
                    # players/{player_id}/cmd/{cmd} or players/{player_id}/cmd/{cmd}/{cmd_args}
                    msg_data_parts = msg.data.split('/')
                    player_id = msg_data_parts[1]
                    cmd = msg_data_parts[3]
                    cmd_args = msg_data_parts[4] if len(msg_data_parts) == 5 else None
                    await self.mass.player.player_command(player_id, cmd, cmd_args)
        finally:
            await self.mass.remove_event_listener(cb_id)
        LOGGER.info('websocket connection closed')
        return ws

    async def get_config(self, request):
        ''' get the config '''
        return web.json_response(self.mass.config)

    async def save_config(self, request):
        ''' save the config '''
        LOGGER.debug('save config called from api')
        new_config = await request.json()
        for key, value in self.mass.config.items():
            if isinstance(value, dict):
                for subkey, subvalue in value.items():
                    if subkey in new_config[key]:
                        self.mass.config[key][subkey] = new_config[key][subkey]
            elif key in new_config:
                self.mass.config[key] = new_config[key]
        self.mass.save_config()
        return web.Response(text='success')

    async def json_rpc(self, request):
        ''' 
            implement LMS jsonrpc interface 
            for some compatability with tools that talk to lms
            only support for basic commands
        '''
        data = await request.json()
        LOGGER.info("jsonrpc: %s" % data)
        params = data['params']
        player_id = params[0]
        cmds = params[1]
        cmd_str = " ".join(cmds)
        if cmd_str in ['play', 'pause', 'stop']:
            await self.mass.player.player_command(player_id, cmd_str)
        elif 'power' in cmd_str:
            args = cmds[1] if len(cmds) > 1 else None
            await self.mass.player.player_command(player_id, cmd_str, args)
        elif cmd_str == 'playlist index +1':
            await self.mass.player.player_command(player_id, 'next')
        elif cmd_str == 'playlist index -1':
            await self.mass.player.player_command(player_id, 'previous')
        elif 'mixer volume' in cmd_str:
            await self.mass.player.player_command(player_id, 'volume', cmds[2])
        elif cmd_str == 'mixer muting 1':
            await self.mass.player.player_command(player_id, 'mute', 'on')
        elif cmd_str == 'mixer muting 0':
            await self.mass.player.player_command(player_id, 'mute', 'off')
        elif cmd_str == 'button volup':
            await self.mass.player.player_command(player_id, 'volume', 'up')
        elif cmd_str == 'button voldown':
            await self.mass.player.player_command(player_id, 'volume', 'down')
        elif cmd_str == 'button power':
            await self.mass.player.player_command(player_id, 'power', 'toggle')
        return web.Response(text='success')
        