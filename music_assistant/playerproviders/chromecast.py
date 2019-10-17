#!/usr/bin/env python3
# -*- coding:utf-8 -*-

import asyncio
import aiohttp
from typing import List
import logging
import pychromecast
from pychromecast.controllers.multizone import MultizoneController
from pychromecast.socket_client import CONNECTION_STATUS_CONNECTED, CONNECTION_STATUS_DISCONNECTED
import types

from ..utils import run_periodic, LOGGER, try_parse_int
from ..models.playerprovider import PlayerProvider
from ..models.player import Player, PlayerState
from ..models.playerstate import PlayerState
from ..models.player_queue import QueueItem, PlayerQueue
from ..constants import CONF_ENABLED, CONF_HOSTNAME, CONF_PORT

PROV_ID = 'chromecast'
PROV_NAME = 'Chromecast'
PROV_CLASS = 'ChromecastProvider'

CONFIG_ENTRIES = [
    (CONF_ENABLED, True, CONF_ENABLED),
    ]

PLAYER_CONFIG_ENTRIES = [
   ("gapless_enabled", False, "gapless_enabled"),
    ]

class ChromecastPlayer(Player):
    ''' Chromecast player object '''

    async def try_chromecast_command(self, cmd:types.MethodType, *args, **kwargs):
        ''' guard for disconnected socket client '''
        def _try_chromecast_command(_cmd:types.MethodType, *_args, **_kwargs):
            try:
                _cmd(*_args, **_kwargs)
            except (pychromecast.error.NotConnected, AttributeError):
                LOGGER.warning("Chromecast %s is not connected!" % self.name)
            except Exception as exc:
                LOGGER.warning(exc)
        return self.mass.event_loop.call_soon_threadsafe(
            _try_chromecast_command, cmd, *args, **kwargs)
    
    async def cmd_stop(self):
        ''' send stop command to player '''
        await self.try_chromecast_command(self.cc.media_controller.stop)

    async def cmd_play(self):
        ''' send play command to player '''
        await self.try_chromecast_command(self.cc.media_controller.play)

    async def cmd_pause(self):
        ''' send pause command to player '''
        await self.try_chromecast_command(self.cc.media_controller.pause)

    async def cmd_next(self):
        ''' send next track command to player '''
        await self.try_chromecast_command(self.cc.media_controller.queue_next)

    async def cmd_previous(self):
        ''' [CAN OVERRIDE] send previous track command to player '''
        await self.try_chromecast_command(self.cc.media_controller.queue_prev)
    
    async def cmd_power_on(self):
        ''' send power ON command to player '''
        self.powered = True

    async def cmd_power_off(self):
        ''' send power OFF command to player '''
        self.powered = False
        # power is not supported so send quit_app instead
        if not self.group_parent:
            await self.try_chromecast_command(self.cc.quit_app)

    async def cmd_volume_set(self, volume_level):
        ''' send new volume level command to player '''
        await self.try_chromecast_command(self.cc.set_volume, volume_level/100)
        self.volume_level = volume_level

    async def cmd_volume_mute(self, is_muted=False):
        ''' send mute command to player '''
        await self.try_chromecast_command(self.cc.set_volume_muted, is_muted)

    async def cmd_play_uri(self, uri:str):
        ''' play single uri on player '''
        if self.queue.use_queue_stream:
            # create CC queue so that skip and previous will work
            queue_item = QueueItem()
            queue_item.name = "Music Assistant"
            queue_item.uri = uri
            return await self.cmd_queue_load([queue_item, queue_item])
        else:
            await self.try_chromecast_command(self.cc.play_media, uri, 'audio/flac')

    async def cmd_queue_load(self, queue_items:List[QueueItem]):
        ''' load (overwrite) queue with new items '''
        cc_queue_items = await self.__create_queue_items(queue_items[:50])
        queuedata = { 
                "type": 'QUEUE_LOAD',
                "repeatMode":  "REPEAT_ALL" if self.queue.repeat_enabled else "REPEAT_OFF",
                "shuffle": False, # handled by our queue controller
                "queueType": "PLAYLIST",
                "startIndex":    0,    # Item index to play after this request or keep same item if undefined
                "items": cc_queue_items # only load 50 tracks at once or the socket will crash
        }
        await self.try_chromecast_command(self.__send_player_queue, queuedata)
        await asyncio.sleep(0.2)
        if len(queue_items) > 50:
            await self.cmd_queue_append(queue_items[51:])
            await asyncio.sleep(0.2)

    async def cmd_queue_insert(self, queue_items:List[QueueItem], insert_at_index):
        # for now we don't support this as google requires a special internal id
        # as item id to determine the insert position
        # https://developers.google.com/cast/docs/reference/caf_receiver/cast.framework.QueueManager#insertItems
        raise NotImplementedError

    async def cmd_queue_append(self, queue_items:List[QueueItem]):
        ''' 
            append new items at the end of the queue
        '''
        cc_queue_items = await self.__create_queue_items(queue_items)
        for chunk in chunks(cc_queue_items, 50):
            queuedata = { 
                        "type": 'QUEUE_INSERT',
                        "insertBefore":     None,
                        "items":            chunk
                }
            await self.try_chromecast_command(self.__send_player_queue, queuedata)

    async def __create_queue_items(self, tracks):
        ''' create list of CC queue items from tracks '''
        queue_items = []
        for track in tracks:
            queue_item = await self.__create_queue_item(track)
            queue_items.append(queue_item)
        return queue_items

    async def __create_queue_item(self, track):
        '''create CC queue item from track info '''
        return {
            'opt_itemId': track.queue_item_id,
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
                    'item_id': track.queue_item_id
                },
                'contentType': "audio/flac",
                'streamType': 'LIVE' if self.queue.use_queue_stream else 'BUFFERED',
                'metadata': {
                    'title': track.name,
                    'artist': track.artists[0].name if track.artists else "",
                },
                'duration': int(track.duration)
            }
        }
        
    def __send_player_queue(self, queuedata):
        '''send new data to the CC queue'''
        media_controller = self.cc.media_controller
        receiver_ctrl = media_controller._socket_client.receiver_controller
        def send_queue():
            """Plays media after chromecast has switched to requested app."""
            queuedata['mediaSessionId'] = media_controller.status.media_session_id
            media_controller.send_message(queuedata, inc_session_id=False)
        if not media_controller.status.media_session_id:
            receiver_ctrl.launch_app(media_controller.app_id, callback_function=send_queue)
        else:
            send_queue()

    def __update_group_members(self):
        ''' update group members '''
        if not self.mz:
            return
        try:
            self.mz.update_members()
        except Exception as exc:
            LOGGER.exception(exc)

    async def handle_player_state(self, caststatus=None, 
            mediastatus=None, connection_status=None):
        ''' handle a player state message from the socket '''
        # handle connection status
        if connection_status:
            if self.mz and connection_status.status == CONNECTION_STATUS_CONNECTED:
                self.mass.event_loop.call_soon_threadsafe(self.__update_group_members)
            elif connection_status.status == CONNECTION_STATUS_DISCONNECTED:
                # schedule a new scan which will handle group parent changes
                self.mass.event_loop.create_task(
                    self.mass.players.providers[self.player_provider].start_chromecast_discovery())
        # handle generic cast status
        if caststatus:
            self.muted = caststatus.volume_muted
            self.volume_level = caststatus.volume_level * 100
        self.name = self.cc.name
        # handle media status
        if mediastatus:
            if mediastatus.player_state in ['PLAYING', 'BUFFERING']:
                self.state = PlayerState.Playing
                self.powered = True
            elif mediastatus.player_state == 'PAUSED':
                self.state = PlayerState.Paused
            else:
                self.state = PlayerState.Stopped
            self.cur_uri = mediastatus.content_id
            self.cur_time = mediastatus.adjusted_current_time
            # create update/poll task for the current time
            async def poll_task():
                self.poll_task = True
                while self.state == PlayerState.Playing:
                    self.cur_time = mediastatus.adjusted_current_time
                    await asyncio.sleep(1)
                self.poll_task = False
            if not self.poll_task and self.state == PlayerState.Playing:
                self.mass.event_loop.create_task(poll_task())

