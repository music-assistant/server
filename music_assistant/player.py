#!/usr/bin/env python3
# -*- coding:utf-8 -*-

import asyncio
import os
from utils import run_periodic, LOGGER, try_parse_int, try_parse_float
import aiohttp
from difflib import SequenceMatcher as Matcher
from models import MediaType, PlayerState, MusicPlayer
from typing import List
import toolz
import operator
import socket
import random
from copy import deepcopy
import functools

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MODULES_PATH = os.path.join(BASE_DIR, "modules", "playerproviders" )

class Player():
    ''' several helpers to handle playback through player providers '''
    
    def __init__(self, mass):
        self.mass = mass
        self.providers = {}
        self._players = {}
        self.create_config_entries()
        # dynamically load provider modules
        self.load_providers()

    def create_config_entries(self):
        ''' sets the config entries for this module (list with key/value pairs)'''
        self.mass.config['player_settings']['__desc__'] = [
            ("enabled", False, "Enable player"),
            ("name", "", "Custom name for this player"),
            ("group_parent", "<player>", "Group this player to another player"),
            ("mute_as_power", False, "Use muting as power control"),
            ("disable_volume", False, "Disable volume controls"),
            ("apply_group_volume", False, "Apply group volume to childs (for group players only)"),
            ("apply_group_power", False, "Apply group power based on childs (for group players only)"),
            ("play_power_on", False, "Issue play command on power on")
        ]
    
    async def players(self):
        ''' return all players '''
        items = list(self._players.values())
        items.sort(key=lambda x: x.name, reverse=False)
        return items

    async def player(self, player_id):
        ''' return players by id '''
        return self._players[player_id]

    async def player_command(self, player_id, cmd, cmd_args=None):
        ''' issue command on player (play, pause, next, previous, stop, power, volume, mute) '''
        if player_id not in self._players:
            return
        player = self._players[player_id]
        # handle some common workarounds
        if cmd in ['pause', 'play'] and cmd_args == 'toggle':
            cmd = 'pause' if player.state == PlayerState.Playing else 'play'
        if cmd == 'power' and cmd_args == 'toggle':
            cmd_args = 'off' if player.powered else 'on'
        if cmd == 'volume' and cmd_args == 'up':
            cmd_args = player.volume_level + 2
        elif cmd == 'volume' and cmd_args == 'down':
            cmd_args = player.volume_level - 2
        # redirect playlist related commands to parent player
        if player.group_parent and cmd not in ['power', 'volume', 'mute']:
            return await self.player_command(player.group_parent, cmd, cmd_args)
        # handle hass integration
        await self.__player_command_hass_integration(player, cmd, cmd_args)
        # handle mute as power
        if cmd == 'power' and player.settings['mute_as_power']:
            cmd = 'mute'
            cmd_args = 'on' if cmd_args == 'off' else 'off' # invert logic (power ON is mute OFF)
        # handle group volume for group players
        player_childs = [item for item in self._players.values() if item.group_parent == player_id]
        if player.is_group and cmd == 'volume' and player.settings['apply_group_volume']:
            return await self.__player_command_group_volume(player, player_childs, cmd_args)
        if player.is_group and cmd == 'power' and cmd_args == 'off':
            for item in player_childs:
                asyncio.create_task(self.player_command(item.player_id, cmd, cmd_args))
        # normal execution of command on player
        prov_id = self._players[player_id].player_provider
        prov = self.providers[prov_id]
        await prov.player_command(player_id, cmd, cmd_args)
        # handle play on power on
        if cmd == 'power' and cmd_args == 'on' and player.settings['play_power_on']:
            LOGGER.info('play_power_on %s' % player.name)
            await prov.player_command(player_id, 'play')

    async def __player_command_hass_integration(self, player, cmd, cmd_args):
        ''' handle hass integration in player command '''
        if not self.mass.hass:
            return
        if cmd == 'power' and cmd_args == 'on' and player.settings.get('hass_power_entity') and player.settings.get('hass_power_entity_source'):
            service_data = { 'entity_id': player.settings['hass_power_entity'], 'source':player.settings['hass_power_entity_source'] }
            await self.mass.hass.call_service('media_player', 'select_source', service_data)
        elif cmd == 'power' and player.settings.get('hass_power_entity'):
            domain = player.settings['hass_power_entity'].split('.')[0]
            service_data = { 'entity_id': player.settings['hass_power_entity']}
            await self.mass.hass.call_service(domain, 'turn_%s' % cmd_args, service_data)
        if cmd == 'volume' and player.settings.get('hass_volume_entity'):
            service_data = { 'entity_id': player.settings['hass_power_entity'], 'volume_level': int(cmd_args)/100}
            await self.mass.hass.call_service('media_player', 'volume_set', service_data)
            cmd_args = 100 # just force full volume on actual player if volume is outsourced to hass
            
    async def __player_command_group_volume(self, player, player_childs, cmd_args):
        ''' handle group volume if needed'''
        cur_volume = player.volume_level
        new_volume = try_parse_int(cmd_args)
        volume_dif = new_volume - cur_volume
        volume_dif_percent = volume_dif/cur_volume
        for child_player in player_childs:
            if child_player.enabled and child_player.powered:
                cur_child_volume = child_player.volume_level
                new_child_volume = cur_child_volume + (cur_child_volume * volume_dif_percent)
                child_player.volume_level = new_child_volume
                await self.player_command(child_player.player_id, 'volume', new_child_volume)
        player.volume_level = new_volume
        return True

    async def remove_player(self, player_id):
        ''' handle a player remove '''
        self._players.pop(player_id, None)
        asyncio.ensure_future(self.mass.event('player removed', player_id))

    async def trigger_update(self, player_id):
        ''' manually trigger update for a player '''
        await self.update_player(self._players[player_id])
    
    async def update_player(self, player_details):
        ''' update (or add) player '''
        player_details = deepcopy(player_details)
        LOGGER.debug('Incoming msg from %s' % player_details.name)
        player_id = player_details.player_id
        player_changed = False
        if not player_id in self._players:
            # first message from player
            self._players[player_id] = MusicPlayer()
            player = self._players[player_id]
            player.player_id = player_id
            player.player_provider  = player_details.player_provider
            player_changed = True
        else:
            player = self._players[player_id]
        player.settings = await self.__get_player_settings(player_id)
        # handle basic player settings
        player_details.enabled = player.settings['enabled']
        player_details.name = player.settings['name'] if player.settings['name'] else player_details.name
        player_details.group_parent = player.settings['group_parent'] if player.settings['group_parent'] else player_details.group_parent
        # handle hass integration
        await self.__update_player_hass_settings(player_details, player.settings)
        # handle mute as power setting
        if player.settings['mute_as_power']:
            player_details.powered = not player_details.muted
        # combine state of group parent
        if player_details.group_parent and player_details.group_parent in self._players:
            parent_player = self._players[player_details.group_parent]
            player_details.cur_item_time = parent_player.cur_item_time
            player_details.cur_item = parent_player.cur_item
            player_details.state = parent_player.state
        # handle group volume/power setting
        player_childs = [item for item in self._players.values() if item.group_parent == player_id]
        player_details.is_group = len(player_childs) > 0
        if player_details.is_group and player.settings['apply_group_volume']:
            await self.__update_player_group_volume(player_details, player_childs)
        if player_details.is_group and player.settings['apply_group_power']:
            await self.__update_player_group_power(player_details, player_childs)
        # compare values to detect changes
        for key, cur_value in player.__dict__.items():
            if key != 'settings':
                new_value = getattr(player_details, key)
                if new_value != cur_value:
                    player_changed = True
                    setattr(player, key, new_value)
                    LOGGER.debug('key changed: %s for player %s - new value: %s' % (key, player.name, new_value))
        if player_changed:
            # player is added or updated!
            asyncio.ensure_future(self.mass.event('player updated', player))
            # is groupplayer, trigger update of its childs
            for child in player_childs:
                asyncio.create_task(self.trigger_update(child.player_id))
            # if child player in a group, trigger update of parent
            if player.group_parent:
                asyncio.create_task(self.trigger_update(player.group_parent))

    async def __update_player_hass_settings(self, player_details, player_settings):
        ''' handle home assistant integration on a player '''
        if not self.mass.hass:
            return
        player_id = player_details.player_id
        player_settings = self.mass.config['player_settings'][player_id]
        if player_settings.get('hass_power_entity') and player_settings.get('hass_power_entity_source'):
            hass_state = await self.mass.hass.get_state(
                    player_settings['hass_power_entity'],
                    attribute='source',
                    register_listener=functools.partial(self.trigger_update, player_id))
            player_details.powered = hass_state == player_settings['hass_power_entity_source']
        elif player_settings.get('hass_power_entity'):
            hass_state = await self.mass.hass.get_state(
                    player_settings['hass_power_entity'],
                    attribute='state',
                    register_listener=functools.partial(self.trigger_update, player_id))
            player_details.powered = hass_state != 'off'
        if player_settings.get('hass_volume_entity'):
            hass_state = await self.mass.hass.get_state(
                    player_settings['hass_volume_entity'], 
                    attribute='volume_level',
                    register_listener=functools.partial(self.trigger_update, player_id))
            player_details.volume_level = int(try_parse_float(hass_state)*100)
    
    async def __update_player_group_volume(self, player_details, player_childs):
        ''' handle group volume '''
        group_volume = 0
        active_players = 0
        for child_player in player_childs:
            if child_player.enabled and child_player.powered:
                group_volume += child_player.volume_level
                active_players += 1
        group_volume = group_volume / active_players if active_players else 0
        player_details.volume_level = group_volume
    
    async def __update_player_group_power(self, player_details, player_childs):
        ''' handle group power '''
        player_powered = False
        for child_player in player_childs:
            if child_player.powered:
                player_powered = True
                break
        if player_details.powered and not player_powered:
            # all childs turned off so turn off group player
            LOGGER.info('all childs turned off so turn off group player %s' % player_details.name)
            await self. player_command(player_details.player_id, 'power', 'off')
            player_details.powered = False
        elif not player_details.powered and player_powered:
            # all childs turned off but group player still off, so turn it on
            LOGGER.info('all childs turned off but group player still off, so turn it on %s' % player_details.name)
            await self. player_command(player_details.player_id, 'power', 'on')
            player_details.powered = True

    async def __get_player_settings(self, player_id):
        ''' get (or create) player config '''
        player_settings = self.mass.config['player_settings'].get(player_id,{})
        for key, def_value, desc in self.mass.config['player_settings']['__desc__']:
            if not key in player_settings:
                player_settings[key] = def_value
        self.mass.config['player_settings'][player_id] = player_settings
        return player_settings

    async def play_media(self, player_id, media_item, queue_opt='play'):
        ''' 
            play media on a player 
            player_id: id of the player
            media_item: media item(s) that should be played (Track, Album, Artist, Playlist)
            queue_opt: play, replace, next or add
        '''
        if not player_id in self._players:
            LOGGER.warning('Player %s not found' % player_id)
            return False
        player_prov = self.providers[self._players[player_id].player_provider]
        # a single item or list of items may be provided
        media_items = media_item if isinstance(media_item, list) else [media_item]
        playable_tracks = []
        for media_item in media_items:
            # collect tracks to play
            if media_item.media_type == MediaType.Artist:
                tracks = await self.mass.music.artist_toptracks(media_item.item_id, provider=media_item.provider)
            elif media_item.media_type == MediaType.Album:
                tracks = await self.mass.music.album_tracks(media_item.item_id, provider=media_item.provider)
            elif media_item.media_type == MediaType.Playlist:
                tracks = await self.mass.music.playlist_tracks(media_item.item_id, provider=media_item.provider, offset=0, limit=0) 
            else:
                tracks = [media_item] # single track
            # check supported music providers by this player and work out how to handle playback...
            for track in tracks:
                # sort by quality
                match_found = False
                for prov_media in sorted(track.provider_ids, key=operator.itemgetter('quality'), reverse=True):
                    media_provider = prov_media['provider']
                    media_item_id = prov_media['item_id']
                    player_supported_provs = player_prov.supported_musicproviders
                    if media_provider in player_supported_provs:
                        # the provider can handle this media_type directly !
                        track.uri = await self.get_track_uri(media_item_id, media_provider)
                        playable_tracks.append(track)
                        match_found = True
                    elif 'http' in player_prov.supported_musicproviders:
                        # fallback to http streaming if supported
                        track.uri = await self.get_track_uri(media_item_id, media_provider, True)
                        playable_tracks.append(track)
                        match_found = True
                    if match_found:
                        break
        if playable_tracks:
            if self._players[player_id].shuffle_enabled:
                random.shuffle(playable_tracks)
            if queue_opt in ['next', 'play'] and len(playable_tracks) > 1:
                queue_opt = 'replace' # always assume playback of multiple items as new queue
            return await player_prov.play_media(player_id, playable_tracks, queue_opt)
        else:
            raise Exception("Musicprovider and/or media not supported by player %s !" % (player_id) )
    
    async def get_track_uri(self, item_id, provider, http_stream=False):
        ''' generate the URL/URI for a media item '''
        uri = ""
        if http_stream:
            host = socket.gethostbyname(socket.gethostname())
            uri = 'http://%s:8095/stream/%s/%s'% (host, provider, item_id)
        elif provider == "spotify":
            uri = 'spotify://spotify:track:%s' % item_id
        elif provider == "qobuz":
            uri = 'qobuz://%s.flac' % item_id
        elif provider == "file":
            uri = 'file://%s' % item_id
        return uri

    async def player_queue(self, player_id, offset=0, limit=50):
        ''' return the items in the player's queue '''
        player = self._players[player_id]
        player_prov = self.providers[player.player_provider]
        return await player_prov.player_queue(player_id, offset=offset, limit=limit)

    def load_providers(self):
        ''' dynamically load providers '''
        for item in os.listdir(MODULES_PATH):
            if (os.path.isfile(os.path.join(MODULES_PATH, item)) and not item.startswith("_") and 
                    item.endswith('.py') and not item.startswith('.')):
                module_name = item.replace(".py","")
                LOGGER.debug("Loading playerprovider module %s" % module_name)
                try:
                    mod = __import__("modules.playerproviders." + module_name, fromlist=[''])
                    if not self.mass.config['playerproviders'].get(module_name):
                        self.mass.config['playerproviders'][module_name] = {}
                    self.mass.config['playerproviders'][module_name]['__desc__'] = mod.config_entries()
                    for key, def_value, desc in mod.config_entries():
                        if not key in self.mass.config['playerproviders'][module_name]:
                            self.mass.config['playerproviders'][module_name][key] = def_value
                    mod = mod.setup(self.mass)
                    if mod:
                        self.providers[mod.prov_id] = mod
                        cls_name = mod.__class__.__name__
                        LOGGER.info("Successfully initialized module %s" % cls_name)
                except Exception as exc:
                    LOGGER.exception("Error loading module %s: %s" %(module_name, exc))
