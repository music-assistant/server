#!/usr/bin/env python3
# -*- coding:utf-8 -*-

import asyncio
import re
import uvloop
import os
import shutil
import slugify as unicode_slug
import uuid
import json
import time
import logging
import threading

from .database import Database
from .config import MassConfig
from .utils import run_periodic, LOGGER, try_parse_bool, serialize_values
from .metadata import MetaData
from .cache import Cache
from .music_manager import MusicManager
from .player_manager import PlayerManager
from .http_streamer import HTTPStreamer
from .homeassistant import HomeAssistant
from .web import Web


class MusicAssistant():

    def __init__(self, datapath, event_loop):
        ''' 
            Create an instance of MusicAssistant
            :param datapath: file location to store the data
            :param event_loop: asyncio event_loop
        '''
        self.event_loop = event_loop
        self.event_loop.set_exception_handler(self.handle_exception)
        self.datapath = datapath
        self.event_listeners = {}
        self.config = MassConfig(self)
        # init modules
        self.db = Database(self)
        self.cache = Cache(self)
        self.metadata = MetaData(self)
        self.web = Web(self)
        self.hass = HomeAssistant(self)
        self.music = MusicManager(self)
        self.players = PlayerManager(self)
        self.http_streamer = HTTPStreamer(self)

    async def start(self):
        ''' start running the music assistant server '''
        await self.db.setup()
        await self.cache.setup()
        await self.metadata.setup()
        await self.hass.setup()
        await self.music.setup()
        await self.players.setup()
        await self.web.setup()
        await self.http_streamer.setup()
        # wait for exit
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            LOGGER.info("Application shutdown")
            await self.signal_event("shutdown")

    def handle_exception(self, loop, context):
        ''' global exception handler '''
        LOGGER.debug(f"Caught exception: {context}")
        loop.default_exception_handler(context)

    async def signal_event(self, msg, msg_details=None):
        ''' signal (systemwide) event '''
        if not (msg_details == None or isinstance(msg_details, (str, dict))):
            msg_details = serialize_values(msg_details)
        listeners = list(self.event_listeners.values())
        for callback, eventfilter in listeners:
            if not eventfilter or eventfilter in msg:
                if msg == 'shutdown':
                    await callback(msg, msg_details)
                else:
                    self.event_loop.create_task(callback(msg, msg_details))

    async def add_event_listener(self, cb, eventfilter=None):
        ''' add callback to our event listeners '''
        cb_id = str(uuid.uuid4())
        self.event_listeners[cb_id] = (cb, eventfilter)
        return cb_id

    async def remove_event_listener(self, cb_id):
        ''' remove callback from our event listeners '''
        self.event_listeners.pop(cb_id, None)

    def run_task(self, corofcn, wait_for_result=False, ignore_exception=None):
        ''' helper to run a task on the main event loop from another thread '''
        if threading.current_thread() is threading.main_thread():
            raise Exception("Can not be called from main event loop!")
        future = asyncio.run_coroutine_threadsafe(corofcn, self.event_loop)
        if wait_for_result:
            try:
                return future.result()
            except Exception as exc:
                if ignore_exception and isinstance(exc, ignore_exception):
                    return None
                raise exc
        return future
