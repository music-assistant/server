#!/usr/bin/env python3
# -*- coding:utf-8 -*-

import os
from typing import List

from .constants import CONF_KEY_PLAYERPROVIDERS, EVENT_PLAYER_ADDED, \
    EVENT_PLAYER_REMOVED, EVENT_HASS_ENTITY_CHANGED
from .utils import LOGGER, load_provider_modules, iter_items
from .models.media_types import MediaItem, MediaType
from .models.player_queue import QueueItem, QueueOption
from .models.player import Player

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MODULES_PATH = os.path.join(BASE_DIR, "playerproviders")


class PlayerManager():
    """ several helpers to handle playback through player providers """
    def __init__(self, mass):
        self.mass = mass
        self._players = {}
        self.providers = {}

    async def setup(self):
        """ async initialize of module """
        # load providers
        await self.load_modules()
        # register state listener
        await self.mass.add_event_listener(self.handle_mass_events,
                                           EVENT_HASS_ENTITY_CHANGED)

    async def load_modules(self, reload_module=None):
        """Dynamically (un)load musicprovider modules."""
        if reload_module and reload_module in self.providers:
            # unload existing module
            if hasattr(self.providers[reload_module], 'http_session'):
                await self.providers[reload_module].http_session.close()
            self.providers.pop(reload_module, None)
            LOGGER.info('Unloaded %s module', reload_module)
        # load all modules (that are not already loaded)
        await load_provider_modules(self.mass, self.providers,
                                    CONF_KEY_PLAYERPROVIDERS)

    @property
    def players(self):
        """ return list of all players """
        return self._players.values()

    async def get_player(self, player_id: str):
        """ return player by id """
        return self._players.get(player_id, None)

    def get_player_sync(self, player_id: str):
        """ return player by id (non async) """
        return self._players.get(player_id, None)

    async def add_player(self, player: Player):
        """ register a new player """
        player.initialized = True
        self._players[player.player_id] = player
        await self.mass.signal_event(EVENT_PLAYER_ADDED, player.to_dict())
        # TODO: turn on player if it was previously turned on ?
        LOGGER.info("New player added: %s/%s", player.player_provider,
                    player.player_id)
        return player

    async def remove_player(self, player_id: str):
        """ handle a player remove """
        self._players.pop(player_id, None)
        await self.mass.signal_event(EVENT_PLAYER_REMOVED,
                                     {"player_id": player_id})
        LOGGER.info("Player removed: %s", player_id)

    async def trigger_update(self, player_id: str):
        """ manually trigger update for a player """
        if player_id in self._players:
            await self._players[player_id].update(force=True)

    async def play_media(self,
                         player_id: str,
                         media_items: List[MediaItem],
                         queue_opt=QueueOption.Play):
        """
            play media item(s) on the given player
            :param media_item: media item(s) that should be played (single item or list of items)
            :param queue_opt:
                Play -> insert new items in queue and start playing at the inserted position
                Replace -> replace queue contents with these items
                Next -> play item(s) after current playing item
                Add -> append new items at end of the queue
        """
        player = await self.get_player(player_id)
        if not player:
            return
        # a single item or list of items may be provided
        queue_items = []
        for media_item in media_items:
            # collect tracks to play
            if media_item.media_type == MediaType.Artist:
                tracks = self.mass.music.artist_toptracks(
                    media_item.item_id, provider=media_item.provider)
            elif media_item.media_type == MediaType.Album:
                tracks = self.mass.music.album_tracks(
                    media_item.item_id, provider=media_item.provider)
            elif media_item.media_type == MediaType.Playlist:
                tracks = self.mass.music.playlist_tracks(
                    media_item.item_id, provider=media_item.provider)
            else:
                tracks = iter_items(media_item)  # single track
            async for track in tracks:
                queue_item = QueueItem(track)
                # generate uri for this queue item
                queue_item.uri = 'http://%s:%s/stream/%s/%s' % (
                    self.mass.web.local_ip, self.mass.web.http_port, player_id,
                    queue_item.queue_item_id)
                queue_items.append(queue_item)

        # load items into the queue
        if (queue_opt == QueueOption.Replace
                or (len(queue_items) > 10
                    and queue_opt in [QueueOption.Play, QueueOption.Next])):
            return await player.queue.load(queue_items)
        elif queue_opt == QueueOption.Next:
            return await player.queue.insert(queue_items, 1)
        elif queue_opt == QueueOption.Play:
            return await player.queue.insert(queue_items, 0)
        elif queue_opt == QueueOption.Add:
            return await player.queue.append(queue_items)

    async def handle_mass_events(self, msg, msg_details=None):
        """ listen to some events on event bus """
        if msg == EVENT_HASS_ENTITY_CHANGED:
            # handle players with hass integration enabled
            player_ids = list(self._players.keys())
            for player_id in player_ids:
                player = self._players[player_id]
                if (msg_details['entity_id'] == player.settings.get(
                        'hass_power_entity') or msg_details['entity_id'] ==
                        player.settings.get('hass_volume_entity')):
                    await player.update()

    async def get_gain_correct(self, player_id, item_id, provider_id):
        """ get gain correction for given player / track combination """
        player = self._players[player_id]
        if not player.settings['volume_normalisation']:
            return 0
        target_gain = int(player.settings['target_volume'])
        fallback_gain = int(player.settings['fallback_gain_correct'])
        track_loudness = await self.mass.db.get_track_loudness(
            item_id, provider_id)
        if track_loudness is None:
            gain_correct = fallback_gain
        else:
            gain_correct = target_gain - track_loudness
        gain_correct = round(gain_correct, 2)
        LOGGER.debug(
            "Loudness level for track %s/%s is %s - calculated replayGain is %s",
            provider_id, item_id, track_loudness, gain_correct)
        return gain_correct
