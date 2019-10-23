#!/usr/bin/env python3
# -*- coding:utf-8 -*-

import asyncio
import aiohttp
from typing import List
import logging
import types

from ..utils import run_periodic, LOGGER, try_parse_int
from ..models.playerprovider import PlayerProvider
from ..models.player import Player, PlayerState
from ..models.playerstate import PlayerState
from ..models.player_queue import QueueItem, PlayerQueue
from ..constants import CONF_ENABLED, CONF_HOSTNAME, CONF_PORT

PROV_ID = 'sonos'
PROV_NAME = 'Sonos'
PROV_CLASS = 'SonosProvider'

CONFIG_ENTRIES = [
    (CONF_ENABLED, False, CONF_ENABLED),
    ]

PLAYER_CONFIG_ENTRIES = []

class SonosPlayer(Player):
    ''' Sonos player object '''
    
    async def cmd_stop(self):
        ''' send stop command to player '''
        self.soco.stop()

    async def cmd_play(self):
        ''' send play command to player '''
        self.soco.play()

    async def cmd_pause(self):
        ''' send pause command to player '''
        self.soco.pause()

    async def cmd_next(self):
        ''' send next track command to player '''
        self.soco.next()

    async def cmd_previous(self):
        ''' send previous track command to player '''
        self.soco.previous()
    
    async def cmd_power_on(self):
        ''' send power ON command to player '''
        self.powered = True

    async def cmd_power_off(self):
        ''' send power OFF command to player '''
        self.powered = False
        # power is not supported so send stop instead
        self.soco.stop()

    async def cmd_volume_set(self, volume_level):
        ''' send new volume level command to player '''
        self.soco.volume = volume_level

    async def cmd_volume_mute(self, is_muted=False):
        ''' send mute command to player '''
        self.soco.mute = is_muted

    async def cmd_play_uri(self, uri:str):
        ''' play single uri on player '''
        self.soco.play_uri(uri)

    async def cmd_queue_play_index(self, index:int):
        '''
            play item at index X on player's queue
            :attrib index: (int) index of the queue item that should start playing
        '''
        self.soco.play_from_queue(index)

    async def cmd_queue_load(self, queue_items:List[QueueItem]):
        ''' load (overwrite) queue with new items '''
        self.soco.clear_queue()
        for pos, item in enumerate(queue_items):
            self.soco.add_uri_to_queue(item.uri, pos)

    async def cmd_queue_insert(self, queue_items:List[QueueItem], insert_at_index):
        for pos, item in enumerate(queue_items):
            self.soco.add_uri_to_queue(item.uri, insert_at_index+pos)

    async def cmd_queue_append(self, queue_items:List[QueueItem]):
        ''' 
            append new items at the end of the queue
        '''
        last_index = len(self.queue.items)
        for pos, item in enumerate(queue_items):
            self.soco.add_uri_to_queue(item.uri, last_index+pos)

    def _update_state(self, event=None):
        ''' update state, triggerer by event '''
        if event:
            variables = event.variables
            if "volume" in variables:
                self.volume_level = int(variables["volume"]["Master"])
            if "mute" in variables:
                self.muted = variables["mute"]["Master"] == "1"
        else:
            self.volume_level = self.soco.volume
            self.muted = self.soco.mute
        transport_info = self.soco.get_current_transport_info()
        current_transport_state = transport_info.get("current_transport_state")
        if current_transport_state == "TRANSITIONING":
            return
        if self.soco.is_playing_tv or self.soco.is_playing_line_in:
            self.powered = False
        else:
            new_state = self.__convert_state(current_transport_state)
            self.state = new_state
            track_info = self.soco.get_current_track_info()
            self.cur_uri = track_info["uri"]
            position_info = self.soco.avTransport.GetPositionInfo(
                    [("InstanceID", 0), ("Channel", "Master")])
            rel_time = self.__timespan_secs(position_info.get("RelTime"))
            self.cur_time = rel_time

    @staticmethod
    def __convert_state(sonos_state):
        ''' convert sonos state to internal state '''
        if sonos_state == 'PLAYING':
            return PlayerState.Playing
        elif sonos_state == 'PAUSED_PLAYBACK':
            return PlayerState.Paused
        else:
            return PlayerState.Stopped

    @staticmethod
    def __timespan_secs(timespan):
        """Parse a time-span into number of seconds."""
        if timespan in ("", "NOT_IMPLEMENTED", None):
            return None
        return sum(60 ** x[0] * int(x[1]) for x in enumerate(reversed(timespan.split(":"))))
        

