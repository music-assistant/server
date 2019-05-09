#!/usr/bin/env python3
# -*- coding:utf-8 -*-

import asyncio
import os
from typing import List
import random
import sys
sys.path.append("..")
from utils import run_periodic, LOGGER, parse_track_title
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
    enabled = mass.config["playerproviders"]['lms'].get(CONF_ENABLED)
    hostname = mass.config["playerproviders"]['lms'].get(CONF_HOSTNAME)
    port = mass.config["playerproviders"]['lms'].get(CONF_PORT)
    if enabled and hostname and port:
        provider = LMSProvider(mass, hostname, port)
        return provider
    return False

def config_entries():
    ''' get the config entries for this provider (list with key/value pairs)'''
    return [
        (CONF_ENABLED, False, CONF_ENABLED),
        (CONF_HOSTNAME, 'localhost', CONF_HOSTNAME), 
        (CONF_PORT, 9000, CONF_PORT)
        ]

class LMSProvider(PlayerProvider):
    ''' support for Logitech Media Server '''

    def __init__(self, mass, hostname, port):
        self.prov_id = 'lms'
        self.name = 'Logitech Media Server'
        self.icon = ''
        self.mass = mass
        self._players = {}
        self._host = hostname
        self._port = port
        self._players = {}
        self.last_msg_received = 0
        self.supports_queue = True # whether this provider has native support for a queue
        self.supports_http_stream = True # whether we can fallback to http streaming
        self.supported_musicproviders = [
            ('qobuz', [MediaType.Track]), 
            ('file', [MediaType.Track, MediaType.Artist, MediaType.Album, MediaType.Playlist]),
            ('spotify', [MediaType.Track, MediaType.Artist, MediaType.Album, MediaType.Playlist]), 
            ('http', [MediaType.Track])
        ]
        self.http_session = aiohttp.ClientSession(loop=mass.event_loop)
        # we use a combi of active polling and subscriptions because the cometd implementation of LMS is somewhat unreliable
        asyncio.ensure_future(self.__lms_events())
        asyncio.ensure_future(self.__get_players())            

    ### Provider specific implementation #####

    async def player_command(self, player_id, cmd:str, cmd_args=None):
        ''' issue command on player (play, pause, next, previous, stop, power, volume, mute) '''
        lms_commands = []
        if cmd == 'play':
            lms_commands = ['play']
        elif cmd == 'pause':
            lms_commands = ['pause', '1']
        elif cmd == 'stop':
            lms_commands = ['stop']
        elif cmd == 'next':
            lms_commands = ['playlist', 'index', '+1']
        elif cmd == 'previous':
            lms_commands = ['playlist', 'index', '-1']
        elif cmd == 'stop':
            lms_commands = ['playlist', 'stop']
        elif cmd == 'power' and cmd_args in ['on', '1', 1]:
            lms_commands = ['power', '1']
        elif cmd == 'power' and cmd_args in ['off', '0', 0]:
            lms_commands = ['power', '0']
        elif cmd == 'volume' and cmd_args == 'up':
            lms_commands = ['mixer', 'volume', '+2']
        elif cmd == 'volume' and cmd_args == 'down':
            lms_commands = ['mixer', 'volume', '-2']
        elif cmd == 'volume':
            lms_commands = ['mixer', 'volume', cmd_args]
        elif cmd == 'mute' and cmd_args in ['on', '1', 1]:
            lms_commands = ['mixer', 'muting', '1']
        elif cmd == 'mute' and cmd_args in ['off', '0', 0]:
            lms_commands = ['mixer', 'muting', '0']
        return await self.__get_data(lms_commands, player_id=player_id)

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
        if queue_opt == 'play':
            cmd = ['playlist', 'insert', uri]
            await self.__get_data(cmd, player_id=player_id)
            cmd2 = ['playlist', 'index', '+1']
            return await self.__get_data(cmd2, player_id=player_id)
        elif queue_opt == 'replace':
            cmd = ['playlist', 'play', uri]
            return await self.__get_data(cmd, player_id=player_id)
        elif queue_opt == 'next':
            cmd = ['playlist', 'insert', uri]
            return await self.__get_data(cmd, player_id=player_id)
        else:
            cmd = ['playlist', 'add', uri]
            return await self.__get_data(cmd, player_id=player_id)
        
    async def player_queue(self, player_id, offset=0, limit=50):
        ''' return the items in the player's queue '''
        items = []
        cur_index = await self.__get_data(["playlist", "index", "?"], player_id=player_id)
        cur_index = int(cur_index['_index'])
        offset += cur_index # we do not care about already played tracks
        player_details = await self.__get_data(["status", offset, limit, "tags:aAcCdegGijJKlostuxyRwk"], player_id=player_id)
        for item in player_details['playlist_loop']:
            track = await self.__parse_track(item)
            items.append(track)
        return items

    ### Provider specific (helper) methods #####
    
    async def __get_players(self):
        ''' update all players, used as fallback if cometd is failing and to detect removed players'''
        server_info = await self.__get_data(['players', 0, 1000])
        player_ids = await self.__process_serverstatus(server_info)
        for player_id in player_ids:
            player_details = await self.__get_data(["status", "-","1", "tags:aAcCdegGijJKlostuxyRwk"], player_id=player_id)
            await self.__process_player_details(player_id, player_details)

    async def __process_player_details(self, player_id, player_details):
        ''' get state of a given player '''
        if player_id not in self._players:
            return
        player = self._players[player_id]
        volume = player_details.get('mixer volume',0)
        player.muted = volume < 0
        if volume >= 0:
            player.volume_level = player_details.get('mixer volume',0)
        player.shuffle_enabled = player_details.get('playlist shuffle',0) != 0
        player.repeat_enabled = player_details.get('playlist repeat',0) != 0
        # player state
        player.powered = player_details['power'] == 1
        if player_details['mode'] == 'play':
            player.state = PlayerState.Playing
        elif player_details['mode'] == 'pause':
            player.state = PlayerState.Paused
        else:
            player.state = PlayerState.Stopped
        # current track
        if player_details.get('playlist_loop'):
            player.cur_item = await self.__parse_track(player_details['playlist_loop'][0])
            player.cur_item_time = player_details.get('time',0)
        else:
            player.cur_item = None
            player.cur_item_time = 0
        await self.mass.player.update_player(player)

    async def __process_serverstatus(self, server_status):
        ''' process players from server state msg (players_loop) '''
        cur_player_ids = []
        for lms_player in server_status['players_loop']:
            if lms_player['isplayer'] != 1:
                continue
            player_id = lms_player['playerid']
            cur_player_ids.append(player_id)
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
            player.name = lms_player['name']
            if lms_player['model'] == "group":
                player.is_group = True
                # player is a groupplayer, retrieve childs
                group_player_child_ids = await self.__get_group_childs(player_id)
                for child_player_id in group_player_child_ids:
                    self._players[child_player_id].group_parent = player_id
            elif player.group_parent:
                # check if player parent is still correct
                group_player_child_ids = await self.__get_group_childs(player.group_parent)
                if not player_id in group_player_child_ids:
                    player.group_parent = None
            # process update
            await self.mass.player.update_player(player)
        # process removed players...
        for player_id, player in self._players.items():
            if player_id not in cur_player_ids:
                await self.mass.player.remove_player(player_id)
        return cur_player_ids

    async def __parse_track(self, track_details):
        ''' parse track in LMS to our internal format '''
        track_url = track_details.get('url','')
        if track_url.startswith('qobuz://') and 'qobuz' in self.mass.music.providers:
            # qobuz track!
            try:
                track_id = track_url.replace('qobuz://','').replace('.flac','')
                return await self.mass.music.providers['qobuz'].track(track_id)
            except Exception as exc:
                LOGGER.error(exc)
        elif track_url.startswith('spotify://track:') and 'spotify' in self.mass.music.providers:
            # spotify track!
            try:
                track_id = track_url.replace('spotify://track:','')
                return await self.mass.music.providers['spotify'].track(track_id)
            except Exception as exc:
                LOGGER.error(exc)
        # fallback to a generic track
        track = Track()
        track.name = track_details['title']
        track.duration = int(track_details['duration'])
        image = "http://%s:%s%s" % (self._host, self._port, track_details['artwork_url'])
        track.metadata['image'] = image
        return track

    async def __get_group_childs(self, group_player_id):
        ''' get child players for groupplayer '''
        group_childs = []
        result = await self.__get_data('playergroup', player_id=group_player_id)
        if result and 'players_loop' in result:
            group_childs = [item['id'] for item in result['players_loop']]
        return group_childs
    
    async def __lms_events(self):
        # Receive events from LMS through CometD socket
        while True:
            try:
                last_msg_received = 0
                async with Client("http://%s:%s/cometd" % (self._host, self._port), 
                            connection_types=ConnectionType.LONG_POLLING, 
                            extensions=[LMSExtension()]) as client:
                    # subscribe
                    watched_players = []
                    await client.subscribe("/slim/subscribe/serverstatus")
                    
                    # listen for incoming messages
                    async for message in client:
                        last_msg_received = int(time.time())
                        if 'playerstatus' in message['channel']:
                            # player state
                            player_id = message['channel'].split('playerstatus/')[1]
                            asyncio.ensure_future(self.__process_player_details(player_id, message['data']))           
                        elif '/slim/serverstatus' in message['channel']:
                            # server state with all players
                            player_ids = await self.__process_serverstatus(message['data'])
                            for player_id in player_ids:
                                if player_id not in watched_players:
                                    # subscribe to player change events
                                    watched_players.append(player_id)
                                    await client.subscribe("/slim/subscribe/playerstatus/%s" % player_id)
            except Exception as exc:
                LOGGER.exception(exc)
      
    async def __get_data(self, cmds:List, player_id=''):
        ''' get data from api'''
        if not isinstance(cmds, list):
            cmds = [cmds]
        cmd = [player_id, cmds]
        url = "http://%s:%s/jsonrpc.js" % (self._host, self._port)
        params = {"id": 1, "method": "slim.request", "params": cmd}
        try:
            async with self.http_session.post(url, json=params) as response:
                result = await response.json()
                return result['result']
        except Exception as exc:
            LOGGER.exception('Error executing LMS command %s' % params)
            return None


