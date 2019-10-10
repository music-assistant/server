#!/usr/bin/env python3
# -*- coding:utf-8 -*-

import asyncio
# import os
# from typing import List
# import random
# import sys
# import json
import aiohttp
# import time
# import datetime
# import hashlib
import pychromecast
from pychromecast.controllers.multizone import MultizoneController
from pychromecast.controllers import BaseController
from pychromecast.controllers.media import MediaController
import types
# import urllib
# import select
from ...utils import run_periodic, LOGGER, try_parse_int
from ...models.playerprovider import PlayerProvider
from ...models.player import Player, PlayerState
from ...constants import CONF_ENABLED, CONF_HOSTNAME, CONF_PORT

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

class ChromecastPlayer(Player):
    ''' Chromecast player object '''
    cc = None

    async def __stop(self):
        ''' send stop command to player '''
        self.cc.media_controller.stop()

    async def __play(self):
        ''' send play command to player '''
        self.cc.media_controller.play()

    async def __pause(self):
        ''' send pause command to player '''
        self.cc.media_controller.pause()

    async def __power_on(self):
        ''' send power ON command to player '''
        self.powered = True

    async def __power_off(self):
        ''' send power OFF command to player '''
        self.powered = False
        # power is not supported so send quit_app instead
        if not self.group_parent:
            self.cc.quit_app()

    async def __volume_set(self, volume_level):
        ''' send new volume level command to player '''
        self.cc.set_volume(volume_level/100)
        self.volume_level = volume_level

    async def __volume_mute(self, is_muted=False):
        ''' send mute command to player '''
        self.cc.set_volume_muted(is_muted)


