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
        # dynamically load provider modules
        self.load_providers()

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
        player = self._players[player_id]
        player_settings = await self.get_player_config(player)
        # handle some common workarounds
        if cmd in ['pause', 'play'] and cmd_args == 'toggle':
            cmd = 'pause' if player.state == PlayerState.Playing else 'play'
        if cmd == 'power' and cmd_args == 'toggle':
            cmd_args = 'off' if player.powered else 'on'
        if cmd == 'volume' and cmd_args == 'up':
            cmd_args = player.volume_level + 2
        elif cmd == 'volume' and cmd_args == 'down':
            cmd_args = player.volume_level - 2
        if player.group_parent and cmd not in ['power', 'volume', 'mute']:
            # redirect playlist related commands to parent player
            return await self.player_command(player.group_parent, cmd, cmd_args)
        # handle hass integration
        if self.mass.hass:
            if cmd == 'power' and cmd_args == 'on' and player_settings.get('hass_power_entity') and player_settings.get('hass_power_entity_source'):
                service_data = { 'entity_id': player_settings['hass_power_entity'], 'source':player_settings['hass_power_entity_source'] }
                await self.mass.hass.call_service('media_player', 'select_source', service_data)
            elif cmd == 'power' and player_settings.get('hass_power_entity'):
                domain = player_settings['hass_power_entity'].split('.')[0]
                service_data = { 'entity_id': player_settings['hass_power_entity']}
                await self.mass.hass.call_service(domain, 'turn_%s' % cmd_args, service_data)
            if cmd == 'volume' and player_settings.get('hass_volume_entity'):
                service_data = { 'entity_id': player_settings['hass_power_entity'], 'volume_level': int(cmd_args)/100}
                await self.mass.hass.call_service('media_player', 'volume_set', service_data)
                cmd_args = 100 # just force full volume on actual player if volume is outsourced to hass
        if cmd == 'power' and player.mute_as_power:
            cmd = 'mute'
            cmd_args = 'on' if cmd_args == 'off' else 'off' # invert logic (power ON is mute OFF)
        player_childs = [item for item in self._players.values() if item.group_parent == player_id]
        is_group = len(player_childs) > 0
        if is_group and cmd == 'volume' and player.apply_group_volume:
            # group volume, apply to childs (if any)
            cur_volume = player.volume_level
            new_volume = try_parse_int(cmd_args)
            if new_volume < cur_volume:
                volume_dif = new_volume - cur_volume
            else:
                volume_dif = cur_volume - new_volume
            for child_player in player_childs:
                if child_player.enabled and child_player.powered:
                    cur_child_volume = child_player.volume_level
                    new_child_volume = cur_child_volume + volume_dif
                    LOGGER.debug('apply group volume %s to child %s' %(new_child_volume, child_player.name))
                    await self.player_command(child_player.player_id, 'volume', new_child_volume)
            player.volume_level = new_volume
            return True
        else:
            prov_id = self._players[player_id].player_provider
            prov = self.providers[prov_id]
            return await prov.player_command(player_id, cmd, cmd_args)
            
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
        player_settings = await self.get_player_config(player_details)
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
        
        # handle basic player settings
        player_details.enabled = player_settings['enabled']
        player_details.name = player_settings['name']
        player_details.disable_volume = player_settings['disable_volume']
        player_details.mute_as_power = player_settings['mute_as_power']
        player_details.apply_group_volume = player_settings['apply_group_volume']

        # handle hass integration
        if self.mass.hass:
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
        
        # handle mute as power setting
        if player_details.mute_as_power:
            player_details.powered = not player_details.muted
        # combine state of group parent
        if player_settings['group_parent']:
            player_details.group_parent = player_settings['group_parent']
        if player_details.group_parent and player_details.group_parent in self._players:
            parent_player = self._players[player_details.group_parent]
            player_details.cur_item_time = parent_player.cur_item_time
            player_details.cur_item = parent_player.cur_item
            player_details.state = parent_player.state
        # handle group volume setting
        player_childs = [item for item in self._players.values() if item.group_parent == player_id]
        player_details.is_group = len(player_childs) > 0
        if player_details.is_group and player_details.apply_group_volume:
            group_volume = 0
            active_players = 0
            for child_player in player_childs:
                if child_player.enabled and child_player.powered:
                    group_volume += child_player.volume_level
                    active_players += 1
            group_volume = group_volume / active_players if active_players else 0
            player_details.volume_level = group_volume
        # compare values to detect changes
        for key, cur_value in player.__dict__.items():
            new_value = getattr(player_details, key)
            if new_value != cur_value:
                player_changed = True
                setattr(player, key, new_value)
                LOGGER.debug('key changed: %s for player %s - new value: %s' % (key, player.name, new_value))
        if player_changed:
            # player is added or updated!
            asyncio.ensure_future(self.mass.event('player updated', player))
            for child in player_childs:
                asyncio.create_task(self.trigger_update(child.player_id))

    async def get_player_config(self, player_details):
        ''' get or create player config '''
        player_id = player_details.player_id
        if player_id in self.mass.config['player_settings']:
            return self.mass.config['player_settings'][player_id]
        new_config = {
                "name": player_details.name,
                "group_parent": player_details.group_parent,
                "mute_as_power": False,
                "disable_volume": False,
                "apply_group_volume": False,
                "enabled": False
            }
        self.mass.config['player_settings'][player_id] = new_config
        return new_config

    async def play_media(self, player_id, media_item, queue_opt='replace'):
        ''' 
            play media on a player 
            player_id: id of the player
            media_item: media item that should be played (Track, Album, Artist, Playlist)
            queue_opt: play, replace, next or add
        '''
        if not player_id in self._players:
            LOGGER.warning('Player %s not found' % player_id)
            return False
        player_prov = self.providers[self._players[player_id].player_provider]
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
        playable_tracks = []
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
            raise Exception("Musicprovider %s and/or mediatype %s not supported by player %s !" % ("/".join(media_item.provider_ids), media_item.media_type, player_id) )
    
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