class LMSExtension(Extension):
    ''' Extension for the custom cometd implementation of LMS'''

    async def incoming(self, payload, headers=None):
        pass

    async def outgoing(self, payload, headers):
        ''' override outgoing messages to fit LMS custom implementation'''

        # LMS does not need/want id for the connect and handshake message    
        if payload[0]['channel'] == '/meta/handshake' or payload[0]['channel'] == '/meta/connect':
            del payload[0]['id']
        
        # handle subscriptions
        if 'subscribe' in payload[0]['channel']:
            client_id = payload[0]['clientId']
            if payload[0]['subscription'] == '/slim/subscribe/serverstatus':
                # append additional request data to the request
                payload[0]['data'] = {'response':'/%s/slim/serverstatus' % client_id, 
                            'request':['', ['serverstatus', 0, 100, 'subscribe:60']]}
                payload[0]['channel'] = '/slim/subscribe'
            if payload[0]['subscription'].startswith('/slim/subscribe/playerstatus'):
                # append additional request data to the request
                player_id = payload[0]['subscription'].split('/')[-1]
                payload[0]['data'] = {'response':'/%s/slim/playerstatus/%s' % (client_id, player_id), 
                            'request':[player_id, ["status", "-", 1, "tags:aAcCdegGijJKlostuxyRwk", "subscribe:60"]]}
                payload[0]['channel'] = '/slim/subscribe'