class ChromecastProvider(PlayerProvider):
    ''' support for ChromeCast Audio '''
    
    def __init__(self, mass, conf):
        super().__init__(mass, conf)
        self.prov_id = PROV_ID
        self.name = PROV_NAME
        self._discovery_running = False
        logging.getLogger('pychromecast').setLevel(logging.WARNING)
        self.player_config_entries = PLAYER_CONFIG_ENTRIES

    async def setup(self):
        ''' perform async setup '''
        self.mass.event_loop.create_task(
                self.__periodic_chromecast_discovery())

    async def __handle_group_members_update(self, mz, added_player=None, removed_player=None):
        ''' handle callback from multizone manager '''
        if added_player:
            player = await self.get_player(added_player)
            group_player = await self.get_player(str(mz._uuid))
            if player and group_player:
                player.group_parent = group_player.player_id
                LOGGER.debug("player %s added to group %s" %(player.name, group_player.name))
        elif removed_player:
            player = await self.get_player(added_player)
            group_player = await self.get_player(str(mz._uuid))
            if player and group_player:
                player.group_parent = None
                LOGGER.debug("player %s removed from group %s" %(player.name, group_player.name))
        else:
            for member in mz.members:
                player = await self.get_player(member)
                if player:
                    LOGGER.debug("player %s added to group %s" %(player.name, str(mz._uuid)))
                    player.group_parent = str(mz._uuid)
    
    @run_periodic(1800)
    async def __periodic_chromecast_discovery(self):
        ''' run chromecast discovery on interval '''
        await self.start_chromecast_discovery()

    async def start_chromecast_discovery(self):
        ''' background non-blocking chromecast discovery and handler '''
        if self._discovery_running:
            return
        self._discovery_running = True
        LOGGER.debug("Chromecast discovery started...")
        # remove any disconnected players...
        removed_players = []
        for player in self.players:
            if not player.cc.socket_client or not player.cc.socket_client.is_connected:
                removed_players.append(player.player_id)
                for child_player in player.group_childs:
                    # update childs
                    child_player.group_parent = None
                # cleanup cast object
                del player.cc
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
            player = asyncio.run_coroutine_threadsafe(
                    self.get_player(player_id), 
                    self.mass.event_loop).result()
            if not player:
                asyncio.run_coroutine_threadsafe(
                        self.__chromecast_discovered(player_id, discovery_info), self.mass.event_loop)
        listener, browser = start_discovery(discovered_callback)
        await asyncio.sleep(15) # run discovery for 15 seconds
        stop_discovery(browser)
        LOGGER.debug("Chromecast discovery completed...")
        self._discovery_running = False
    
    async def __chromecast_discovered(self, player_id, discovery_info):
        ''' callback when a (new) chromecast device is discovered '''
        from pychromecast import _get_chromecast_from_host, ChromecastConnectionError
        try:
            chromecast = _get_chromecast_from_host(discovery_info, tries=2, timeout=5, retry_wait=5)
        except ChromecastConnectionError:
            LOGGER.warning("Could not connect to device %s" % player_id)
            return
        player = ChromecastPlayer(self.mass, player_id, self.prov_id)
        player.cc = chromecast
        player.mz = None
        player.poll_task = False
        self.supports_queue = True
        self.supports_gapless = False
        self.supports_crossfade = False
        # register status listeners
        status_listener = StatusListener(player_id, 
                player.handle_player_state, self.mass.event_loop)
        if chromecast.cast_type == 'group':
            player.is_group = True
            mz = MultizoneController(chromecast.uuid)
            mz.register_listener(MZListener(mz, self.__handle_group_members_update, self.mass.event_loop))
            chromecast.register_handler(mz)
            player.mz = mz
        chromecast.register_connection_listener(status_listener)
        chromecast.register_status_listener(status_listener)
        chromecast.media_controller.register_status_listener(status_listener)
        player.cc.wait()
        await self.add_player(player)


def chunks(l, n):
    """Yield successive n-sized chunks from l."""
    for i in range(0, len(l), n):
        yield l[i:i + n]


class StatusListener:
    def __init__(self, player_id, status_callback, loop):
        self.__handle_callback = status_callback
        self.loop = loop
        self.player_id = player_id
    def new_cast_status(self, status):
        ''' chromecast status changed (like volume etc.)'''
        asyncio.run_coroutine_threadsafe(
                self.__handle_callback(caststatus=status), self.loop)
    def new_media_status(self, status):
        ''' mediacontroller has new state '''
        asyncio.run_coroutine_threadsafe(
                self.__handle_callback(mediastatus=status), self.loop)
    def new_connection_status(self, status):
        ''' will be called when the connection changes '''
        asyncio.run_coroutine_threadsafe(
                self.__handle_callback(connection_status=status), self.loop)

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