class ChromecastProvider(PlayerProvider):
    ''' support for ChromeCast Audio '''
    
    def __init__(self, mass):
        self.prov_id = 'chromecast'
        self.name = 'Chromecast'
        self.mass = mass
        self._discovery_running = False
        self.mass.event_loop.create_task(self.__periodic_chromecast_discovery())

    async def __queue_load(self, player_id, new_tracks, startindex=None):
        ''' load queue on player with given queue items '''
        castplayer = self._chromecasts[player_id]
        player = self._players[player_id]
        queue_items = await self.__create_queue_items(new_tracks[:50])
        self._player_queue_index[player_id] = 0
        queuedata = { 
                "type": 'QUEUE_LOAD',
                "repeatMode":  "REPEAT_ALL" if player.repeat_enabled else "REPEAT_OFF",
                "shuffle": player.shuffle_enabled,
                "queueType": "PLAYLIST",
                "startIndex":    startindex,    # Item index to play after this request or keep same item if undefined
                "items": queue_items # only load 50 tracks at once or the socket will crash
        }
        await self.__send_player_queue(castplayer, queuedata)
        await asyncio.sleep(0.2)
        if len(new_tracks) > 50:
            await self.__queue_insert(player_id, new_tracks[51:])
            await asyncio.sleep(0.2)

    async def __play_stream_queue(self, player_id, startindex=0):
        ''' tell the cast player to stream our special queue (crossfaded) stream '''
        castplayer = self._chromecasts[player_id]
        uri = 'http://%s:%s/stream_queue?player_id=%s&startindex=%s'% (
            self.mass.player.local_ip, self.mass.config['base']['web']['http_port'], player_id, startindex)
        castplayer.play_media(uri, 'audio/flac')

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
        LOGGER.info('resuming queue....')
        tracks = self._player_queue[player_id]
        await self.play_media(player_id, tracks)

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
                    'artist': track.artists[0].name if track.artists else "",
                },
                'duration': int(track.duration)
            }
        }
        
    async def __send_player_queue(self, castplayer, queuedata):
        '''send new data to the CC queue'''
        media_controller = castplayer.media_controller
        receiver_ctrl = media_controller._socket_client.receiver_controller
        def send_queue():
                """Plays media after chromecast has switched to requested app."""
                queuedata['mediaSessionId'] = media_controller.status.media_session_id
                media_controller.send_message(queuedata, inc_session_id=False)
        if not media_controller.status.media_session_id:
            receiver_ctrl.launch_app(media_controller.app_id, callback_function=send_queue)
        else:
            send_queue()
        await asyncio.sleep(0.2)

    async def __handle_player_state(self, chromecast, caststatus=None, mediastatus=None):
        ''' handle a player state message from the socket '''
        player_id = str(chromecast.uuid)
        player = self.get_player(player_id)
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
            if not mediastatus.content_id:
                player.cur_item = None
                player.cur_item_time = 0
            elif not 'stream_queue' in mediastatus.content_id:
                player.cur_item = await self.__parse_track(mediastatus)
                player.cur_item_time =  mediastatus.adjusted_current_time
                self._player_queue_index[player_id] = await self.__get_cur_queue_index(player_id, mediastatus.content_id)
            elif 'stream_queue' in mediastatus.content_id:
                # player is playing our special queue continuous stream
                # try to work out the current time
                # player is playing a constant stream of the queue so we need to do this the hard way
                cur_time_queue = mediastatus.adjusted_current_time
                total_time = 0
                track_time = 0
                queue_index = self._player_queue_stream_startindex[player_id]
                queue_track = None
                while True:
                    queue_track = self._player_queue[player_id][queue_index]
                    if cur_time_queue > (queue_track.duration + total_time):
                        total_time += queue_track.duration
                        queue_index += 1
                    else:
                        track_time = cur_time_queue - total_time
                        break
                player.cur_item = queue_track
                player.cur_item_time = track_time
                self._player_queue_index[player_id] = queue_index

    async def __handle_group_members_update(self, mz, added_player=None, removed_player=None):
        ''' callback when cast group members update '''
        if added_player:
            player = self.get_player(added_player)
            group_player = self.get_player(str(mz._uuid))
            if player and group_player:
                player.group_parent = str(mz._uuid)
                LOGGER.debug("player %s added to group %s" %(player.name, group_player.name))
        elif removed_player:
            player = self.get_player(added_player)
            group_player = self.get_player(str(mz._uuid))
            if player and group_player:
                player.group_parent = None
                LOGGER.debug("player %s removed from group %s" %(player.name, group_player.name))
        else:
            for member in mz.members:
                player = self.get_player(member)
                if player:
                    player.group_parent = str(mz._uuid)
    
    @run_periodic(1800)
    async def __periodic_chromecast_discovery(self):
        ''' run chromecast discovery on interval '''
        await self.__chromecast_discovery()

    async def __chromecast_discovery(self):
        ''' background non-blocking chromecast discovery and handler '''
        if self._discovery_running:
            return
        self._discovery_running = True
        LOGGER.info("Chromecast discovery started...")
        # remove any disconnected players...
        removed_players = []
        for player in self.players:
            if not player.cc.socket_client or not player.cc.socket_client.is_connected:
                LOGGER.info("%s is disconnected" % player.name)
                # cleanup cast object
                del player.cc
                removed_players.append(player.player_id)
        # signal removed players
        for player_id in removed_players:
            await self.remove_player(player_id)
        # search for available chromecasts
        from pychromecast.discovery import start_discovery, stop_discovery
        def discovered_callback(name):
            """Called when zeroconf has discovered a (new) chromecast."""
            discovery_info = listener.services[name]
            ip_address, port, uuid, model_name, friendly_name = discovery_info
            player_id = str(uuid)
            if not self.get_player(player_id):
                LOGGER.info("discovered chromecast: %s - %s:%s" % (friendly_name, ip_address, port))
                asyncio.run_coroutine_threadsafe(
                        self.__chromecast_discovered(player_id, discovery_info), self.mass.event_loop)
        listener, browser = start_discovery(discovered_callback)
        await asyncio.sleep(15) # run discovery for 15 seconds
        stop_discovery(browser)
        LOGGER.info("Chromecast discovery completed...")
        self._discovery_running = False
    
    async def __chromecast_discovered(self, player_id, discovery_info):
        ''' callback when a (new) chromecast device is discovered '''
        from pychromecast import _get_chromecast_from_host, ChromecastConnectionError
        try:
            chromecast = _get_chromecast_from_host(discovery_info, tries=2, retry_wait=5)
        except ChromecastConnectionError:
            LOGGER.warning("Could not connect to device %s" % player_id)
            return
        # patch the receive message method for handling queue status updates
        chromecast.media_controller.queue_items = []
        chromecast.media_controller.queue_cur_id = None
        chromecast.media_controller.receive_message = types.MethodType(receive_message, chromecast.media_controller)
        listenerCast = StatusListener(chromecast, self.__handle_player_state, self.mass.event_loop)
        chromecast.register_status_listener(listenerCast)
        listenerMedia = StatusMediaListener(chromecast, self.__handle_player_state, self.mass.event_loop)
        chromecast.media_controller.register_status_listener(listenerMedia)
        player = ChromecastPlayer(self.mass, player_id, self.prov_id)
        if chromecast.cast_type == 'group':
            player.is_group = True
            mz = MultizoneController(chromecast.uuid)
            mz.register_listener(MZListener(mz, self.__handle_group_members_update, self.mass.event_loop))
            chromecast.register_handler(mz)
            chromecast.register_connection_listener(MZConnListener(mz))
            chromecast.mz = mz
        player.cc = chromecast
        player.cc.wait()
        self.add_player(player)
        self.update_all_group_members()

    def update_all_group_members(self):
        ''' force member update of all cast groups '''
        for player in self.players:
            if player.cc.cast_type == 'group':
                player.cc.mz.update_members()

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
        asyncio.run_coroutine_threadsafe(
                self.__handle_player_state(self.chromecast, caststatus=status), self.loop)

class StatusMediaListener:
    def __init__(self, chromecast, callback, loop):
        self.chromecast= chromecast
        self.__handle_player_state = callback
        self.loop = loop
    def new_media_status(self, status):
        asyncio.run_coroutine_threadsafe(
                self.__handle_player_state(self.chromecast, mediastatus=status), self.loop)

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
                self.__handle_group_members_update(
                        self._mz, added_player=str(uuid)), self._loop)

    def multizone_member_removed(self, uuid):
        asyncio.run_coroutine_threadsafe(
                self.__handle_group_members_update(
                        self._mz, removed_player=str(uuid)), self._loop)

    def multizone_status_received(self):
        asyncio.run_coroutine_threadsafe(
                self.__handle_group_members_update(self._mz), self._loop)

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