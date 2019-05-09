#!/usr/bin/env python3
# -*- coding:utf-8 -*-

import asyncio
import os
from typing import List
import random
import sys
sys.path.append("..")
from utils import run_periodic, run_background_task, LOGGER, parse_track_title, try_parse_int
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
import pychromecast
from pychromecast.controllers.multizone import MultizoneController
from pychromecast.controllers import BaseController
from pychromecast.controllers.spotify import SpotifyController
import logging
logging.getLogger("pychromecast").setLevel(logging.WARNING)

def setup(mass):
    ''' setup the provider'''
    enabled = mass.config["playerproviders"]['chromecast'].get(CONF_ENABLED)
    if enabled:
        provider = ChromecastProvider(mass)
        return provider
    return False

def config_entries():
    ''' get the config entries for this provider (list with key/value pairs)'''
    return [
        (CONF_ENABLED, True, CONF_ENABLED),
        ]

class ChromecastProvider(PlayerProvider):
    ''' support for Home Assistant '''

    def __init__(self, mass):
        self.prov_id = 'chromecast'
        self.name = 'Chromecast'
        self.icon = ''
        self.mass = mass
        self._players = {}
        self.supports_queue = False
        self.supports_http_stream = True
        self.supported_musicproviders = [
            ('spotify', [MediaType.Track, MediaType.Artist, MediaType.Album, MediaType.Playlist]), 
            ('http', [MediaType.Track])
        ]
        self.http_session = aiohttp.ClientSession(loop=mass.event_loop)
        asyncio.ensure_future(self.__discover_chromecasts())
        

    ### Provider specific implementation #####

    async def player_command(self, player_id, cmd:str, cmd_args=None):
        ''' issue command on player (play, pause, next, previous, stop, power, volume, mute) '''
        if cmd == 'play':
            self._players[player_id].cast.media_controller.play()
        elif cmd == 'pause':
            self._players[player_id].cast.media_controller.pause()
        elif cmd == 'stop':
            self._players[player_id].cast.media_controller.stop()
        elif cmd == 'next':
            self._players[player_id].cast.media_controller.queue_next()
        elif cmd == 'previous':
            self._players[player_id].cast.media_controller.queue_previous()
        elif cmd == 'power' and cmd_args in ['on', '1', 1]:
            # power is not supported
            self._players[player_id].state = PlayerState.Stopped
            self._players[player_id].cast.media_controller.play()
        elif cmd == 'power' and cmd_args in ['off', '0', 0]:
            # power is not supported
            self._players[player_id].state = PlayerState.Off
            self._players[player_id].cast.media_controller.stop()
        elif cmd == 'volume':
            self._players[player_id].cast.set_volume(try_parse_int(cmd_args)/100)
        elif cmd == 'mute' and cmd_args in ['on', '1', 1]:
            self._players[player_id].cast.set_volume_muted(True)
        elif cmd == 'mute' and cmd_args in ['off', '0', 0]:
            self._players[player_id].cast.set_volume_muted(False)

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
        if uri.startswith('spotify:'):
            # native spotify playback
            uri = uri.replace('spotify://', '')
            from pychromecast.controllers.spotify import SpotifyController
            spotify =  self.mass.music.providers['spotify']
            token = await spotify.get_token()
            sp = SpController(token['accessToken'], token['expiresIn'])
            self._players[player_id].cast.register_handler(sp)
            sp.launch_app()
            spotify_player_id = sp.device
            if spotify_player_id:
                return await spotify.play_media(spotify_player_id, uri)
            else:
                LOGGER.error('player not found in spotify! %s' % player_id)
        elif uri.startswith('http'):
            self._players[player_id].cast.media_controller.play_media(uri, 'audio/flac')
        else:
            raise Exception("Not supported media_type or uri")

    ### Provider specific (helper) methods #####
    
    async def __handle_player_state(self, chromecast, caststatus=None, mediastatus=None):
        ''' handle a player state message from the socket '''
        player_id = str(chromecast.uuid)
        player = self._players[player_id]
        # always update player details that may change
        player.name = chromecast.name
        if caststatus:
            player.muted = caststatus.volume_muted
            player.volume_level = caststatus.volume_level * 100
            player.powered = not caststatus.is_stand_by
        if mediastatus:
            if mediastatus.player_state in ['PLAYING', 'BUFFERING']:
                player.state = PlayerState.Playing
            elif mediastatus.player_state == 'PAUSED':
                player.state = PlayerState.Paused
            else:
                player.state = PlayerState.Stopped
            player.cur_item = await self.__parse_track(mediastatus)
            player.cur_item_time = try_parse_int(mediastatus.current_time)
        await self.mass.player.update_player(player)

    async def __parse_track(self, mediastatus):
        ''' parse track in CC to our internal format '''
        if mediastatus.content_type == 'application/x-spotify.track':
            track_id = mediastatus.content_id.replace('spotify:track:','')
            track = await self.mass.music.providers['spotify'].track(track_id)
        else:
            # TODO: match this info manually in the DB!!
            track = Track()
            artist = mediastatus.artist
            album = mediastatus.album_name
            title = mediastatus.title
            track.name = "%s - %s" %(artist, title)
            track.duration = try_parse_int(mediastatus.duration)
            if mediastatus.media_metadata and mediastatus.media_metadata.get('images'):
                track.metadata.image = mediastatus.media_metadata['images'][-1]['url']
        return track

    async def __handle_group_members_update(self, mz, added_player=None, removed_player=None):
        ''' callback when cast group members update '''
        if added_player:
            if added_player in self._players:
                self._players[added_player].group_parent = str(mz._uuid)
        elif removed_player:
            if removed_player in self._players:
                self._players[removed_player].group_parent = None
        else:
            for member in mz.members:
                if member in self._players:
                    self._players[member].group_parent = str(mz._uuid)

    @run_periodic(600)
    async def __discover_chromecasts(self):
        ''' discover chromecasts on the network '''
        LOGGER.info('Starting Chromecast discovery...')
        bg_task = run_background_task(self.mass.bg_executor, pychromecast.get_chromecasts)
        chromecasts = await asyncio.gather(bg_task)
        for chromecast in chromecasts[0]:
            player_id = str(chromecast.uuid)
            if not player_id in self._players:
                player = MusicPlayer()
                player.player_id = player_id
                player.name = chromecast.name
                chromecast.start()
                listenerCast = StatusListener(chromecast, self.__handle_player_state, self.mass.event_loop)
                chromecast.register_status_listener(listenerCast)
                listenerMedia = StatusMediaListener(chromecast, self.__handle_player_state, self.mass.event_loop)
                chromecast.media_controller.register_status_listener(listenerMedia)
                if chromecast.cast_type == 'group':
                    player.is_group = True
                    mz = MultizoneController(chromecast.uuid)
                    mz.register_listener(MZListener(mz, self.__handle_group_members_update, self.mass.event_loop))
                    chromecast.register_handler(mz)
                    chromecast.register_connection_listener(MZConnListener(mz))
                    chromecast.wait()
                player.cast = chromecast
                player.player_provider = self.prov_id
                self._players[player_id] = player
        LOGGER.info('Chromecast discovery done...')


