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
import slugify as slug

'''
    Homeassistant integration
    allows publishing of our players to hass
    allows using hass entities (like switches, media_players or gui inputs) to be triggered
'''

def setup(mass):
    ''' setup the module and read/apply config'''
    create_config_entries(mass.config)
    conf = mass.config['base']['homeassistant']
    enabled = conf.get(CONF_ENABLED)
    token = conf.get('token')
    url = conf.get('url')
    if enabled and url and token:
        return HomeAssistant(mass, url, token)
    return None

def create_config_entries(config):
    ''' get the config entries for this module (list with key/value pairs)'''
    config_entries = [
        (CONF_ENABLED, False, CONF_ENABLED),
        ('url', 'localhost', 'URL to homeassistant (e.g. https://homeassistant:8123)'), 
        ('token', '<password>', 'Long Lived Access Token'),
        ('publish_players', True, 'Publish players to Home Assistant')
        ]
    if not config['base'].get('homeassistant'):
        config['base']['homeassistant'] = {}
    config['base']['homeassistant']['__desc__'] = config_entries
    for key, def_value, desc in config_entries:
        if not key in config['base']['homeassistant']:
            config['base']['homeassistant'][key] = def_value
    # append hass player config settings
    if config['base']['homeassistant'][CONF_ENABLED]:
        hass_player_conf = [("hass_power_entity", "", "Attach player power to homeassistant entity"),
                        ("hass_power_entity_source", "", "Source on the homeassistant entity (optional)"),
                        ("hass_volume_entity", "", "Attach player volume to homeassistant entity")]
        for key, default, desc in hass_player_conf:
            entry_found = False
            for value in config['player_settings']['__desc__']:
                if value[0] == key:
                    entry_found = True
                    break
            if not entry_found:
                config['player_settings']['__desc__'].append((key, default, desc))

