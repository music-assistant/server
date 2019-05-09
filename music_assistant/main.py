#!/usr/bin/env python3
# -*- coding:utf-8 -*-

import sys
import asyncio
from concurrent.futures import ThreadPoolExecutor
import re
import uvloop
import logging
import os
import shutil
import slugify as unicode_slug
import uuid
import json
import pkgutil
import time

from database import Database
from metadata import MetaData
from api import Api
from utils import run_periodic, LOGGER
from cache import Cache
from music import Music
from player import Player

class Main():

    def __init__(self, datapath):
        uvloop.install()
        self._datapath = datapath
        self.parse_config()
        self.event_loop = asyncio.get_event_loop()
        self.bg_executor = ThreadPoolExecutor(max_workers=5)
        self.event_listeners = {}

        import signal
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)

        # init database and metadata modules
        self.db = Database(self.event_loop)
        # allow some time for the database to initialize
        while not self.db.db_ready:
            time.sleep(0.5) 
        self.cache = Cache()
        self.metadata = MetaData(self.event_loop, self.db, self.cache)
        self.music = Music(self)
        self.player = Player(self)

        # init web/api
        self.api = Api(self)
        asyncio.ensure_future(self.api.setup_web())
        
        # start the event loop
        self.event_loop.run_forever()

    async def event(self, msg, msg_details=None):
        ''' signal event '''
        LOGGER.debug("Event: %s - %s" %(msg, msg_details))
        for listener in self.event_listeners.values():
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
            "musicproviders": {},
            "playerproviders": {},
            "player_settings": 
                {
                    "__desc__":
                    [
                        ("name", "", "Custom name for this player"),
                        ("group_parent", "<player>", "Group this player with another player"),
                        ("mute_as_power", False, "Use muting as power control"),
                        ("disable_volume", False, "Disable volume controls"),
                        ("apply_group_volume", False, "Apply group volume to childs (for group players only)"),
                        ("enabled", False, "Enable player")
                    ]
                }
            }
        conf_file = os.path.join(self._datapath, 'config.json')
        if os.path.isfile(conf_file):
            with open(conf_file) as f:
                data = f.read()
                stored_config = json.loads(data)
                for key in config.keys():
                    if stored_config.get(key):
                        config[key].update(stored_config[key])
        self.config = config

    def stop(self, signum=None, frame=None):
        ''' properly close all connections'''
        print('stop requested!')
        self.save_config()
        self.api.stop()
        print('stopping event loop...')
        self.event_loop.stop()
        self.event_loop.close()

if __name__ == "__main__":
    datapath = sys.argv[1:]
    if not datapath:
        datapath = os.path.dirname(os.path.abspath(__file__))
    Main(datapath)
    