#!/usr/bin/env python3
# -*- coding:utf-8 -*-

import asyncio
import os
import struct
from collections import OrderedDict
import time
import decimal
from typing import List
import random
import sys
import socket
from ..utils import run_periodic, LOGGER, parse_track_title, try_parse_int, get_ip, get_hostname
from ..models import PlayerProvider, Player, PlayerState, MediaType, TrackQuality, AlbumType, Artist, Album, Track, Playlist
from ..constants import CONF_ENABLED


PROV_ID = 'web'
PROV_NAME = 'WebPlayer'
PROV_CLASS = 'WebPlayerProvider'

CONFIG_ENTRIES = [
    (CONF_ENABLED, True, CONF_ENABLED),
    ]

PLAYER_CONFIG_ENTRIES = []


class WebPlayerProvider(PlayerProvider):
    ''' Python implementation of SlimProto server '''

    def __init__(self, mass, conf):
        super().__init__(mass, conf)
        self.prov_id = PROV_ID
        self.name = PROV_NAME
        self.player_config_entries = PLAYER_CONFIG_ENTRIES

     ### Provider specific implementation #####

    async def setup(self):
        ''' async initialize of module '''
        pass

    