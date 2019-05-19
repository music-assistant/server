#!/usr/bin/env python3
# -*- coding:utf-8 -*-

import asyncio
import os
from typing import List
import random
import sys
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
from modules.cache import use_cache
import copy
import pychromecast
from pychromecast.controllers.multizone import MultizoneController
from pychromecast.controllers import BaseController
from pychromecast.controllers.spotify import SpotifyController
from pychromecast.controllers.media import MediaController
import types

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
        self._chromecasts = {}
        self.supported_musicproviders = ['http']
        self.http_session = aiohttp.ClientSession(loop=mass.event_loop)
        asyncio.ensure_future(self.__discover_chromecasts())
        

    ### Provider specific implementation #####

    async def player_command(self, player_id, cmd:str, cmd_args=None):
        ''' issue command on player (play, pause, next, previous, stop, power, volume, mute) '''
        if cmd == 'play':
            if self._chromecasts[player_id].media_controller.status.media_session_id:
                self._chromecasts[player_id].media_controller.play()
            else:
                await self.__resume_queue(player_id)
        elif cmd == 'pause':
            self._chromecasts[player_id].media_controller.pause()
        elif cmd == 'stop':
            self._chromecasts[player_id].media_controller.stop()
        elif cmd == 'next':
            self._chromecasts[player_id].media_controller.queue_next()
        elif cmd == 'previous':
            self._chromecasts[player_id].media_controller.queue_prev()
        elif cmd == 'power' and cmd_args == 'off':
            self._chromecasts[player_id].quit_app() # power is not supported so send quit app instead
        elif cmd == 'power':
            self._chromecasts[player_id].media_controller.launch()
        elif cmd == 'volume':
            self._chromecasts[player_id].set_volume(try_parse_int(cmd_args)/100)
        elif cmd == 'mute' and cmd_args == 'off':
            self._chromecasts[player_id].set_volume_muted(False)
        elif cmd == 'mute':
            self._chromecasts[player_id].set_volume_muted(True)

    async def player_queue(self, player_id, offset=0, limit=50):
        ''' return the items in the player's queue '''
        items = []
        for item in self._chromecasts[player_id].queue[offset:limit]:
            track = await self.__track_from_uri(item['media']['contentId'])
            if track:
                items.append(track)
        return items
    
    async def create_queue_item(self, track):
        '''create queue item from track info '''
        return {
            'autoplay' : True,
            'preloadTime' : 10,
            'playbackDuration': int(track.duration),
            'startTime' : 0,
            'activeTrackIds' : [],
            'media': {
                'contentId':  track.uri,
                'customData': {'provider': track.provider},
                'contentType': "audio/flac",
                'streamType': 'BUFFERED',
                'metadata': {
                    'title': track.name,
                    'artist': track.artists[0].name,
                },
                'duration': int(track.duration)
            }
        }
    
    async def play_media(self, player_id, media_items, queue_opt='play'):
        ''' 
            play media on a player
        '''
        castplayer = self._chromecasts[player_id]
        player = self._players[player_id]
        media_controller = castplayer.media_controller
        receiver_ctrl = media_controller._socket_client.receiver_controller
        cur_queue_index = 0
        if media_controller.queue_cur_id != None:
            for item in media_controller.queue_items:
                # status queue may contain at max 3 tracks (previous, current and next)
                if item['itemId'] == media_controller.queue_cur_id:
                    cur_queue_item = item
                    # find out the current index
                    for counter, value in enumerate(castplayer.queue):
                        if value['media']['contentId'] == cur_queue_item['media']['contentId']:
                            cur_queue_index = counter
                            break
                    break
        if (not media_controller.queue_cur_id or not media_controller.status.media_session_id or not castplayer.queue):
            queue_opt = 'replace'

        new_queue_items = []
        for track in media_items:
            queue_item = await self.create_queue_item(track)
            new_queue_items.append(queue_item)

        if (queue_opt in ['replace', 'play'] or not media_controller.queue_cur_id or 
                not media_controller.status.media_session_id or not castplayer.queue):
            # load new Chromecast queue with items
            if queue_opt == 'add':
                # append items to queue
                castplayer.queue = castplayer.queue + new_queue_items
                startindex = cur_queue_index
            elif queue_opt == 'play':
                # keep current queue but append new items at begin and start playing first item
                castplayer.queue = new_queue_items + castplayer.queue[cur_queue_index:] + castplayer.queue[:cur_queue_index]
                startindex = 0
            elif queue_opt == 'next':
                # play the new items after the current playing item (insert before current next item)
                castplayer.queue = new_queue_items + castplayer.queue[cur_queue_index:] + castplayer.queue[:cur_queue_index]
                startindex = cur_queue_index
            else:
                # overwrite the whole queue with new item(s)
                castplayer.queue = new_queue_items
                startindex = 0
            # load first 10 items as soon as possible
            queuedata = { 
                    "type": 'QUEUE_LOAD',
                    "repeatMode":  "REPEAT_ALL" if player.repeat_enabled else "REPEAT_OFF",
                    "shuffle": player.shuffle_enabled,
                    "queueType": "PLAYLIST",
                    "startIndex":    startindex,    # Item index to play after this request or keep same item if undefined
                    "items": castplayer.queue[:10]
            }
            await self.__send_player_queue(receiver_ctrl, media_controller, queuedata)
            await asyncio.sleep(1)
            # append the rest of the items in the queue in chunks
            for chunk in chunks(castplayer.queue[10:], 100):
                queuedata = { "type": 'QUEUE_INSERT', "items": chunk }
                await self.__send_player_queue(receiver_ctrl, media_controller, queuedata)
                await asyncio.sleep(0.1)
        elif queue_opt == 'add':
            # existing queue is playing: simply append items to the end of the queue (in small chunks)
            castplayer.queue = castplayer.queue + new_queue_items
            insertbefore = None
            for chunk in chunks(new_queue_items, 100):
                queuedata = { "type": 'QUEUE_INSERT', "items": chunk }
                await self.__send_player_queue(receiver_ctrl, media_controller, queuedata)
                await asyncio.sleep(0.1)
        elif queue_opt == 'next':
            # play the new items after the current playing item (insert before current next item)
            player.queue = castplayer.queue[:cur_queue_index] + new_queue_items + castplayer.queue[cur_queue_index:]
            queuedata = { 
                        "type": 'QUEUE_INSERT',
                        "insertBefore":     media_controller.queue_cur_id+1,
                        "items":            new_queue_items[:200] # limit of the queue message
                }
            await self.__send_player_queue(receiver_ctrl, media_controller, queuedata)
            
    ### Provider specific (helper) methods #####

    async def __resume_queue(self, player_id):
        ''' resume queue play after power off '''
        player = self._players[player_id]
        castplayer = self._chromecasts[player_id]
        media_controller = castplayer.media_controller
        receiver_ctrl = media_controller._socket_client.receiver_controller
        startindex = 0
        if player.cur_item and player.cur_item.name:
            for index, item in enumerate(castplayer.queue):
                if item['media']['metadata']['title'] == player.cur_item.name:
                    startindex = index
                    break
        queuedata = { 
                "type": 'QUEUE_LOAD',
                "repeatMode":  "REPEAT_ALL" if player.repeat_enabled else "REPEAT_OFF",
                "shuffle": player.shuffle_enabled,
                "queueType": "PLAYLIST",
                "startIndex":    startindex,    # Item index to play after this request or keep same item if undefined
                "items": castplayer.queue[:10]
        }
        await self.__send_player_queue(receiver_ctrl, media_controller, queuedata)
        await asyncio.sleep(1)
        # append the rest of the items in the queue in chunks
        for chunk in chunks(castplayer.queue[10:], 100):
            await asyncio.sleep(0.1)
            queuedata = { "type": 'QUEUE_INSERT', "items": chunk }
            await self.__send_player_queue(receiver_ctrl, media_controller, queuedata)
        
    async def __send_player_queue(self, receiver_ctrl, media_controller, queuedata):
        '''send new data to the CC queue'''
        def app_launched_callback():
                LOGGER.info("app_launched_callback")
                """Plays media after chromecast has switched to requested app."""
                queuedata['mediaSessionId'] = media_controller.status.media_session_id
                LOGGER.info('')
                LOGGER.info('')
                media_controller.send_message(queuedata, inc_session_id=False)
        receiver_ctrl.launch_app(media_controller.app_id,
                                callback_function=app_launched_callback)

    async def __handle_player_state(self, chromecast, caststatus=None, mediastatus=None):
        ''' handle a player state message from the socket '''
        player_id = str(chromecast.uuid)
        player = self._players[player_id]
        # always update player details that may change
        player.name = chromecast.name
        if caststatus:
            player.muted = caststatus.volume_muted
            player.volume_level = caststatus.volume_level * 100
            player.powered = chromecast.media_controller.status.media_session_id != None
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
        track = await self.__track_from_uri(mediastatus.content_id)
        if not track:
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

    async def __track_from_uri(self, uri):
        ''' try to parse uri loaded in CC to a track we understand '''
        track = None
        if uri.startswith('spotify://track:') and 'spotify' in self.mass.music.providers:
            track_id = uri.replace('spotify:track:','')
            track = await self.mass.music.providers['spotify'].track(track_id)
        elif uri.startswith('qobuz://') and 'qobuz' in self.mass.music.providers:
            track_id = uri.replace('qobuz://','').replace('.flac','')
            track = await self.mass.music.providers['qobuz'].track(track_id)
        elif uri.startswith('http') and '/stream' in uri:
            item_id = uri.split('/')[-1]
            provider = uri.split('/')[-2]
            track = await self.mass.music.providers[provider].track(item_id)
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
                player.player_provider = self.prov_id
                chromecast.start()
                # patch the receive message method for handling queue status updates
                chromecast.queue = []
                chromecast.media_controller.queue_items = []
                chromecast.media_controller.queue_cur_id = None
                chromecast.media_controller.receive_message = types.MethodType(receive_message, chromecast.media_controller)
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
                self._chromecasts[player_id] = chromecast
                self._players[player_id] = player
        LOGGER.info('Chromecast discovery done...')

def chunks(l, n):
    """Yield successive n-sized chunks from l."""
    for i in range(0, len(l), n):
        yield l[i:i + n]

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

def receive_message(self, message, data):
    """ Called when a media message is received. """
    #LOGGER.info('message: %s - data: %s'%(message, data))
    if data['type'] == 'MEDIA_STATUS':
        try:
            self.queue_items = data['status'][0]['items']
        except:
            pass
        try:
            self.queue_cur_id = data['status'][0]['currentItemId']
        except:
            pass
        self._process_media_status(data)
        return True
    return False