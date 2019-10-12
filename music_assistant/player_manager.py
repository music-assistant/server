#!/usr/bin/env python3
# -*- coding:utf-8 -*-

import asyncio
import os
from enum import Enum
import operator
import random
import functools
import urllib
import importlib

from .utils import run_periodic, LOGGER, try_parse_int, try_parse_float, get_ip, run_async_background_task
from .models.media_types import MediaType, TrackQuality
from .models.player_queue import QueueItem
from .models.player import PlayerState

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MODULES_PATH = os.path.join(BASE_DIR, "playerproviders" )



class PlayerManager():
    ''' several helpers to handle playback through player providers '''
    
    def __init__(self, mass):
        self.mass = mass
        self.providers = {}
        self._players = {}
        # dynamically load provider modules
        self.load_providers()

    @property
    def players(self):
        ''' all players as property '''
        return self.mass.bg_executor.submit(asyncio.run, 
                self.get_players()).result()
    
    async def get_players(self):
        ''' return all players as a list '''
        items = list(self._players.values())
        items.sort(key=lambda x: x.name, reverse=False)
        return items

    async def get_player(self, player_id):
        ''' return player by id '''
        return self._players.get(player_id, None)

    async def get_provider_players(self, player_provider):
        ''' return all players for given provider_id '''
        return [item for item in self._players.values() if item.player_provider == player_provider] 

    async def add_player(self, player):
        ''' register a new player '''
        self._players[player.player_id] = player
        self.mass.signal_event('player added', player)
        # TODO: turn on player if it was previously turned on ?
        return player

    async def remove_player(self, player_id):
        ''' handle a player remove '''
        self._players.pop(player_id, None)
        self.mass.signal_event('player removed', player_id)

    async def trigger_update(self, player_id):
        ''' manually trigger update for a player '''
        if player_id in self._players:
            await self._players[player_id].update()
    
    async def play_media(self, player_id, media_item, queue_opt='play'):
        ''' 
            play media item(s) on the given player 
            :param media_item: media item(s) that should be played (Track, Album, Artist, Playlist, Radio)
                        single item or list of items
            :param queue_opt: 
                play -> insert new items in queue and start playing at the inserted position
                replace -> replace queue contents with these items
                next -> play item(s) after current playing item
                add -> append new items at end of the queue
        '''
        player = await self.get_player(player_id)
        if not player:
            return
        # a single item or list of items may be provided
        media_items = media_item if isinstance(media_item, list) else [media_item]
        queue_items = []
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
                queue_item = QueueItem(track)
                # generate uri for this queue item
                queue_item.uri = 'http://%s:%s/stream/%s?queue_item_id=%s'% (
                        self.mass.web.local_ip, self.mass.web.http_port, player_id, queue_item.queue_item_id)
                # sort by quality and check track availability
                for prov_media in sorted(track.provider_ids, key=operator.itemgetter('quality'), reverse=True):
                    queue_item.provider = prov_media['provider']
                    queue_item.item_id = prov_media['item_id']
                    queue_item.quality = prov_media['quality']
                    # TODO: check track availability
                    # TODO: handle direct stream capability
                    queue_items.append(queue_item)
                    break
        # load items into the queue
        if queue_opt == 'replace' or (queue_opt in ['next', 'play'] and len(queue_items) > 50):
            return await player.queue.load(queue_items)
        elif queue_opt == 'next':
            return await player.queue.insert(queue_items, 1)
        elif queue_opt == 'play':
            return await player.queue.insert(queue_items, 0)
        elif queue_opt == 'add':
            return await player.queue.append(queue_items)
    
    def load_providers(self):
        ''' dynamically load providers '''
        for item in os.listdir(MODULES_PATH):
            if (os.path.isfile(os.path.join(MODULES_PATH, item)) and not item.startswith("_") and 
                    item.endswith('.py') and not item.startswith('.')):
                module_name = item.replace(".py","")
                LOGGER.debug("Loading playerprovider module %s" % module_name)
                try:
                    mod = importlib.import_module("." + module_name, "music_assistant.playerproviders")
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
