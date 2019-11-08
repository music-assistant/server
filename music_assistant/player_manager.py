#!/usr/bin/env python3
# -*- coding:utf-8 -*-

import asyncio
import os
from enum import Enum
import operator
import random
import functools
import urllib

from .constants import CONF_KEY_PLAYERPROVIDERS, EVENT_PLAYER_ADDED, EVENT_PLAYER_REMOVED, EVENT_HASS_ENTITY_CHANGED
from .utils import run_periodic, LOGGER, try_parse_int, try_parse_float, \
    get_ip, run_async_background_task, load_provider_modules
from .models.media_types import MediaType, TrackQuality
from .models.player_queue import QueueItem
from .models.playerstate import PlayerState

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MODULES_PATH = os.path.join(BASE_DIR, "playerproviders" )


class PlayerManager():
    ''' several helpers to handle playback through player providers '''
    
    def __init__(self, mass):
        self.mass = mass
        self._players = {}
        # dynamically load musicprovider modules
        self.providers = load_provider_modules(mass, CONF_KEY_PLAYERPROVIDERS)
        
    async def setup(self):
        ''' async initialize of module '''
        # start providers
        for prov in self.providers.values():
            await prov.setup()
        # register state listener
        await self.mass.add_event_listener(self.handle_mass_events, EVENT_HASS_ENTITY_CHANGED)
    
    @property
    def players(self):
        ''' return list of all players '''
        return self._players.values()

    async def get_player(self, player_id):
        ''' return player by id '''
        return self._players.get(player_id, None)

    def get_player_sync(self, player_id):
        ''' return player by id (non async) '''
        return self._players.get(player_id, None)

    async def add_player(self, player):
        ''' register a new player '''
        player._initialized = True
        self._players[player.player_id] = player
        await self.mass.signal_event(EVENT_PLAYER_ADDED, player.to_dict())
        # TODO: turn on player if it was previously turned on ?
        LOGGER.info(f"New player added: {player.player_provider}/{player.player_id}")
        return player

    async def remove_player(self, player_id):
        ''' handle a player remove '''
        self._players.pop(player_id, None)
        await self.mass.signal_event(EVENT_PLAYER_REMOVED, {"player_id": player_id})
        LOGGER.info(f"Player removed: {player_id}")

    async def trigger_update(self, player_id):
        ''' manually trigger update for a player '''
        if player_id in self._players:
            await self._players[player_id].update(force=True)
    
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
                        provider=media_item.provider) 
            else:
                tracks = [media_item] # single track
            for track in tracks:
                queue_item = QueueItem(track)
                # generate uri for this queue item
                queue_item.uri = 'http://%s:%s/stream/%s/%s'% (
                        self.mass.web.local_ip, self.mass.web.http_port, player_id, queue_item.queue_item_id)
                queue_items.append(queue_item)
                    
        # load items into the queue
        if queue_opt == 'replace' or (queue_opt in ['next', 'play'] and len(queue_items) > 10):
            return await player.queue.load(queue_items)
        elif queue_opt == 'next':
            return await player.queue.insert(queue_items, 1)
        elif queue_opt == 'play':
            return await player.queue.insert(queue_items, 0)
        elif queue_opt == 'add':
            return await player.queue.append(queue_items)
    
    async def handle_mass_events(self, msg, msg_details=None):
        ''' listen to some events on event bus '''
        if msg == EVENT_HASS_ENTITY_CHANGED:
            # handle players with hass integration enabled
            player_ids = list(self._players.keys())
            for player_id in player_ids:
                player = self._players[player_id]
                if (msg_details['entity_id'] == player.settings.get('hass_power_entity') or 
                        msg_details['entity_id'] == player.settings.get('hass_volume_entity')):
                    await player.update()
    
    async def get_gain_correct(self, player_id, item_id, provider_id, replaygain=False):
        ''' get gain correction for given player / track combination '''
        player = self._players[player_id]
        if not player.settings['volume_normalisation']:
            return 0
        target_gain = int(player.settings['target_volume'])
        fallback_gain = int(player.settings['fallback_gain_correct'])
        track_loudness = await self.mass.db.get_track_loudness(item_id, provider_id)
        if track_loudness == None:
            gain_correct = fallback_gain
        else:
            gain_correct = target_gain - track_loudness
        gain_correct = round(gain_correct,2)
        LOGGER.debug(f"Loudness level for track {provider_id}/{item_id} is {track_loudness} - calculated replayGain is {gain_correct}")
        return gain_correct