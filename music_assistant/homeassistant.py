#!/usr/bin/env python3
# -*- coding:utf-8 -*-

import asyncio
import os
from typing import List
import random
import aiohttp
import time
import datetime
import hashlib
from asyncio_throttle import Throttler
from aiocometd import Client, ConnectionType, Extension
import copy
import slugify as slug
import json
from .utils import run_periodic, LOGGER, parse_track_title, try_parse_int
from .models.media_types import Track
from .constants import CONF_ENABLED, CONF_URL, CONF_TOKEN, EVENT_PLAYER_CHANGED, EVENT_PLAYER_ADDED, EVENT_HASS_ENTITY_CHANGED
from .cache import use_cache

CONF_KEY = 'homeassistant'
CONF_PUBLISH_PLAYERS = "publish_players"

### auto detect hassio for auto config ####
if os.path.isfile('/data/options.json'):
    IS_HASSIO = True
    CONFIG_ENTRIES = [
        (CONF_ENABLED, False, CONF_ENABLED),
        (CONF_PUBLISH_PLAYERS, True, 'hass_publish')]
else:
    IS_HASSIO = False
    CONFIG_ENTRIES = [
        (CONF_ENABLED, False, CONF_ENABLED),
        (CONF_URL, 'localhost', 'hass_url'), 
        (CONF_TOKEN, '<password>', 'hass_token'),
        (CONF_PUBLISH_PLAYERS, True, 'hass_publish')]
    