class StatusListener:
    def __init__(self, chromecast, callback, loop):
        self.chromecast = chromecast
        self.__handle_player_state = callback
        self.loop = loop

    def new_cast_status(self, status):
        asyncio.run_coroutine_threadsafe(self.__handle_player_state(self.chromecast, caststatus=status), self.loop)


class StatusMediaListener:
    def __init__(self, chromecast, callback, loop):
        self.chromecast= chromecast
        self.__handle_player_state = callback
        self.loop = loop

    def new_media_status(self, status):
        asyncio.run_coroutine_threadsafe(self.__handle_player_state(self.chromecast, mediastatus=status), self.loop)


class MZConnListener:
    def __init__(self, mz):
        self._mz=mz
    def new_connection_status(self, connection_status):
        """Handle reception of a new ConnectionStatus."""
        if connection_status.status == 'CONNECTED':
            self._mz.update_members()

class MZListener:
    def __init__(self, mz, callback, loop):
        self._mz = mz
        self._loop = loop
        self.__handle_group_members_update = callback

    def multizone_member_added(self, uuid):
        asyncio.run_coroutine_threadsafe(
                self.__handle_group_members_update(self._mz, added_player=str(uuid)), self._loop)

    def multizone_member_removed(self, uuid):
        asyncio.run_coroutine_threadsafe(
                self.__handle_group_members_update(self._mz, removed_player=str(uuid)), self._loop)

    def multizone_status_received(self):
        asyncio.run_coroutine_threadsafe(
                self.__handle_group_members_update(self._mz), self._loop)


class SpController(SpotifyController):
    """ Controller to interact with Spotify namespace. """
    def receive_message(self, message, data):
        """ handle the auth flow and active player selection """
        if data['type'] == 'setCredentialsResponse':
            self.send_message({'type': 'getInfo', 'payload': {}})
        if data['type'] == 'setCredentialsError':
            self.device = None
        if data['type'] == 'getInfoResponse':
            self.device = data['payload']['deviceID']
            self.is_launched = True
        return True
