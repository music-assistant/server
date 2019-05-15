#!/usr/bin/env python3
# -*- coding:utf-8 -*-

import sys
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
import re
import uvloop
import os
import shutil
import slugify as unicode_slug
import uuid
import json
import time

from database import Database
from metadata import MetaData
from utils import run_periodic, LOGGER
from cache import Cache
from music import Music
from player import Player
from modules.homeassistant import setup as hass_setup
from modules.web import setup as web_setup

class Main():

    def __init__(self, datapath):
        uvloop.install()
        self._datapath = datapath
        self.parse_config()
        self.event_loop = asyncio.get_event_loop()
        self.bg_executor = ThreadPoolExecutor(max_workers=5)
        self.event_listeners = {}

        # init database and metadata modules
        self.db = Database(datapath, self.event_loop)
        # allow some time for the database to initialize
        while not self.db.db_ready:
            time.sleep(0.15)
        self.cache = Cache(datapath)
        self.metadata = MetaData(self.event_loop, self.db, self.cache)

        # init modules
        self.web = web_setup(self)
        self.hass = hass_setup(self)
        self.music = Music(self)
        self.player = Player(self)

        # start the event loop
        try:
            self.event_loop.run_forever()
        except (KeyboardInterrupt, SystemExit):
            LOGGER.info('Exit requested!')
            self.save_config()
            self.event_loop.close()
            LOGGER.info('Shutdown complete.')


    async def event(self, msg, msg_details=None):
        ''' signal event '''
        LOGGER.debug("Event: %s - %s" %(msg, msg_details))
        listeners = list(self.event_listeners.values())
        for listener in listeners:
            await listener(msg, msg_details)

    async def add_event_listener(self, cb):
        ''' add callback to our event listeners '''
        cb_id = str(uuid.uuid4())
        self.event_listeners[cb_id] = cb

    async def remove_event_listener(self, cb_id):
        ''' add callback to our event listeners '''
        self.event_listeners.pop(cb_id, None)

    def save_config(self):
        ''' save config to file '''
        # backup existing file
        conf_file = os.path.join(self._datapath, 'config.json')
        conf_file_backup = os.path.join(self._datapath, 'config.json')
        if os.path.isfile(conf_file):
            shutil.move(conf_file, conf_file_backup)
        with open(conf_file, 'w') as f:
            f.write(json.dumps(self.config, indent=4))
        
    def parse_config(self):
        '''get config from config file'''
        config = {
            "base": {},
            "musicproviders": {},
            "playerproviders": {},
            "player_settings": {}
            }
        conf_file = os.path.join(self._datapath, 'config.json')
        if os.path.isfile(conf_file):
            with open(conf_file) as f:
                data = f.read()
                if data:
                    config = json.loads(data)
        self.config = config

if __name__ == "__main__":
    datapath = sys.argv[1]
    if not datapath:
        datapath = os.path.dirname(os.path.abspath(__file__))
    Main(datapath)
    