class HomeAssistant():
    '''
        Homeassistant integration
        allows publishing of our players to hass
        allows using hass entities (like switches, media_players or gui inputs) to be triggered
    '''

    def __init__(self, mass):
        self.mass = mass
        self._published_players = {}
        self._tracked_entities = {}
        self._state_listeners = {}
        self._sources = []
        self.__send_ws = None
        self.__last_id = 10
        # load/create/update config
        config = self.mass.config.create_module_config(CONF_KEY, CONFIG_ENTRIES)
        self.enabled = config[CONF_ENABLED]
        if (self.enabled and not IS_HASSIO and not 
                (config[CONF_URL] or config[CONF_TOKEN])):
            LOGGER.warning("Invalid configuration for Home Assistant")
            self.enabled = False
        if IS_HASSIO:
            self._token = os.environ['HASSIO_TOKEN']
            self._use_ssl = False
            self._host = 'hassio/homeassistant'
        else:
            self._token = config[CONF_TOKEN]
            url = config[CONF_URL]
            if url.startswith('https://'):
                self._use_ssl = True
                self._host = url.replace('https://','').split('/')[0]
            else:
                self._use_ssl = False
                self._host = url.replace('http://','').split('/')[0]
        if self.enabled:
            LOGGER.info('Homeassistant integration is enabled')

    async def setup(self):
        ''' perform async setup '''
        if not self.enabled:
            return
        self.http_session = aiohttp.ClientSession(
                loop=self.mass.event_loop, connector=aiohttp.TCPConnector())
        self.mass.event_loop.create_task(self.__hass_websocket())
        await self.mass.add_event_listener(self.mass_event, EVENT_PLAYER_CHANGED)
        await self.mass.add_event_listener(self.mass_event, EVENT_PLAYER_ADDED)
        self.mass.event_loop.create_task(self.__get_sources())

    async def get_state_async(self, entity_id, attribute='state'):
        ''' get state of a hass entity (async)'''
        state = self.get_state(entity_id, attribute)
        if not state:
            await self.__request_state(entity_id)
        state = self.get_state(entity_id, attribute)
        return state

    def get_state(self, entity_id, attribute='state'):
        ''' get state of a hass entity'''
        state_obj = self._tracked_entities.get(entity_id)
        if state_obj:
            if attribute == 'state':
                return state_obj['state']
            elif attribute:
                return state_obj['attributes'].get(attribute)
            else:
                return state_obj
        else:
            self.mass.event_loop.create_task(self.__request_state(entity_id))
            return None

    async def __request_state(self, entity_id):
        ''' get state of a hass entity'''
        state_obj = await self.__get_data('states/%s' % entity_id)
        if 'state' in state_obj:
            self._tracked_entities[entity_id] = state_obj
            self.mass.event_loop.create_task(
                self.mass.signal_event(EVENT_HASS_ENTITY_CHANGED, state_obj))
    
    async def mass_event(self, msg, msg_details):
        ''' received event from mass '''
        if msg in [EVENT_PLAYER_CHANGED, EVENT_PLAYER_ADDED]:
            await self.publish_player(msg_details)

    async def hass_event(self, event_type, event_data):
        ''' received event from hass '''
        if event_type == 'state_changed':
            if event_data['entity_id'] in self._tracked_entities:
                self._tracked_entities[event_data['entity_id']] = event_data['new_state']
                self.mass.event_loop.create_task(
                    self.mass.signal_event(EVENT_HASS_ENTITY_CHANGED, event_data))
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
                player = await self.mass.players.get_player(player_id)
                if service == 'turn_on':
                    await player.power_on()
                elif service == 'turn_off':
                    await player.power_off()
                elif service == 'toggle':
                    await player.power_toggle()
                elif service == 'volume_mute':
                    await player.volume_mute(service_data['is_volume_muted'])
                elif service == 'volume_up':
                    await player.volume_up()
                elif service == 'volume_down':
                    await player.volume_down()
                elif service == 'volume_set':
                    volume_level = service_data['volume_level']*100
                    await player.volume_set(volume_level)
                elif service == 'media_play':
                    await player.play()
                elif service == 'media_pause':
                    await player.pause()
                elif service == 'media_stop':
                    await player.stop()
                elif service == 'media_next_track':
                    await player.next()
                elif service == 'media_play_pause':
                    await player.play_pause()
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
            return await self.mass.players.play_media(player_id, media_items, queue_opt)
        elif media_content_type == 'playlist' and 'spotify://playlist' in media_content_id:
            # TODO: handle parsing of other uri's here
            playlist = self.mass.music.providers['spotify'].playlist(media_content_id.split(':')[-1])
            return await self.mass.players.play_media(player_id, playlist, queue_opt)
        elif media_content_id.startswith('http'):
            track = Track()
            track.uri = media_content_id
            track.provider = 'http'
            return await self.mass.players.play_media(player_id, track, queue_opt)
    
    async def publish_player(self, player_info):
        ''' publish player details to hass'''
        if not self.mass.config['base']['homeassistant']['publish_players']:
            return False
        if not player_info["name"]:
            return
        # TODO: throttle updates to home assistant ?
        player_id = player_info["player_id"]
        entity_id = 'media_player.mass_' + slug.slugify(player_info["name"], separator='_').lower()
        state = player_info["state"]
        state_attributes = {
                "supported_features": 65471, 
                "friendly_name": player_info["name"],
                "source_list": self._sources,
                "source": 'unknown',
                "volume_level": player_info["volume_level"]/100,
                "is_volume_muted": player_info["muted"],
                "media_position_updated_at": player_info["media_position_updated_at"],
                "media_duration": None,
                "media_position": player_info["cur_time"],
                "media_title": None,
                "media_artist": None,
                "media_album_name": None,
                "entity_picture": None
                }
        if state != "off":
            player = await self.mass.players.get_player(player_id)
            if player.queue.cur_item:
                queue_item = await player.queue.by_item_id(player.queue.cur_item)
                state_attributes["media_duration"] = queue_item.duration
                state_attributes["media_title"] = queue_item.name
                state_attributes["media_artist"] = queue_item.artists[0].name
                state_attributes["media_album_name"] = queue_item.album.name
                state_attributes["entity_picture"] = queue_item.album.metadata.get("image")
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
                async with self.http_session.ws_connect('%s://%s/api/websocket' % (protocol, self._host), verify_ssl=False) as ws:
                    
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
                                # else:
                                #     LOGGER.info(data)
                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            raise Exception("error in websocket")
            except Exception as exc:
                LOGGER.exception(exc)
                await asyncio.sleep(10)

    async def __get_data(self, endpoint):
        ''' get data from hass rest api'''
        url = "http://%s/api/%s" % (self._host, endpoint)
        if self._use_ssl:
            url = "https://%s/api/%s" % (self._host, endpoint)
        headers = {"Authorization": "Bearer %s" % self._token, "Content-Type": "application/json"}
        async with self.http_session.get(url, headers=headers, verify_ssl=False) as response:
            return await response.json()

    async def __post_data(self, endpoint, data):
        ''' post data to hass rest api'''
        url = "http://%s/api/%s" % (self._host, endpoint)
        if self._use_ssl:
            url = "https://%s/api/%s" % (self._host, endpoint)
        headers = {"Authorization": "Bearer %s" % self._token, "Content-Type": "application/json"}
        async with self.http_session.post(url, headers=headers, json=data, verify_ssl=False) as response:
            return await response.json()