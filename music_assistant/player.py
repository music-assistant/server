#!/usr/bin/env python3
# -*- coding:utf-8 -*-

import asyncio
import os
from utils import run_periodic, LOGGER, try_parse_int
import aiohttp
from difflib import SequenceMatcher as Matcher
from models import MediaType, PlayerState, MusicPlayer
from typing import List
import toolz
import operator


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
        if not player_id in self._players:
            LOGGER.warning('Player %s not found' % player_id)
            return False
        player = self._players[player_id]
        if player.group_parent and cmd not in ['power', 'volume', 'mute']:
            # redirect playlist related commands to parent player
            return await self.player_command(player.group_parent, cmd, cmd_args)
        if cmd == 'power' and player.mute_as_power:
            cmd = 'mute'
            cmd_args = 'on' if cmd_args == 'off' else 'off' # invert logic (power ON is mute OFF)
        if cmd == 'volume' and player.apply_group_volume:
            # group volume, apply to childs (if any)
            cur_volume = player.volume_level
            if cmd_args == 'up':
                new_volume = cur_volume + 2
            elif cmd_args == 'down':
                new_volume = cur_volume - 2
            else:
                new_volume = try_parse_int(cmd_args)
            if new_volume < cur_volume:
                volume_dif = new_volume - cur_volume
            else:
                volume_dif = cur_volume - new_volume
            for child_player in await self.players():
                if child_player.group_parent == player_id:
                    LOGGER.debug("%s - %s - %s" % (child_player.name, child_player.state, child_player.muted))
                if child_player.group_parent == player_id and child_player.state != PlayerState.Off:
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

    async def update_player(self, player_details):
        ''' update (or add) player '''
        LOGGER.debug('Incoming msg from %s' % player_details.name)
        player_id = player_details.player_id
        player_settings = await self.get_player_config(player_details)
        player_changed = False
        if not player_id in self._players:
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
        
        # handle mute as power setting
        if player_details.mute_as_power:
            player_details.powered = not player_details.muted
        # combine state of group parent
        if player_settings['group_parent']:
            player_details.group_parent = player_settings['group_parent']
        if player_details.group_parent and player_details.group_parent in self._players:
            parent_player = self._players[player_details.group_parent]
            if player_details.powered and player_details.state != PlayerState.Playing:
                player_details.cur_item_time = parent_player.cur_item_time
                player_details.cur_item = parent_player.cur_item
        # handle group volume setting
        if player_details.is_group and player_details.apply_group_volume:
            group_volume = 0
            active_players = 0
            for child_player in self._players.values():
                if child_player.group_parent == player_id and child_player.enabled and child_player.powered:
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
            queue_opt: replace, next or add
        '''
        if not player_id in self._players:
            LOGGER.warning('Player %s not found' % player_id)
            return False
        prov_id = self._players[player_id].player_provider
        prov = self.providers[prov_id]
        # check supported music providers by this player and work out how to handle playback...
        musicprovider = None
        item_id = None
        for prov_id, supported_types in prov.supported_musicproviders:
            if media_item.provider_ids.get(prov_id):
                musicprovider = prov_id
                prov_item_id = media_item.provider_ids[prov_id]
                if media_item.media_type in supported_types:
                    # the provider can handle this media_type directly !
                    uri = await self.get_item_uri(media_item.media_type, prov_item_id, prov_id)
                    return await prov.play_media(player_id, uri, queue_opt)
                else:
                    # manually enqueue the tracks of this listing
                    return await self.queue_items(player_id, media_item, queue_opt)
            elif prov_id == 'http':
                # fallback to http streaming
                if media_item.media_type == MediaType.Track:
                    for media_prov_id, media_prov_item_id in media_item.provider_ids.items():
                        stream_details = await self.mass.music.providers[media_prov_id].get_stream_details(media_prov_item_id)
                        return await prov.play_media(player_id, stream_details['url'], queue_opt)
                else:
                    return await self.queue_items(player_id, media_item, queue_opt)
        raise Exception("Musicprovider %s and/or mediatype %s not supported by player %s !" % ("/".join(media_item.provider_ids.keys()), media_item.media_type, player_id) )

    async def queue_items(self, player_id, media_item, queue_opt):
        ''' extract a list of items and manually enqueue the tracks '''
        tracks = []
        #TODO: respect shuffle
        if media_item.media_type == MediaType.Artist:
            tracks = await self.mass.music.artist_toptracks(media_item.item_id)
        elif media_item.media_type == MediaType.Album:
            tracks = await self.mass.music.album_tracks(media_item.item_id)
        elif media_item.media_type == MediaType.Playlist:
            tracks = await self.mass.music.playlist_tracks(media_item.item_id, offset=0, limit=0)
        if queue_opt == 'replace':
            await self.play_media(player_id, tracks[0], 'replace')
            tracks = tracks[1:]
            queue_opt = 'add'
        for track in tracks:
            await self.play_media(player_id, track, queue_opt)

    async def player_queue(self, player_id, offset=0, limit=50):
        ''' return the items in the player's queue '''
        player = self._players[player_id]
        player_prov = self.providers[player.player_provider]
        if player_prov.supports_queue:
            return await player_prov.player_queue(player_id, offset=offset, limit=limit)
        else:
            # TODO: Implement 'fake' queue
            raise NotImplementedError
    
    async def get_item_uri(self, media_type, item_id, provider):
        ''' generate the URL/URI for a media item '''
        uri = ""
        if provider == "spotify" and media_type == MediaType.Track:
            uri = 'spotify://spotify:track:%s' % item_id
        elif provider == "spotify" and media_type == MediaType.Album:
            uri = 'spotify://spotify:album:%s' % item_id
        elif provider == "spotify" and media_type == MediaType.Artist:
            uri = 'spotify://spotify:artist:%s' % item_id
        elif provider == "spotify" and media_type == MediaType.Playlist:
            uri = 'spotify://spotify:playlist:%s' % item_id
        elif provider == "qobuz" and media_type == MediaType.Track:
            uri = 'qobuz://%s.flac' % item_id
        elif provider == "file":
            uri = 'file://%s' % item_id
        return uri

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
