#!/usr/bin/env python3
# -*- coding:utf-8 -*-

import asyncio
import os
from typing import List
import random
import sys
sys.path.append("..")
from utils import run_periodic, LOGGER, parse_track_title, try_parse_int
from models import PlayerProvider, MusicPlayer, PlayerState, MediaType, TrackQuality, AlbumType, Artist, Album, Track, Playlist
from constants import CONF_ENABLED, CONF_HOSTNAME, CONF_PORT
import json
import aiohttp
import time
import datetime
import hashlib
from asyncio_throttle import Throttler
from aiocometd import Client, ConnectionType, Extension
from cache import use_cache
import copy

def setup(mass):
    ''' setup the provider'''
    enabled = mass.config["playerproviders"]['homeassistant'].get(CONF_ENABLED)
    token = mass.config["playerproviders"]['homeassistant'].get('token')
    hostname = mass.config["playerproviders"]['homeassistant'].get(CONF_HOSTNAME)
    if enabled and hostname and token:
        provider = HassProvider(mass, hostname, token)
        return provider
    return False

def config_entries():
    ''' get the config entries for this provider (list with key/value pairs)'''
    return [
        (CONF_ENABLED, False, CONF_ENABLED),
        (CONF_HOSTNAME, 'localhost', CONF_HOSTNAME), 
        ('token', '<password>', 'Long Lived Access Token')
        ]

