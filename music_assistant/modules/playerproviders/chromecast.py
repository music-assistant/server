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
import urllib
import select

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
        self._player_queue = {}
        self.supported_musicproviders = ['http']
        asyncio.ensure_future(self.__discover_chromecasts())
        

    ### Provider specific implementation #####

    async def player_command(self, player_id, cmd:str, cmd_args=None):
        ''' issue command on player (play, pause, next, previous, stop, power, volume, mute) '''
        if cmd == 'play':
            self._players[player_id].powered = True
            if self._chromecasts[player_id].media_controller.status.player_is_playing:
                pass
            elif self._chromecasts[player_id].media_controller.status.player_is_paused:
                self._chromecasts[player_id].media_controller.play()
            else:
                await self.__resume_queue(player_id)
            await self.mass.player.update_player(self._players[player_id])
        elif cmd == 'pause':
            self._chromecasts[player_id].media_controller.pause()
        elif cmd == 'stop':
            self._chromecasts[player_id].media_controller.stop()
        elif cmd == 'next':
            self._chromecasts[player_id].media_controller.queue_next()
        elif cmd == 'previous':
            self._chromecasts[player_id].media_controller.queue_prev()
        elif cmd == 'power' and cmd_args == 'off':
            self._players[player_id].powered = False
            self._chromecasts[player_id].media_controller.stop() # power is not supported so send stop instead
            await self.mass.player.update_player(self._players[player_id])
        elif cmd == 'power':
            self._players[player_id].powered = True
            await self.mass.player.update_player(self._players[player_id])
        elif cmd == 'volume':
            self._chromecasts[player_id].set_volume(try_parse_int(cmd_args)/100)
        elif cmd == 'mute' and cmd_args == 'off':
            self._chromecasts[player_id].set_volume_muted(False)
        elif cmd == 'mute':
            self._chromecasts[player_id].set_volume_muted(True)
        self._chromecasts[player_id].wait()

    async def player_queue(self, player_id, offset=0, limit=50):
        ''' return the current items in the player's queue '''
        return self._player_queue[player_id][offset:limit]
    
    async def play_media(self, player_id, media_items, queue_opt='play'):
        ''' 
            play media on a player
        '''
        castplayer = self._chromecasts[player_id]
        cur_queue_index = await self.__get_cur_queue_index(player_id)

        if queue_opt == 'replace' or not self._player_queue[player_id]:
            # overwrite queue with new items
            self._player_queue[player_id] = media_items
            await self.__queue_load(player_id, self._player_queue[player_id], 0)
        elif queue_opt == 'play':
            # replace current item with new item(s)
            self._player_queue[player_id] = self._player_queue[player_id][:cur_queue_index] + media_items + self._player_queue[player_id][cur_queue_index+1:]
            await self.__queue_load(player_id, self._player_queue[player_id], cur_queue_index)
        elif queue_opt == 'next':
            # insert new items at current index +1
            if len(self._player_queue[player_id]) > cur_queue_index:
                old_next_uri = self._player_queue[player_id][cur_queue_index+1].uri
            else:
                old_next_uri = None
            self._player_queue[player_id] = self._player_queue[player_id][:cur_queue_index+1] + media_items + self._player_queue[player_id][cur_queue_index+1:]
            # find out the itemID of the next item in CC queue
            insert_at_item_id = None
            if old_next_uri:
                for item in castplayer.media_controller.queue_items:
                    if item['media']['contentId'] == old_next_uri:
                        insert_at_item_id = item['itemId']
            await self.__queue_insert(player_id, media_items, insert_at_item_id)
        elif queue_opt == 'add':
            # add new items at end of queue
            self._player_queue[player_id] = self._player_queue[player_id] + media_items
            await self.__queue_insert(player_id, media_items)

    ### Provider specific (helper) methods #####

    async def __get_cur_queue_index(self, player_id):
        ''' retrieve index of current item in the player queue '''
        cur_index = 0
        for index, track in enumerate(self._player_queue[player_id]):
            if track.uri == self._chromecasts[player_id].media_controller.status.content_id:
                cur_index = index
                break
        return cur_index

    async def __queue_load(self, player_id, new_tracks, startindex=None):
        ''' load queue on player with given queue items '''
        castplayer = self._chromecasts[player_id]
        player = self._players[player_id]
        queue_items = await self.__create_queue_items(new_tracks[:50])
        queuedata = { 
                "type": 'QUEUE_LOAD',
                "repeatMode":  "REPEAT_ALL" if player.repeat_enabled else "REPEAT_OFF",
                "shuffle": player.shuffle_enabled,
                "queueType": "PLAYLIST",
                "startIndex":    startindex,    # Item index to play after this request or keep same item if undefined
                "items": queue_items # only load 50 tracks at once or the socket will crash
        }
        await self.__send_player_queue(castplayer, queuedata)
        if len(new_tracks) > 50:
            await self.__queue_insert(player_id, new_tracks[51:])

    async def __queue_insert(self, player_id, new_tracks, insert_before=None):
        ''' insert item into the player queue '''
        castplayer = self._chromecasts[player_id]
        queue_items = await self.__create_queue_items(new_tracks)
        for chunk in chunks(queue_items, 50):
            queuedata = { 
                        "type": 'QUEUE_INSERT',
                        "insertBefore":     insert_before,
                        "items":            chunk
                }
            await self.__send_player_queue(castplayer, queuedata)

    async def __queue_update(self, player_id, queue_items_to_update):
        ''' update the cast player queue '''
        castplayer = self._chromecasts[player_id]
        queuedata = { 
                    "type": 'QUEUE_UPDATE',
                    "items": queue_items_to_update
            }
        await self.__send_player_queue(castplayer, queuedata)

    async def __queue_remove(self, player_id, queue_item_ids):
        ''' remove items from the cast player queue '''
        castplayer = self._chromecasts[player_id]
        queuedata = { 
                    "type": 'QUEUE_REMOVE',
                    "items": queue_item_ids
            }
        await self.__send_player_queue(castplayer, queuedata)

    async def __resume_queue(self, player_id):
        ''' resume queue play after power off '''
        
        player = self._players[player_id]
        queue_index = await self.__get_cur_queue_index(player_id)
        print('resume queue at index %s' % queue_index)
        tracks = self._player_queue[player_id]
        await self.__queue_load(player_id, tracks, queue_index)

    async def __create_queue_items(self, tracks):
        ''' create list of CC queue items from tracks '''
        queue_items = []
        for track in tracks:
            queue_item = await self.__create_queue_item(track)
            queue_items.append(queue_item)
        return queue_items

    async def __create_queue_item(self, track):
        '''create queue item from track info '''
        return {
            'autoplay' : True,
            'preloadTime' : 10,
            'playbackDuration': int(track.duration),
            'startTime' : 0,
            'activeTrackIds' : [],
            'media': {
                'contentId':  track.uri,
                'customData': {
                    'provider': track.provider, 
                    'uri': track.uri, 
                    'item_id': track.item_id
                },
                'contentType': "audio/flac",
                'streamType': 'BUFFERED',
                'metadata': {
                    'title': track.name,
                    'artist': track.artists[0].name,
                },
                'duration': int(track.duration)
            }
        }
        
    async def __send_player_queue(self, castplayer, queuedata):
        '''send new data to the CC queue'''
        media_controller = castplayer.media_controller
        receiver_ctrl = media_controller._socket_client.receiver_controller
        def app_launched_callback():
                """Plays media after chromecast has switched to requested app."""
                queuedata['mediaSessionId'] = media_controller.status.media_session_id
                media_controller.send_message(queuedata, inc_session_id=False)
                castplayer.wait()
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
        if mediastatus:
            # chromecast does not support power on/of so we only set state
            if mediastatus.player_state in ['PLAYING', 'BUFFERING']:
                player.state = PlayerState.Playing
            elif mediastatus.player_state == 'PAUSED':
                player.state = PlayerState.Paused
            else:
                player.state = PlayerState.Stopped
            player.cur_item = await self.__parse_track(mediastatus)
            player.cur_item_time =  chromecast.media_controller.status.adjusted_current_time
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
            params = urllib.parse.parse_qs(uri.split('?')[1])
            track_id = params['track_id'][0]
            provider = params['provider'][0]
            track = await self.mass.music.providers[provider].track(track_id)
        return track

    async def __handle_group_members_update(self, mz, added_player=None, removed_player=None):
        ''' callback when cast group members update '''
        if added_player:
            if added_player in self._players:
                self._players[added_player].group_parent = str(mz._uuid)
                self.mass.event_loop.create_task(self.mass.player.update_player(self._players[added_player]))
        elif removed_player:
            if removed_player in self._players:
                self._players[removed_player].group_parent = None
                self.mass.event_loop.create_task(self.mass.player.update_player(self._players[removed_player]))
        else:
            for member in mz.members:
                if member in self._players:
                    self._players[member].group_parent = str(mz._uuid)
                    self.mass.event_loop.create_task(self.mass.player.update_player(self._players[member]))

    async def __chromecast_discovered(self, chromecast):
        LOGGER.debug("discovered chromecast: %s" % chromecast)
        player_id = str(chromecast.uuid)
        ip_change = False
        if player_id in self._chromecasts and chromecast.uri != self._chromecasts[player_id].uri:
            LOGGER.warning('Chromecast uri changed ?! - old: %s - new: %s' %(self._chromecasts[player_id].uri, chromecast.uri))
            ip_change = True
        if not player_id in self._players or ip_change:
            player = MusicPlayer()
            player.player_id = player_id
            player.name = chromecast.name
            player.player_provider = self.prov_id
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
            self._chromecasts[player_id] = chromecast
            self._players[player_id] = player
            if not player_id in self._player_queue:
                # TODO: persistant storage of player queue ?
                self._player_queue[player_id] = []
            chromecast.wait()

    @run_periodic(3600)
    async def __discover_chromecasts(self):
        ''' discover chromecasts on the network '''
        LOGGER.info('Running Chromecast discovery...')
        def callback(chromecast):
            self.mass.event_loop.create_task(self.__chromecast_discovered(chromecast))
        
        stop_discovery = pychromecast.get_chromecasts(blocking=False, callback=callback)
        await asyncio.sleep(10)
        stop_discovery()
        LOGGER.info('Finished Chromecast discovery...')

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