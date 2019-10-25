#!/usr/bin/env python3
# -*- coding:utf-8 -*-

import asyncio
import os
import struct
from collections import OrderedDict
import time
import decimal
from typing import List
import random
import sys
import socket
from ..utils import run_periodic, LOGGER, parse_track_title, try_parse_int, get_ip, get_hostname
from ..models import PlayerProvider, Player, PlayerState, MediaType, TrackQuality, AlbumType, Artist, Album, Track, Playlist
from ..constants import CONF_ENABLED


PROV_ID = 'webplayer'
PROV_NAME = 'WebPlayer'
PROV_CLASS = 'WebPlayerProvider'

CONFIG_ENTRIES = [
    (CONF_ENABLED, True, CONF_ENABLED),
    ]

PLAYER_CONFIG_ENTRIES = []

EVENT_WEBPLAYER_CMD = 'webplayer command'
EVENT_WEBPLAYER_STATE = 'webplayer state'
EVENT_WEBPLAYER_REGISTER = 'webplayer register'

class WebPlayerProvider(PlayerProvider):
    ''' 
        Implementation of a player using pure HTML/javascript
        used in the front-end.
        Communication is handled through the websocket connection
        and our internal event bus
    '''

    def __init__(self, mass, conf):
        super().__init__(mass, conf)
        self.prov_id = PROV_ID
        self.name = PROV_NAME
        self.player_config_entries = PLAYER_CONFIG_ENTRIES

     ### Provider specific implementation #####

    async def setup(self):
        ''' async initialize of module '''
        await self.mass.add_event_listener(self.handle_mass_event, EVENT_WEBPLAYER_STATE)
        await self.mass.add_event_listener(self.handle_mass_event, EVENT_WEBPLAYER_REGISTER)
        self.mass.event_loop.create_task(self.check_players())

    async def handle_mass_event(self, msg, msg_details):
        ''' received event for the webplayer component '''
        if msg == EVENT_WEBPLAYER_REGISTER:
            # register new player
            player_id = msg_details['player_id']
            player = WebPlayer(self.mass, player_id, self.prov_id)
            player.supports_crossfade = False
            player.supports_gapless = False
            player.supports_queue = False
            player.name = msg_details['name']
            await self.add_player(player)
        elif msg == EVENT_WEBPLAYER_STATE:
            player_id = msg_details['player_id']
            player = await self.get_player(player_id)
            if player:
                await player.handle_state(msg_details)

    @run_periodic(30)
    async def check_players(self):
        ''' invalidate players that did not send a heartbeat message in a while '''
        cur_time = time.time()
        offline_players = []
        for player in self.players:
            if cur_time - player._last_message > 30:
                offline_players.append(player.player_id)
        for player_id in offline_players:
            await self.remove_player(player_id)


class WebPlayer(Player):
    ''' Web player object '''

    def __init__(self, mass, player_id, prov_id):
        self._last_message = time.time()
        super().__init__(mass, player_id, prov_id)

    async def cmd_stop(self):
        ''' send stop command to player '''
        data = { 'player_id': self.player_id, 'cmd': 'stop'}
        await self.mass.signal_event(EVENT_WEBPLAYER_CMD, data)

    async def cmd_play(self):
        ''' send play command to player '''
        data = { 'player_id': self.player_id, 'cmd': 'play'}
        await self.mass.signal_event(EVENT_WEBPLAYER_CMD, data)

    async def cmd_pause(self):
        ''' send pause command to player '''
        data = { 'player_id': self.player_id, 'cmd': 'pause'}
        await self.mass.signal_event(EVENT_WEBPLAYER_CMD, data)
    
    async def cmd_power_on(self):
        ''' send power ON command to player '''
        self.powered = True # not supported on webplayer
        data = { 'player_id': self.player_id, 'cmd': 'stop'}
        await self.mass.signal_event(EVENT_WEBPLAYER_CMD, data)

    async def cmd_power_off(self):
        ''' send power OFF command to player '''
        self.powered = False

    async def cmd_volume_set(self, volume_level):
        ''' send new volume level command to player '''
        data = { 'player_id': self.player_id, 'cmd': 'volume_set', 'volume_level': volume_level}
        await self.mass.signal_event(EVENT_WEBPLAYER_CMD, data)

    async def cmd_volume_mute(self, is_muted=False):
        ''' send mute command to player '''
        data = { 'player_id': self.player_id, 'cmd': 'volume_mute', 'is_muted': is_muted}
        await self.mass.signal_event(EVENT_WEBPLAYER_CMD, data)

    async def cmd_play_uri(self, uri:str):
        ''' play single uri on player '''
        data = { 'player_id': self.player_id, 'cmd': 'play_uri', 'uri': uri}
        await self.mass.signal_event(EVENT_WEBPLAYER_CMD, data)

    async def handle_state(self, data):
        ''' handle state event from player '''
        if 'volume_level' in data:
            self.volume_level = data['volume_level']
        if 'muted' in data:
            self.muted = data['muted']
        if 'state' in data:
            self.state = PlayerState(data['state'])
        if 'cur_time' in data:
            self.cur_time = data['cur_time']
        if 'cur_uri' in data:
            self.cur_uri = data['cur_uri']
        if 'powered' in data:
            self.powered = data['powered']
        if 'name' in data:
            self.name = data['name']
        self._last_message = time.time()


