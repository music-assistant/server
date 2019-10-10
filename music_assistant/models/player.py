#!/usr/bin/env python3
# -*- coding:utf-8 -*-

from enum import Enum
from typing import List
from ..utils import run_periodic, LOGGER, parse_track_title, try_parse_int, try_parse_bool, try_parse_float
from ..constants import CONF_ENABLED
from ..modules.cache import use_cache
from media_types import Track, MediaType
from player_queue import PlayerQueue, QueueItem


class PlayerState(str, Enum):
    Off = "off"
    Stopped = "stopped"
    Paused = "paused"
    Playing = "playing"

class Player():
    ''' representation of a player '''

    #### Provider specific implementation, should be overridden ####

    async def get_config_entries(self):
        ''' [MAY OVERRIDE] get the player-specific config entries for this player (list with key/value pairs)'''
        return []

    async def __stop(self):
        ''' [MUST OVERRIDE] send stop command to player '''
        raise NotImplementedError

    async def __play(self):
        ''' [MUST OVERRIDE] send play (unpause) command to player '''
        raise NotImplementedError

    async def __pause(self):
        ''' [MUST OVERRIDE] send pause command to player '''
        raise NotImplementedError
    
    async def __power_on(self):
        ''' [MUST OVERRIDE] send power ON command to player '''
        raise NotImplementedError

    async def __power_off(self):
        ''' [MUST OVERRIDE] send power TOGGLE command to player '''
        raise NotImplementedError

    async def __volume_set(self, volume_level):
        ''' [MUST OVERRIDE] send new volume level command to player '''
        raise NotImplementedError

    async def __volume_mute(self, is_muted=False):
        ''' [MUST OVERRIDE] send mute command to player '''
        raise NotImplementedError

    async def __play_queue(self):
        ''' [MUST OVERRIDE] tell player to start playing the queue '''
        raise NotImplementedError

    #### Common implementation, should NOT be overrridden #####

    def __init__(self, mass, player_id, prov_id):
        self.mass = mass
        self._player_id = player_id
        self._prov_id = prov_id
        self._name = ''
        self._is_group = False
        self._state = PlayerState.Stopped
        self._powered = False
        self._cur_time = 0
        self._volume_level = 0
        self._muted = False
        self._group_parent = None
        self._queue = PlayerQueue(mass, self)

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
            group_player = self.mass.event_loop.run_until_complete(
                    self.mass.player.get_player(self.group_parent))
            if group_player:
                return group_player.state
        return self._state

    @state.setter
    def state(self, state:PlayerState):
        ''' [PROTECTED] set state property of this player '''
        if state != self.state:
            self._state = state
            self.mass.event_loop.create_task(self.update())

    @property
    def powered(self):
        ''' [PROTECTED] return power state for this player '''
        # homeassistant integration
        if self.mass.hass and self.settings.get('hass_power_entity') and self.settings.get('hass_power_entity_source'):
            hass_state = self.mass.event_loop.run_until_complete(
                self.mass.hass.get_state(
                    self.settings['hass_power_entity'],
                    attribute='source',
                    register_listener=self.update()))
            return hass_state == self.settings['hass_power_entity_source']
        elif self.settings.get('hass_power_entity'):
            hass_state = self.mass.event_loop.run_until_complete(
                self.mass.hass.get_state(
                    self.settings['hass_power_entity'],
                    attribute='state',
                    register_listener=self.update()))
            return hass_state != 'off'
        # mute as power
        elif self.settings.get('mute_as_power'):
            return self.muted
        else:
            return self._powered

    @powered.setter
    def powered(self, powered):
        ''' [PROTECTED] set (real) power state for this player '''
        self._powered = powered

    @property
    def cur_time(self):
        ''' [PROTECTED] cur_time (player's elapsed time) property of this player '''
        # handle group player
        if self.group_parent:
            group_player = self.mass.event_loop.run_until_complete(
                    self.mass.player.get_player(self.group_parent))
            if group_player:
                return group_player.cur_time
        return self._cur_time

    @cur_time.setter
    def cur_time(self, cur_time:int):
        ''' [PROTECTED] set cur_time (player's elapsed time) property of this player '''
        if cur_time != self._cur_time:
            self._cur_time = cur_time
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
            hass_state = self.mass.event_loop.run_until_complete(
                self.mass.hass.get_state(
                    self.settings['hass_volume_entity'], 
                    attribute='volume_level',
                    register_listener=self.update()))
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
    def settings(self):
        ''' [PROTECTED] get the player config settings '''
        player_settings = self.mass.config['player_settings'].get(self.player_id)
        if not player_settings:
            return self.mass.event_loop.run_until_complete(self.__update_player_settings())

    @property
    def enabled(self):
        ''' [PROTECTED] player enabled config setting '''
        return self.settings.get('enabled')

    @property
    def queue(self):
        ''' [PROTECTED] player's queue '''
        # handle group player
        if self.group_parent:
            group_player = self.mass.event_loop.run_until_complete(
                    self.mass.player.get_player(self.group_parent))
            if group_player:
                return group_player.queue
        return self._queue

    async def stop(self):
        ''' [PROTECTED] send stop command to player '''
        if self.group_parent:
            # redirect playback related commands to parent player
            group_player = await self.mass.player.get(self.group_parent)
            if group_player:
                return await group_player.stop()
        else:
            return await self.__stop()

    async def play(self):
        ''' [PROTECTED] send play (unpause) command to player '''
        if self.group_parent:
            # redirect playback related commands to parent player
            group_player = await self.mass.player.get_player(self.group_parent)
            if group_player:
                return await group_player.play()
        elif self.state == PlayerState.Paused:
            return await self.__play()
        elif self.state != PlayerState.Playing:
            return await self.play_queue()

    async def pause(self):
        ''' [PROTECTED] send pause command to player '''
        if self.group_parent:
            # redirect playback related commands to parent player
            group_player = await self.mass.player.get_player(self.group_parent)
            if group_player:
                return await group_player.pause()
        else:
            return await self.__pause()
    
    async def power_on(self):
        ''' [PROTECTED] send power ON command to player '''
        self.__power_on()
        # handle mute as power
        if self.settings['mute_as_power']:
            self.volume_mute(False)
        # handle hass integration
        if self.mass.hass and self.settings.get('hass_power_entity') and self.settings.get('hass_power_entity_source'):
            cur_source = await self.mass.hass.get_state(self.settings['hass_power_entity'], attribute='source')
            if not cur_source:
                service_data = { 
                    'entity_id': self.settings['hass_power_entity'], 
                    'source':self.settings['hass_power_entity_source'] 
                }
                await self.mass.hass.call_service('media_player', 'select_source', service_data)
        elif self.settings.get('hass_power_entity'):
            domain = self.settings['hass_power_entity'].split('.')[0]
            service_data = { 'entity_id': self.settings['hass_power_entity']}
            await self.mass.hass.call_service(domain, 'turn_on', service_data)
        # handle play on power on
        if self.settings['play_power_on']:
            self.play()
        # handle group power
        if self.group_parent:
            # player has a group parent, check if it should be turned on
            group_player = await self.mass.player.get_player(self.group_parent)
            if group_player and not group_player.powered:
                return await group_player.power_on()

    async def power_off(self):
        ''' [PROTECTED] send power TOGGLE command to player '''
        self.__power_off()
        # handle mute as power
        if self.settings['mute_as_power']:
            self.volume_mute(True)
        # handle hass integration
        if self.mass.hass and self.settings.get('hass_power_entity') and self.settings.get('hass_power_entity_source'):
            cur_source = await self.mass.hass.get_state(self.settings['hass_power_entity'], attribute='source')
            if cur_source == self.settings['hass_power_entity_source']:
                service_data = { 'entity_id': self.settings['hass_power_entity'] }
                await self.mass.hass.call_service('media_player', 'turn_off', service_data)
        elif self.mass.hass and self.settings.get('hass_power_entity'):
            domain = self.settings['hass_power_entity'].split('.')[0]
            service_data = { 'entity_id': self.settings['hass_power_entity']}
            await self.mass.hass.call_service(domain, 'turn_ff', service_data)
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
            await self.__volume_set(100) # just force full volume on actual player if volume is outsourced to hass
        else:
            await self.__volume_set(volume_level)

    async def volume_up(self):
        ''' [MAY OVERRIDE] send volume up command to player '''
        new_level = self.volume_level + 1
        return await self.volume_set(new_level)

    async def volume_down(self):
        ''' [MAY OVERRIDE] send volume down command to player '''
        new_level = self.volume_level - 1
        if new_level < 0:
            new_level = 0
        return await self.volume_set(new_level)

    async def volume_mute(self, is_muted=False):
        ''' [MUST OVERRIDE] send mute command to player '''
        return await self.__volume_mute(is_muted)

    async def play_queue(self):
        ''' [PROTECTED] send play_queue (start stream) command to player '''
        if self.group_parent:
            # redirect playback related commands to parent player
            group_player = await self.mass.player.get_player(self.group_parent)
            if group_player:
                return await group_player.play_queue()
        elif self.queue.items:
            return await self.__play_queue()

    async def play_media(self, media_item, queue_opt='play'):
        ''' 
            play media item(s) on this player 
            media_item: media item(s) that should be played (Track, Album, Artist, Playlist, Radio)
                        single item or list of items
            queue_opt: 
                play -> insert new items in queue and start playing at the inserted position
                replace -> replace queue contents with these items
                next -> play item(s) after current playing item
                add -> append new items at end of the queue
        '''
        # a single item or list of items may be provided
        media_items = media_item if isinstance(media_item, list) else [media_item]
        queue_tracks = []
        for media_item in media_items:
            # collect tracks to play
            if media_item.media_type == MediaType.Artist:
                tracks = await self.mass.music.artist_toptracks(media_item.item_id, 
                        provider=media_item.provider)
            elif media_item.media_type == MediaType.Album:
                tracks = await self.mass.music.album_tracks(media_item.item_id, 
                        provider=media_item.provider)
            elif media_item.media_type == MediaType.Playlist:
                tracks = await self.mass.music.playlist_tracks(media_item.item_id, 
                        provider=media_item.provider, offset=0, limit=0) 
            else:
                tracks = [media_item] # single track
            for track in tracks:
                queue_item = QueueItem()
                queue_item.name = track.name
                queue_item.artists = track.artists
                queue_item.album = track.album
                queue_item.duration = track.duration
                queue_item.version = track.version
                queue_item.metadata = track.metadata
                queue_item.media_type = track.media_type
                queue_item.uri = 'http://%s:%s/stream_queue?player_id=%s'% (
                        self.local_ip, self.mass.config['base']['web']['http_port'], player_id)
                # sort by quality and check track availability
                for prov_media in sorted(track.provider_ids, key=operator.itemgetter('quality'), reverse=True):
                    media_provider = prov_media['provider']
                    media_item_id = prov_media['item_id']
                    player_supported_provs = player_prov.supported_musicproviders
                    if media_provider in player_supported_provs and not self.mass.config['player_settings'][player_id]['force_http_streamer']:
                        # the provider can handle this media_type directly !
                        track.uri = await self.get_track_uri(media_item_id, media_provider, player_id, is_radio=is_radio)
                        playable_tracks.append(track)
                        match_found = True
                    elif 'http' in player_prov.supported_musicproviders:
                        # fallback to http streaming if supported
                        track.uri = await self.get_track_uri(media_item_id, media_provider, player_id, True, is_radio=is_radio)
                        queue_tracks.append(track)
                        match_found = True
                    if match_found:
                        break
        if queue_tracks:
            if self._players[player_id].shuffle_enabled:
                random.shuffle(playable_tracks)
            if queue_opt in ['next', 'play'] and len(playable_tracks) > 1:
                queue_opt = 'replace' # always assume playback of multiple items as new queue
            return await player_prov.play_media(player_id, playable_tracks, queue_opt)
        else:
            raise Exception("Musicprovider and/or media not supported by player %s !" % (player_id) )
    
    async def update(self):
        ''' [PROTECTED] signal player updated '''
        self.__update_player_settings()
        LOGGER.info("player updated: %s" % self.name)
        self.mass.signal_event('player changed', self)
    
    async def __update_player_settings(self):
        ''' [PROTECTED] get (or create) player config settings '''
        config_entries = [ # default config entries for a player
            ("enabled", False, "player_enabled"),
            ("name", "", "player_name"),
            ("mute_as_power", False, "player_mute_power"),
            ("max_sample_rate", 96000, "max_sample_rate"),
            ('volume_normalisation', True, 'enable_r128_volume_normalisation'), 
            ('target_volume', '-23', 'target_volume_lufs'),
            ('fallback_gain_correct', '-12', 'fallback_gain_correct')
        ]
        # append player specific settings
        config_entries += await self.get_config_entries()
        if self.is_group or not self.group_parent:
            config_entries += [ # play on power on setting
                ("play_power_on", False, "player_power_play"),
            ]
        if self.mass.config['base'].get('homeassistant',{}).get("enabled"):
            # append hass specific config entries
            config_entries += [("hass_power_entity", "", "hass_player_power"),
                            ("hass_power_entity_source", "", "hass_player_source"),
                            ("hass_volume_entity", "", "hass_player_volume")]
        player_settings = self.mass.config['player_settings'].get(self.player_id,{})
        for key, def_value, desc in config_entries:
            if not key in player_settings:
                if (isinstance(def_value, str) and def_value.startswith('<')):
                    player_settings[key] = None
                else:
                    player_settings[key] = def_value
        self.mass.config['player_settings'][self.player_id] = player_settings
        self.mass.config['player_settings'][self.player_id]['__desc__'] = config_entries
        return player_settings
    