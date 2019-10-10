#!/usr/bin/env python3
# -*- coding:utf-8 -*-

import asyncio
import os
from enum import Enum
from ..utils import run_periodic, LOGGER, try_parse_int, try_parse_float, get_ip, run_async_background_task
from ..models.media_types import MediaType, TrackQuality
from ..models.player_queue import QueueItem
from ..models.player import PlayerState
import operator
import random
import functools
import urllib

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MODULES_PATH = os.path.join(BASE_DIR, "playerproviders" )



class PlayerManager():
    ''' several helpers to handle playback through player providers '''
    
    def __init__(self, mass):
        self.mass = mass
        self.providers = {}
        self._players = {}
        self.local_ip = get_ip()
        # dynamically load provider modules
        self.load_providers()

    @property
    def players(self):
        ''' all players as property '''
        return self.mass.event_loop.run_until_complete(self.get_players())
    
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