class SonosProvider(PlayerProvider):
    ''' support for Sonos speakers '''
    
    def __init__(self, mass, conf):
        super().__init__(mass, conf)
        self.prov_id = PROV_ID
        self.name = PROV_NAME
        self._discovery_running = False
        self.player_config_entries = PLAYER_CONFIG_ENTRIES

    async def setup(self):
        ''' perform async setup '''
        self.mass.event_loop.create_task(
                self.__periodic_discovery())

    @run_periodic(1800)
    async def __periodic_discovery(self):
        ''' run sonos discovery on interval '''
        await self.run_discovery()

    async def run_discovery(self):
        ''' background sonos discovery and handler '''
        if self._discovery_running:
            return
        self._discovery_running = True
        LOGGER.debug("Sonos discovery started...")
        import soco
        discovered_devices = soco.discover()
        new_device_ids = [item.uid for item in discovered_devices]
        cur_player_ids = [item.player_id for item in self.players]
        # remove any disconnected players...
        for player in self.players:
            if not player.is_group and not player.soco.uid in new_device_ids:
                await self.remove_player(player.player_id)
        # process new players
        for device in discovered_devices:
            if device.uid not in cur_player_ids and device.is_visible:
                await self.__device_discovered(device)
        # handle groups
        if len(discovered_devices) > 0:
            await self.__process_groups(discovered_devices[0].all_groups)
        else:
            await self.__process_groups([])

    async def __device_discovered(self, soco_device):
        '''handle new player '''
        player = SonosPlayer(self.mass, soco_device.uid, self.prov_id)
        player.soco = soco_device
        player.name = soco_device.player_name
        self.supports_queue = True
        self.supports_gapless = True
        self.supports_crossfade = True
        player._subscriptions = []
        player._media_position_updated_at = None
        # handle subscriptions to events
        def subscribe(service, action):
            queue = _ProcessSonosEventQueue(action)
            sub = service.subscribe(auto_renew=True, event_queue=queue)
            player._subscriptions.append(sub)
        subscribe(soco_device.avTransport, player._update_state)
        subscribe(soco_device.renderingControl, player._update_state)
        subscribe(soco_device.zoneGroupTopology, self.__topology_changed)
        return await self.add_player(player)

    async def __process_groups(self, sonos_groups):
        ''' process all sonos groups '''
        all_group_ids = []
        for group in sonos_groups:
            all_group_ids.append(group.uid)
            if group.uid not in self.mass.players._players:
                # new group player
                group_player = await self.__device_discovered(group.coordinator)
            else:
                group_player = await self.get_player(group.uid)
            # check members
            group_player.name = group.label
            group_player.group_childs = [item.uid for item in group.members]
            
    def __topology_changed(self, event=None):
        ''' 
            received topology changed event 
            from one of the sonos players
            schedule discovery to work out the changes
        '''
        self.mass.event_loop.create_task(self.run_discovery())

class _ProcessSonosEventQueue:
    """Queue like object for dispatching sonos events."""

    def __init__(self, handler):
        """Initialize Sonos event queue."""
        self._handler = handler

    def put(self, item, block=True, timeout=None):
        """Process event."""
        try:
            self._handler(item)
        except Exception as ex:
            LOGGER.warning("Error calling %s: %s", self._handler, ex)