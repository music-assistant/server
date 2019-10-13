#!/usr/bin/env python3
# -*- coding:utf-8 -*-

import asyncio
from enum import Enum
from typing import List
import operator
from ..utils import run_periodic, LOGGER, parse_track_title, try_parse_int, try_parse_bool, try_parse_float
from ..constants import CONF_ENABLED
from ..cache import use_cache
from .media_types import Track, MediaType
from .player_queue import PlayerQueue, QueueItem
from .playerstate import PlayerState


class Player():
    ''' representation of a player '''

    #### Provider specific implementation, should be overridden ####

    async def cmd_stop(self):
        ''' [MUST OVERRIDE] send stop command to player '''
        raise NotImplementedError

    async def cmd_play(self):
        ''' [MUST OVERRIDE] send play (unpause) command to player '''
        raise NotImplementedError

    async def cmd_pause(self):
        ''' [MUST OVERRIDE] send pause command to player '''
        raise NotImplementedError

    async def cmd_next(self):
        ''' [CAN OVERRIDE] send next track command to player '''
        return await self.queue.play_index(self.queue.cur_index+1)

    async def cmd_previous(self):
        ''' [CAN OVERRIDE] send previous track command to player '''
        return await self.queue.play_index(self.queue.cur_index-1)
    
    async def cmd_power_on(self):
        ''' [MUST OVERRIDE] send power ON command to player '''
        raise NotImplementedError

    async def cmd_power_off(self):
        ''' [MUST OVERRIDE] send power TOGGLE command to player '''
        raise NotImplementedError

    async def cmd_volume_set(self, volume_level):
        ''' [MUST OVERRIDE] send new volume level command to player '''
        raise NotImplementedError

    async def cmd_volume_mute(self, is_muted=False):
        ''' [MUST OVERRIDE] send mute command to player '''
        raise NotImplementedError

    async def cmd_queue_play_index(self, index:int):
        '''
            [OVERRIDE IF SUPPORTED]
            play item at index X on player's queue
            :attrib index: (int) index of the queue item that should start playing
        '''
        raise NotImplementedError

    async def cmd_queue_load(self, queue_items):
        ''' 
            [OVERRIDE IF SUPPORTED]
            load/overwrite given items in the player's own queue implementation
            :param queue_items: a list of QueueItems
        '''
        raise NotImplementedError

    async def cmd_queue_insert(self, queue_items, offset=0):
        ''' 
            [OVERRIDE IF SUPPORTED]
            insert new items at position X into existing queue
            if offset 0 or None, will start playing newly added item(s)
                :param queue_items: a list of QueueItems
                :param offset: offset from current queue position to insert new items
        '''
        raise NotImplementedError

    async def cmd_queue_append(self, queue_items):
        ''' 
            append new items at the end of the queue
            :param queue_items: a list of QueueItems
        '''
        raise NotImplementedError

    async def cmd_play_uri(self, uri:str):
        '''
            [MUST OVERRIDE]
            tell player to start playing a single uri
        '''
        raise NotImplementedError

    #### Common implementation, should NOT be overrridden #####

    def __init__(self, mass, player_id, prov_id):
        # private attributes
        self.mass = mass
        self._player_id = player_id # unique id for this player
        self._prov_id = prov_id # unique provider id for the player
        self._name = ''
        self._is_group = False 
        self._state = PlayerState.Stopped 
        self._powered = False 
        self._cur_time = 0
        self._cur_uri = ''
        self._volume_level = 0
        self._muted = False
        self._group_parent = None
        self._queue = PlayerQueue(mass, self)
        self._player_settings = None
        # public attributes
        self.supports_queue = True # has native support for a queue
        self.supports_gapless = True # has native gapless support
        self.supports_crossfade = False # has native crossfading support
        self.supports_replay_gain = False # has native support for replaygain volume leveling
        # if home assistant support is enabled, register state listener
        if self.mass.hass:
            self.mass.event_loop.create_task(
                self.mass.add_event_listener(self.hass_state_listener, "hass entity changed"))

    @property
    def player_id(self):
        ''' [PROTECTED] player_id of this player '''
        return self._player_id

    @property
    def player_provider(self):
        ''' [PROTECTED] provider id of this player '''
        return self._prov_id

    @property
    def name(self):
        ''' [PROTECTED] name of this player '''
        if self.settings.get('name'):
            return self.settings['name']
        else:
            return self._name

    @name.setter
    def name(self, name):
        ''' [PROTECTED] set (real) name of this player '''
        if name != self._name:
            self._name = name
            self.mass.event_loop.create_task(self.update())

    @property
    def is_group(self):
        ''' [PROTECTED] is_group property of this player '''
        return self._is_group

    @is_group.setter
    def is_group(self, is_group:bool):
        ''' [PROTECTED] set is_group property of this player '''
        if is_group != self._is_group:
            self._is_group = is_group
            self.mass.event_loop.create_task(self.update())

    @property
    def state(self):
        ''' [PROTECTED] state property of this player '''
        if not self.powered:
            return PlayerState.Off
        if self.group_parent:
            group_player = self.mass.bg_executor.submit(asyncio.run, 
                self.mass.player.get_player(self.group_parent)).result()
            if group_player:
                return group_player.state
        return self._state

    @state.setter
    def state(self, state:PlayerState):
        ''' [PROTECTED] set state property of this player '''
        if state != self._state:
            self._state = state
            self.mass.event_loop.create_task(self.update())

    @property
    def powered(self):
        ''' [PROTECTED] return power state for this player '''
        # homeassistant integration
        if (self.mass.hass and self.settings.get('hass_power_entity') and 
                self.settings.get('hass_power_entity_source')):
            hass_state = self.mass.hass.get_state(
                    self.settings['hass_power_entity'],
                    attribute='source')
            return hass_state == self.settings['hass_power_entity_source']
        elif self.mass.hass and self.settings.get('hass_power_entity'):
            hass_state = self.mass.hass.get_state(
                    self.settings['hass_power_entity'])
            return hass_state != 'off'
        # mute as power
        elif self.settings.get('mute_as_power'):
            return not self.muted
        else:
            return self._powered

    @powered.setter
    def powered(self, powered):
        ''' [PROTECTED] set (real) power state for this player '''
        if powered != self._powered:
            self._powered = powered
        self.mass.event_loop.create_task(self.update())

    @property
    def cur_time(self):
        ''' [PROTECTED] cur_time (player's elapsed time) property of this player '''
        # handle group player
        if self.group_parent:
            group_player = self.mass.player.get_player_sync(self.group_parent)
            if group_player:
                return group_player.cur_time
        return self.queue.cur_item_time

    @cur_time.setter
    def cur_time(self, cur_time:int):
        ''' [PROTECTED] set cur_time (player's elapsed time) property of this player '''
        if cur_time != self._cur_time:
            self._cur_time = cur_time
            self.mass.event_loop.create_task(self.update())

    @property
    def cur_uri(self):
        ''' [PROTECTED] cur_uri (uri loaded in player) property of this player '''
        # handle group player
        if self.group_parent:
            group_player = self.mass.player.get_player_sync(self.group_parent)
            if group_player:
                return group_player.cur_uri
        return self._cur_uri

    @cur_uri.setter
    def cur_uri(self, cur_uri:str):
        ''' [PROTECTED] set cur_uri (uri loaded in player) property of this player '''
        if cur_uri != self._cur_uri:
            self._cur_uri = cur_uri
            self.mass.event_loop.create_task(self.update())

    @property
    def volume_level(self):
        ''' [PROTECTED] volume_level property of this player '''
        # handle group volume
        if self.is_group:
            group_volume = 0
            active_players = 0
            for child_player in self.group_childs:
                if child_player.enabled and child_player.powered:
                    group_volume += child_player.volume_level
                    active_players += 1
            if active_players:
                group_volume = group_volume / active_players
            return group_volume
        # handle hass integration
        elif self.mass.hass and self.settings.get('hass_volume_entity'):
            hass_state = self.mass.hass.get_state(
                    self.settings['hass_volume_entity'],
                    attribute='volume_level')
            return int(try_parse_float(hass_state)*100)
        else:
            return self._volume_level

    @volume_level.setter
    def volume_level(self, volume_level:int):
        ''' [PROTECTED] set volume_level property of this player '''
        volume_level = try_parse_int(volume_level)
        if volume_level != self._volume_level:
            self._volume_level = volume_level
            self.mass.event_loop.create_task(self.update())

    @property
    def muted(self):
        ''' [PROTECTED] muted property of this player '''
        return self._muted

    @muted.setter
    def muted(self, is_muted:bool):
        ''' [PROTECTED] set muted property of this player '''
        is_muted = try_parse_bool(is_muted)
        if is_muted != self._muted:
            self._muted = is_muted
            self.mass.event_loop.create_task(self.update())

    @property
    def group_parent(self):
        ''' [PROTECTED] group_parent property of this player '''
        return self._group_parent

    @group_parent.setter
    def group_parent(self, group_parent:str):
        ''' [PROTECTED] set muted property of this player '''
        if group_parent != self._group_parent:
            self._group_parent = group_parent
            self.mass.create_task(self.update())

    @property
    def group_childs(self):
        ''' [PROTECTED] return group childs '''
        if not self.is_group:
            return []
        return [item for item in self.mass.player.players if item.group_parent == self.player_id]

    @property
    def enabled(self):
        ''' [PROTECTED] player enabled config setting '''
        return self.settings.get('enabled')

    @property
    def queue(self):
        ''' [PROTECTED] player's queue '''
        # handle group player
        if self.group_parent:
            group_player = self.mass.player.get_player_sync(self.group_parent)
            if group_player:
                return group_player.queue
        return self._queue

    @property
    def cur_item(self):
        ''' current item in the player's queue '''
        return self.queue.cur_item

    async def stop(self):
        ''' [PROTECTED] send stop command to player '''
        if self.group_parent:
            # redirect playback related commands to parent player
            group_player = await self.mass.player.get_player(self.group_parent)
            if group_player:
                return await group_player.stop()
        else:
            return await self.cmd_stop()

    async def play(self):
        ''' [PROTECTED] send play (unpause) command to player '''
        if self.group_parent:
            # redirect playback related commands to parent player
            group_player = await self.mass.player.get_player(self.group_parent)
            if group_player:
                return await group_player.play()
        elif self.state == PlayerState.Paused:
            return await self.cmd_play()
        elif self.state != PlayerState.Playing:
            return await self.queue.resume()

    async def pause(self):
        ''' [PROTECTED] send pause command to player '''
        if self.group_parent:
            # redirect playback related commands to parent player
            group_player = await self.mass.player.get_player(self.group_parent)
            if group_player:
                return await group_player.pause()
        else:
            return await self.cmd_pause()
    
    async def play_pause(self):
        ''' toggle play/pause'''
        if self.state == PlayerState.Playing:
            return await self.pause()
        else:
            return await self.play()
    
    async def next(self):
        ''' [PROTECTED] send next command to player '''
        if self.group_parent:
            # redirect playback related commands to parent player
            group_player = await self.mass.player.get_player(self.group_parent)
            if group_player:
                return await group_player.next()
        else:
            return await self.queue.next()

    async def previous(self):
        ''' [PROTECTED] send previous command to player '''
        if self.group_parent:
            # redirect playback related commands to parent player
            group_player = await self.mass.player.get_player(self.group_parent)
            if group_player:
                return await group_player.previous()
        else:
            return await self.queue.previous()
    
    async def power(self, power):
        ''' [PROTECTED] send power ON command to player '''
        power = try_parse_bool(power)
        if power:
            return await self.power_on()
        else:
            return await self.power_off()

    async def power_on(self):
        ''' [PROTECTED] send power ON command to player '''
        await self.cmd_power_on()
        # handle mute as power
        if self.settings['mute_as_power']:
            await self.volume_mute(False)
        # handle hass integration
        if (self.mass.hass and 
                self.settings.get('hass_power_entity') and 
                self.settings.get('hass_power_entity_source')):
            cur_source = await self.mass.hass.get_state_async(
                        self.settings['hass_power_entity'], attribute='source')
            if not cur_source:
                service_data = { 
                    'entity_id': self.settings['hass_power_entity'], 
                    'source': self.settings['hass_power_entity_source'] 
                }
                await self.mass.hass.call_service('media_player', 'select_source', service_data)
        elif self.settings.get('hass_power_entity'):
            domain = self.settings['hass_power_entity'].split('.')[0]
            service_data = { 'entity_id': self.settings['hass_power_entity']}
            await self.mass.hass.call_service(domain, 'turn_on', service_data)
        # handle play on power on
        if self.settings['play_power_on']:
            await self.play()
        # handle group power
        if self.group_parent:
            # player has a group parent, check if it should be turned on
            group_player = await self.mass.player.get_player(self.group_parent)
            if group_player and not group_player.powered:
                return await group_player.power_on()

    async def power_off(self):
        ''' [PROTECTED] send power TOGGLE command to player '''
        await self.cmd_power_off()
        # handle mute as power
        if self.settings['mute_as_power']:
            await self.volume_mute(True)
        # handle hass integration
        if (self.mass.hass and 
                self.settings.get('hass_power_entity') and 
                self.settings.get('hass_power_entity_source')):
            cur_source = await self.mass.hass.get_state_async(
                    self.settings['hass_power_entity'], attribute='source')
            if cur_source == self.settings['hass_power_entity_source']:
                service_data = { 'entity_id': self.settings['hass_power_entity'] }
                await self.mass.hass.call_service('media_player', 'turn_off', service_data)
        elif self.mass.hass and self.settings.get('hass_power_entity'):
            domain = self.settings['hass_power_entity'].split('.')[0]
            service_data = { 'entity_id': self.settings['hass_power_entity']}
            await self.mass.hass.call_service(domain, 'turn_off', service_data)
        # handle group power
        if self.is_group:
            # player is group, turn off all childs
            for item in self.group_childs:
                if item.powered:
                    await item.power_off()
        elif self.group_parent:
            # player has a group parent, check if it should be turned off
            group_player = await self.mass.player.get_player(self.group_parent)
            if group_player.powered:
                needs_power = False
                for child_player in group_player.group_childs:
                    if child_player.player_id != self.player_id and child_player.powered:
                        needs_power = True
                        break
                if not needs_power:
                    await group_player.power_off()

    async def power_toggle(self):
        ''' [PROTECTED] send toggle power command to player '''
        if self.powered:
            return await self.power_off()
        else:
            return await self.power_on()

    async def volume_set(self, volume_level):
        ''' [PROTECTED] send new volume level command to player '''
        volume_level = try_parse_int(volume_level)
        # handle group volume
        if self.is_group:
            cur_volume = self.volume_level
            new_volume = volume_level
            volume_dif = new_volume - cur_volume
            if cur_volume == 0:
                volume_dif_percent = 1+(new_volume/100)
            else:
                volume_dif_percent = volume_dif/cur_volume
            for child_player in self.group_childs:
                if child_player.enabled and child_player.powered:
                    cur_child_volume = child_player.volume_level
                    new_child_volume = cur_child_volume + (cur_child_volume * volume_dif_percent)
                    await child_player.volume_set(new_child_volume)
        # handle hass integration
        elif self.mass.hass and self.settings.get('hass_volume_entity'):
            service_data = { 
                'entity_id': self.settings['hass_volume_entity'], 
                'volume_level': volume_level/100
            }
            await self.mass.hass.call_service('media_player', 'volume_set', service_data)
            await self.cmd_volume_set(100) # just force full volume on actual player if volume is outsourced to hass
        else:
            await self.cmd_volume_set(volume_level)

    async def volume_up(self):
        ''' [PROTECTED] send volume up command to player '''
        new_level = self.volume_level + 1
        if new_level > 100:
            new_level = 100
        return await self.volume_set(new_level)

    async def volume_down(self):
        ''' [PROTECTED] send volume down command to player '''
        new_level = self.volume_level - 1
        if new_level < 0:
            new_level = 0
        return await self.volume_set(new_level)

    async def volume_mute(self, is_muted=False):
        ''' [PROTECTED] send mute command to player '''
        return await self.cmd_volume_mute(is_muted)

    async def update(self):
        ''' [PROTECTED] signal player updated '''
        await self.queue.update()
        LOGGER.debug("player changed: %s" % self.name)
        await self.mass.signal_event('player changed', self)
        self.get_player_settings()
    
    async def hass_state_listener(self, msg, msg_details=None):
        ''' called when tracked entities in hass change state '''
        if (msg_details == self.settings.get('hass_power_entity') or 
                msg_details == self.settings.get('hass_volume_entity')):
            await self.update()

    @property
    def settings(self):
        ''' [PROTECTED] get (or create) player config settings '''
        if self._player_settings:
            return self._player_settings
        else:
            return self.get_player_settings()

    def get_player_settings(self):
        ''' [PROTECTED] get (or create) player config settings '''
        player_settings = self.mass.config['player_settings'].get(self.player_id,{})
        # generate config for the player
        config_entries = [ # default config entries for a player
            ("enabled", True, "player_enabled"),
            ("name", "", "player_name"),
            ("mute_as_power", False, "player_mute_power"),
            ("max_sample_rate", 96000, "max_sample_rate"),
            ('volume_normalisation', True, 'enable_r128_volume_normalisation'), 
            ('target_volume', '-23', 'target_volume_lufs'),
            ('fallback_gain_correct', '-12', 'fallback_gain_correct'),
            ("crossfade_duration", 0, "crossfade_duration"),
        ]
        # append player specific settings
        config_entries += self.mass.player.providers[self._prov_id].player_config_entries
        if self.is_group or not self.group_parent:
            config_entries += [ # play on power on setting
                ("play_power_on", False, "player_power_play"),
            ]
        if self.mass.config['base'].get('homeassistant',{}).get("enabled"):
            # append hass specific config entries
            config_entries += [("hass_power_entity", "", "hass_player_power"),
                            ("hass_power_entity_source", "", "hass_player_source"),
                            ("hass_volume_entity", "", "hass_player_volume")]
        for key, def_value, desc in config_entries:
            if not key in player_settings:
                if (isinstance(def_value, str) and def_value.startswith('<')):
                    player_settings[key] = None
                else:
                    player_settings[key] = def_value
        self.mass.config['player_settings'][self.player_id] = player_settings
        self.mass.config['player_settings'][self.player_id]['__desc__'] = config_entries
        self._player_settings = self.mass.config['player_settings'][self.player_id]
        return player_settings 
    
    def to_dict(self):
        ''' instance attributes as dict so it can be serialized to json '''
        return {
            "player_id": self.player_id,
            "player_provider": self.player_provider,
            "name": self.name,
            "is_group": self.is_group,
            "state": self.state,
            "powered": self.powered,
            "cur_time": self.cur_time,
            "cur_uri": self.cur_uri,
            "volume_level": self.volume_level,
            "muted": self.muted,
            "group_parent": self.group_parent,
            "enabled": self.enabled,
            "cur_item": self.cur_item.__dict__ if self.cur_item else None
        }