class HassProvider(PlayerProvider):
    ''' support for Home Assistant '''

    def __init__(self, mass, hostname, token):
        self.prov_id = 'homeassistant'
        self.name = 'Home Assistant'
        self.icon = ''
        self.mass = mass
        self._players = {}
        self._token = token
        self._host = hostname
        self.supports_queue = False
        self.supports_http_stream = True # whether we can fallback to http streaming
        self.supported_musicproviders = [] # we have no idea about the mediaplayers attached to hass so assume we can only do http playback
        self.http_session = aiohttp.ClientSession(loop=mass.event_loop)
        self.__send_ws = None
        self.__last_id = 10
        asyncio.ensure_future(self.__hass_connect())
        

    ### Provider specific implementation #####

    async def player_command(self, player_id, cmd:str, cmd_args=None):
        ''' issue command on player (play, pause, next, previous, stop, power, volume, mute) '''
        service_data = {"entity_id": player_id}
        service = None
        if cmd == 'play':
            service = 'media_play'
        elif cmd == 'pause':
            service = 'media_pause'
        elif cmd == 'stop':
            service = 'media_stop'
        elif cmd == 'next':
            service = 'media_next_track'
        elif cmd == 'previous':
            service = 'media_previous_track'
        elif cmd == 'power' and cmd_args in ['on', '1', 1]:
            service = 'turn_on'
        elif cmd == 'power' and cmd_args in ['off', '0', 0]:
            service = 'turn_off'
        elif cmd == 'volume' and cmd_args == 'up':
            service = 'volume_up'
        elif cmd == 'volume' and cmd_args == 'down':
            service = 'volume_down'
        elif cmd == 'volume':
            service = 'volume_set'
            service_data['volume_level'] = try_parse_int(cmd_args) / 100
            self._players[player_id].volume_level = try_parse_int(cmd_args)
        elif cmd == 'mute' and cmd_args in ['on', '1', 1]:
            service = 'volume_mute'
            service_data['is_volume_muted'] = True
        elif cmd == 'mute' and cmd_args in ['off', '0', 0]:
            service = 'volume_mute'
            service_data['is_volume_muted'] = False
        return await self.__call_service(service, service_data)

    async def play_media(self, player_id, uri, queue_opt='play'):
        ''' 
            play media on a player
            params:
            - player_id: id of the player
            - uri: the uri for/to the media item (e.g. spotify:track:1234 or http://pathtostream)
            - queue_opt: 
                replace: replace whatever is currently playing with this media
                next: the given media will be played after the currently playing track
                add: add to the end of the queue
                play: keep existing queue but play the given item now
        '''
        service = "play_media"
        service_data = {
            "entity_id": player_id,
            "media_content_id": uri,
            "media_content_type": "music"
            }
        return await self.__call_service(service, service_data)

    async def __call_service(self, service, service_data=None, domain='media_player'):
        ''' call service on hass '''
        if not self.__send_ws:
            return False
        msg = {
            "type": "call_service",
            "domain": domain,
            "service": service,
            }
        if service_data:
            msg['service_data'] = service_data
        return await self.__send_ws(msg)
    
    ### Provider specific (helper) methods #####
    
    async def __handle_player_state(self, data):
        ''' handle a player state message from the websockets '''
        player_id = data['entity_id']
        if not player_id in self._players:
            # new player
            self._players[player_id] = MusicPlayer()
            player = self._players[player_id]
            player.player_id = player_id
            player.player_provider = self.prov_id
        else: 
            # existing player
            player = self._players[player_id]
        # always update player details that may change
        player.name = data['attributes']['friendly_name']
        player.powered = not data['state'] == 'off'
        if data['state'] == 'playing':
            player.state == PlayerState.Playing
        elif data['state'] == 'paused':
            player.state == PlayerState.Paused
        else:
            player.state = PlayerState.Stopped
        if 'is_volume_muted' in data['attributes']:
            player.muted = data['attributes']['is_volume_muted']
        if 'volume_level' in data['attributes']:
            player.volume_level = float(data['attributes']['volume_level']) * 100
        if 'media_position' in data['attributes']:
            player.cur_item_time = try_parse_int(data['attributes']['media_position'])
        player.cur_item = await self.__parse_track(data)
        await self.mass.player.update_player(player)

    async def __parse_track(self, data):
        ''' parse track in hass to our internal format '''
        track = Track()
        # TODO: match this info in the DB!
        if 'media_content_id' in data['attributes']:
            artist = data['attributes'].get('media_artist')
            album = data['attributes'].get('media_album')
            title = data['attributes'].get('media_title')
            track.name = "%s - %s" %(artist, title)
            if 'entity_picture' in data['attributes']:
                img = "https://%s%s" %(self._host, data['attributes']['entity_picture'])
                track.metadata['image'] = img
            track.duration = try_parse_int(data['attributes'].get('media_duration',0))
        return track

    async def __hass_connect(self):
        ''' Receive events from Hass through websockets '''
        while True:
            try:
                async with self.http_session.ws_connect('wss://%s/api/websocket' % self._host) as ws:
                    
                    async def send_msg(msg):
                        ''' callback to send message to the websockets client'''
                        self.__last_id += 1
                        msg['id'] = self.__last_id
                        await ws.send_json(msg)

                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            if msg.data == 'close cmd':
                                await ws.close()
                                break
                            else:
                                data = msg.json()
                                if data['type'] == 'auth_required':
                                    # send auth token
                                    auth_msg = {"type": "auth", "access_token": self._token}
                                    await ws.send_json(auth_msg)
                                elif data['type'] == 'auth_invalid':
                                    raise Exception(data)
                                elif data['type'] == 'auth_ok':
                                    # register callback
                                    self.__send_ws = send_msg
                                    # subscribe to events
                                    subscribe_msg = {"type": "subscribe_events", "event_type": "state_changed"}
                                    await send_msg(subscribe_msg)
                                    subscribe_msg = {"type": "get_states"}
                                    await send_msg(subscribe_msg)
                                elif data['type'] == 'event' and data['event']['event_type'] == 'state_changed':
                                    if data['event']['data']['entity_id'].startswith('media_player'):
                                        asyncio.ensure_future(self.__handle_player_state(data['event']['data']['new_state']))
                                elif data['type'] == 'result' and data.get('result'):
                                    # reply to our get_states request
                                    for item in data['result']:
                                        if item['entity_id'].startswith('media_player'):
                                            asyncio.ensure_future(self.__handle_player_state(item))
                                else:
                                    LOGGER.info(data)
                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            break
            except Exception as exc:
                LOGGER.exception(exc)
                asyncio.sleep(10)