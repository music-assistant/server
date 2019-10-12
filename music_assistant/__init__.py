#!/usr/bin/env python3
# -*- coding:utf-8 -*-

# import os, sys; sys.path.append(os.path.dirname(os.path.realpath(__file__)))

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
# import stackimpact

# __package__ = 'music_assistant'

from .database import Database
from .utils import run_periodic, LOGGER
from .metadata import MetaData
from .cache import Cache
from .music_manager import Music
from .player_manager import PlayerManager
from .http_streamer import HTTPStreamer
from .homeassistant import setup as hass_setup
from .web import setup as web_setup

def handle_exception(loop, context):
    # context["message"] will always be there; but context["exception"] may not
    msg = context.get("exception", context["message"])
    LOGGER.error(f"Caught exception: {msg}")

class MusicAssistant():

    def __init__(self, datapath):
        uvloop.install()
        self.datapath = datapath
        self.parse_config()
        self.event_loop = asyncio.get_event_loop()
        self.bg_executor = ThreadPoolExecutor()
        self.event_loop.set_default_executor(self.bg_executor)
        self.event_loop.set_exception_handler(handle_exception)
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
        self.player = PlayerManager(self)
        self.http_streamer = HTTPStreamer(self)

        # agent = stackimpact.start(
        #     agent_key = '4a00b6f2c7da20f692807d204ab3760318978ba3',
        #     app_name = 'MusicAssistant')
        # print("profiler started...")

        # start the event loop
        try:
            self.event_loop.run_forever()
        except (KeyboardInterrupt, SystemExit):
            LOGGER.info('Exit requested!')
            self.signal_event("system_shutdown")
            self.event_loop.stop()
            self.save_config()
            time.sleep(5)
            self.event_loop.close()
            LOGGER.info('Shutdown complete.')

    def signal_event(self, msg, msg_details=None):
        ''' signal (systemwide) event '''
        LOGGER.debug("Event: %s - %s" %(msg, msg_details))
        listeners = list(self.event_listeners.values())
        for callback, eventfilter in listeners:
            if not eventfilter or eventfilter in msg:
                if not asyncio.iscoroutinefunction(callback):
                    callback(msg, msg_details)
                else:
                    self.event_loop.create_task(callback(msg, msg_details))

    def add_event_listener(self, cb, eventfilter=None):
        ''' add callback to our event listeners '''
        cb_id = str(uuid.uuid4())
        self.event_listeners[cb_id] = (cb, eventfilter)
        return cb_id

    def remove_event_listener(self, cb_id):
        ''' remove callback from our event listeners '''
        self.event_listeners.pop(cb_id, None)

    def save_config(self):
        ''' save config to file '''
        # backup existing file
        conf_file = os.path.join(self.datapath, 'config.json')
        conf_file_backup = os.path.join(self.datapath, 'config.json.backup')
        if os.path.isfile(conf_file):
            shutil.move(conf_file, conf_file_backup)
        # remove description keys from config
        final_conf = {}
        for key, value in self.config.items():
            final_conf[key] = {}
            for subkey, subvalue in value.items():
                if subkey != "__desc__":
                    final_conf[key][subkey] = subvalue
        with open(conf_file, 'w') as f:
            f.write(json.dumps(final_conf, indent=4))
        
    def parse_config(self):
        '''get config from config file'''
        config = {
            "base": {},
            "musicproviders": {},
            "playerproviders": {},
            "player_settings": {}
            }
        conf_file = os.path.join(self.datapath, 'config.json')
        if os.path.isfile(conf_file):
            with open(conf_file) as f:
                data = f.read()
                if data:
                    data = json.loads(data)
                    for key, value in data.items():
                        config[key] = value
        self.config = config

    