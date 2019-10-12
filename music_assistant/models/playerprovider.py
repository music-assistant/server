#!/usr/bin/env python3
# -*- coding:utf-8 -*-

import asyncio
from enum import Enum
from typing import List
from ..utils import run_periodic, LOGGER, parse_track_title
from ..constants import CONF_ENABLED
from ..cache import use_cache
from .player_queue import PlayerQueue
from .media_types import Track
from .player import Player


class PlayerProvider():
    ''' 
        Model for a Playerprovider
        Common methods usable for every provider
        Provider specific methods should be overriden in the provider specific implementation
    '''
    

    def __init__(self, mass):
        self.mass = mass
        self.name = 'My great Musicplayer provider' # display name
        self.prov_id = 'my_provider' # used as id

    ### Common methods and properties ####

    async def get_player_config_entries(self):
        ''' [CAN OVERRIDE] get the player-specific config entries for this provider (list with key/value pairs)'''
        return []

    @property
    def players(self):
        ''' return all players for this provider '''
        return self.mass.bg_executor.submit(asyncio.run, 
                self.mass.player.get_provider_players(self.prov_id)).result()
    
    async def get_player(self, player_id:str):
        ''' return player by id '''
        return await self.mass.player.get_player(player_id)

    async def add_player(self, player:Player):
        ''' register a new player '''
        return await self.mass.player.add_player(player)

    async def remove_player(self, player_id:str):
        ''' remove a player '''
        return await self.mass.player.remove_player(player_id)

    ### Provider specific implementation #####