class HomeAssistant():
    ''' HomeAssistant integration '''

    def __init__(self, mass, url, token):
        self.mass = mass
        self._published_players = {}
        self._tracked_states = {}
        self._state_listeners = []
        self._sources = []
        self._token = token
        if url.startswith('https://'):
            self._use_ssl = True
            self._host = url.replace('https://','').split('/')[0]
        else:
            self._use_ssl = False
            self._host = url.replace('http://','').split('/')[0]
        self.http_session = aiohttp.ClientSession(loop=mass.event_loop, connector=aiohttp.TCPConnector(verify_ssl=False))
        self.__send_ws = None
        self.__last_id = 10
        LOGGER.info('Homeassistant integration is enabled')
        mass.event_loop.create_task(self.__hass_websocket())
        mass.event_loop.create_task(self.mass.add_event_listener(self.mass_event))
        mass.event_loop.create_task(self.__get_sources())

    async def get_state(self, entity_id, attribute='state', register_listener=None):
        ''' get state of a hass entity'''
        if entity_id in self._tracked_states:
            state_obj = self._tracked_states[entity_id]
        else:
            # first request
            state_obj = await self.__get_data('states/%s' % entity_id)
            if register_listener:
                # register state listener
                self._state_listeners.append( (entity_id, register_listener) )
            self._tracked_states[entity_id] = state_obj
        if attribute == 'state':
            return state_obj['state']
        elif not attribute:
            return state_obj
        else:
            return state_obj['attributes'].get(attribute)
    
    async def mass_event(self, msg, msg_details):
        ''' received event from mass '''
        if msg == "player updated":
            await self.publish_player(msg_details)

    async def hass_event(self, event_type, event_data):
        ''' received event from hass '''
        if event_type == 'state_changed':
            if event_data['entity_id'] in self._tracked_states:
                self._tracked_states[event_data['entity_id']] = event_data['new_state']
                for entity_id, handler in self._state_listeners:
                    if entity_id == event_data['entity_id']:
                        asyncio.create_task(handler())
        elif event_type == 'call_service' and event_data['domain'] == 'media_player':
            await self.__handle_player_command(event_data['service'], event_data['service_data'])

    async def __handle_player_command(self, service, service_data):
        ''' handle forwarded service call for one of our players '''
        if isinstance(service_data['entity_id'], list):
            # can be a list of entity ids if action fired on multiple items
            entity_ids = service_data['entity_id']
        else:
            entity_ids = [service_data['entity_id']]
        for entity_id in entity_ids:
            if entity_id in self._published_players:
                # call is for one of our players so handle it
                player_id = self._published_players[entity_id]
                if service == 'turn_on':
                    await self.mass.player.player_command(player_id, 'power', 'on')
                elif service == 'turn_off':
                    await self.mass.player.player_command(player_id, 'power', 'off')
                elif service == 'toggle':
                    await self.mass.player.player_command(player_id, 'power', 'toggle')
                elif service == 'volume_mute':
                    args = 'on' if service_data['is_volume_muted'] else 'off'
                    await self.mass.player.player_command(player_id, 'mute', args)
                elif service == 'volume_up':
                    await self.mass.player.player_command(player_id, 'volume', 'up')
                elif service == 'volume_down':
                    await self.mass.player.player_command(player_id, 'volume', 'down')
                elif service == 'volume_set':
                    volume_level = service_data['volume_level']*100
                    await self.mass.player.player_command(player_id, 'volume', volume_level)
                elif service == 'media_play':
                    await self.mass.player.player_command(player_id, 'play')
                elif service == 'media_pause':
                    await self.mass.player.player_command(player_id, 'pause')
                elif service == 'media_stop':
                    await self.mass.player.player_command(player_id, 'stop')
                elif service == 'media_next_track':
                    await self.mass.player.player_command(player_id, 'next')
                elif service == 'media_play_pause':
                    await self.mass.player.player_command(player_id, 'pause', 'toggle')
                elif service == 'play_media':
                    return await self.__handle_play_media(player_id, service_data)

    async def __handle_play_media(self, player_id, service_data):
        ''' handle play_media request from homeassistant'''
        media_content_type = service_data['media_content_type'].lower()
        media_content_id = service_data['media_content_id']
        queue_opt = 'add' if service_data.get('enqueue') else 'play'
        if media_content_type == 'playlist' and not '://' in media_content_id:
            media_items = []
            for playlist_str in media_content_id.split(','):
                playlist_str = playlist_str.strip()
                playlist = await self.mass.music.playlist_by_name(playlist_str)
                if playlist:
                    media_items.append(playlist)
            return await self.mass.player.play_media(player_id, media_items, queue_opt)
        elif media_content_type == 'playlist' and 'spotify://playlist' in media_content_id:
            # TODO: handle parsing of other uri's here
            playlist = self.mass.music.providers['spotify'].playlist(media_content_id.split(':')[-1])
            return await self.mass.player.play_media(player_id, playlist, queue_opt)
        elif media_content_id.startswith('http'):
            track = Track()
            track.uri = media_content_id
            track.provider = 'http'
            return await self.mass.player.play_media(player_id, track, queue_opt)
    
    async def publish_player(self, player):
        ''' publish player details to hass'''
        if not self.mass.config['base']['homeassistant']['publish_players']:
            return False
        player_id = player.player_id
        entity_id = 'media_player.mass_' + slug.slugify(player.name, separator='_').lower()
        state = player.state if player.powered else 'off'
        state_attributes = {
                "supported_features": 65471, 
                "friendly_name": player.name,
                "source_list": self._sources,
                "source": 'unknown',
                "volume_level": player.volume_level/100,
                "is_volume_muted": player.muted,
                "media_duration": player.cur_item.duration if player.cur_item else 0,
                "media_position": player.cur_item_time,
                "media_title": player.cur_item.name if player.cur_item else "",
                "media_artist": player.cur_item.artists[0].name if player.cur_item and player.cur_item.artists else "",
                "media_album_name": player.cur_item.album.name if player.cur_item and player.cur_item.album else "",
                "entity_picture": player.cur_item.album.metadata.get('image') if player.cur_item and player.cur_item.album else ""
                }
        self._published_players[entity_id] = player_id
        await self.__set_state(entity_id, state, state_attributes)

    async def call_service(self, domain, service, service_data=None):
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

    @run_periodic(120)
    async def __get_sources(self):
        ''' we build a list of all playlists to use as player sources '''
        self._sources = [playlist.name for playlist in await self.mass.music.playlists()]

    async def __set_state(self, entity_id, new_state, state_attributes={}):
        ''' set state to hass entity '''
        data = {
            "state": new_state,
            "entity_id": entity_id,
            "attributes": state_attributes
            }
        return await self.__post_data('states/%s' % entity_id, data)
    
    async def __hass_websocket(self):
        ''' Receive events from Hass through websockets '''
        while self.mass.event_loop.is_running():
            try:
                protocol = 'wss' if self._use_ssl else 'ws'
                async with self.http_session.ws_connect('%s://%s/api/websocket' % (protocol, self._host)) as ws:
                    
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
                                    subscribe_msg = {"type": "subscribe_events", "event_type": "call_service"}
                                    await send_msg(subscribe_msg)
                                elif data['type'] == 'event':
                                    asyncio.create_task(self.hass_event(data['event']['event_type'], data['event']['data']))
                                elif data['type'] == 'result' and data.get('result'):
                                    # reply to our get_states request
                                    asyncio.create_task(self.hass_event('all_states', data['result']))
                                else:
                                    LOGGER.info(data)
                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            break
            except Exception as exc:
                LOGGER.exception(exc)
                await asyncio.sleep(10)

    async def __get_data(self, endpoint):
        ''' get data from hass rest api'''
        url = "http://%s/api/%s" % (self._host, endpoint)
        if self._use_ssl:
            url = "https://%s/api/%s" % (self._host, endpoint)
        headers = {"Authorization": "Bearer %s" % self._token, "Content-Type": "application/json"}
        async with self.http_session.get(url, headers=headers) as response:
            return await response.json()

    async def __post_data(self, endpoint, data):
        ''' post data to hass rest api'''
        url = "http://%s/api/%s" % (self._host, endpoint)
        if self._use_ssl:
            url = "https://%s/api/%s" % (self._host, endpoint)
        headers = {"Authorization": "Bearer %s" % self._token, "Content-Type": "application/json"}
        async with self.http_session.post(url, headers=headers, json=data) as response:
            return await response.json()