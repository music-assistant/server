#!/usr/bin/env python3
# -*- coding:utf-8 -*-

import asyncio
import os
from utils import run_periodic, LOGGER
import json
import aiohttp
from aiohttp import web
from models import MediaType, media_type_from_string
from functools import partial
json_serializer = partial(json.dumps, default=lambda x: x.__dict__)


class Api():
    ''' expose our data through json api '''
    
    def __init__(self, mass):
        self.mass = mass
        self.http_session = aiohttp.ClientSession()

    def stop(self):
        self.runner.cleanup()
        self.http_session.close()

    async def setup_web(self):
        app = web.Application()
        app.add_routes([web.get('/ws', self.websocket_handler)])
        app.add_routes([web.get('/stream/{provider}/{track_id}', self.stream)])
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
        site = web.TCPSite(self.runner, '0.0.0.0', 8095)
        await site.start()

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
        if action:
            result = await self.mass.music.item_action(media_id, media_type, action)
        else:
            result = await self.mass.music.item(media_id, media_type, lazy=lazy)
        return web.json_response(result, dumps=json_serializer)

    async def artist_toptracks(self, request):
        ''' get top tracks for given artist '''
        artist_id = request.match_info.get('artist_id')
        result = await self.mass.music.artist_toptracks(artist_id)
        return web.json_response(result, dumps=json_serializer)

    async def artist_albums(self, request):
        ''' get (all) albums for given artist '''
        artist_id = request.match_info.get('artist_id')
        result = await self.mass.music.artist_albums(artist_id)
        return web.json_response(result, dumps=json_serializer)

    async def playlist_tracks(self, request):
        ''' get playlist tracks from provider'''
        playlist_id = request.match_info.get('playlist_id')
        limit = int(request.query.get('limit', 50))
        offset = int(request.query.get('offset', 0))
        result = await self.mass.music.playlist_tracks(playlist_id, offset=offset, limit=limit)
        return web.json_response(result, dumps=json_serializer)

    async def album_tracks(self, request):
        ''' get album tracks from provider'''
        album_id = request.match_info.get('album_id')
        result = await self.mass.music.album_tracks(album_id)
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
        media_item = await self.mass.music.item(media_id, media_type, lazy=True)
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
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        # register callback for internal events
        async def send_event(msg, msg_details):
            ws_msg = {"message": msg, "message_details": msg_details }
            try:
                await ws.send_json(ws_msg, dumps=json_serializer)
            except Exception as exc:
                if 'the handler is closed' in str(exc):
                    await self.mass.remove_event_listener(cb_id)
                else:
                    LOGGER.exception(exc)

        cb_id = await self.mass.add_event_listener(send_event)
        # process incoming messages
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                if msg.data == 'close':
                    await self.mass.remove_event_listener(cb_id)
                    await ws.close()
                else:
                    # for now we only use WS for player commands
                    if msg.data == 'players':
                        players = await self.mass.player.players()
                        ws_msg = {'message': 'players', 'message_details': players}
                        await ws.send_json(ws_msg, dumps=json_serializer)
                    elif msg.data.startswith('players') and '/play_media/' in msg.data:
                        #'players/{player_id}/play_media/{media_type}/{media_id}/{queue_opt}'
                        msg_data_parts = msg.data.split('/')
                        player_id = msg_data_parts[1]
                        media_type = msg_data_parts[3]
                        media_type = media_type_from_string(media_type)
                        media_id = msg_data_parts[4]
                        queue_opt = msg_data_parts[5] if len(msg_data_parts) == 6 else 'replace'
                        media_item = await self.mass.music.item(media_id, media_type, lazy=True)
                        await self.mass.player.play_media(player_id, media_item, queue_opt)

                    elif msg.data.startswith('players') and '/cmd/' in msg.data:
                        # players/{player_id}/cmd/{cmd} or players/{player_id}/cmd/{cmd}/{cmd_args}
                        msg_data_parts = msg.data.split('/')
                        player_id = msg_data_parts[1]
                        cmd = msg_data_parts[3]
                        cmd_args = msg_data_parts[4] if len(msg_data_parts) == 5 else None
                        await self.mass.player.player_command(player_id, cmd, cmd_args)
            elif msg.type == aiohttp.WSMsgType.ERROR:
                LOGGER.error('ws connection closed with exception %s' %
                    ws.exception())
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

    async def stream(self, request):
        ''' start streaming audio from provider '''
        track_id = request.match_info.get('track_id')
        provider = request.match_info.get('provider')
        stream_details = await self.mass.music.providers[provider].get_stream_details(track_id)
        resp = web.StreamResponse(status=200,
                                reason='OK',
                                headers={'Content-Type': stream_details['mime_type']})
        await resp.prepare(request)
        async for chunk in self.mass.music.providers[provider].get_stream(track_id):
            await resp.write(chunk)
        return resp