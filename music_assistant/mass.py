#!/usr/bin/env python3
# -*- coding:utf-8 -*-

import asyncio
import functools
import threading
import uuid
from typing import Any, Awaitable, Callable, Coroutine, Union, Optional, List

import zeroconf

from .cache import Cache
from .config import MassConfig
from .constants import EVENT_SHUTDOWN
from .database import Database
from .homeassistant import HomeAssistant
from .http_streamer import HTTPStreamer
from .metadata import MetaData
from .music_manager import MusicManager
from .player_manager import PlayerManager
from .utils import LOGGER, T, callback, is_callback, serialize_values
from .web import Web


class MusicAssistant:
    def __init__(self, datapath):
        """
            Create an instance of MusicAssistant
                :param datapath: file location to store the data
        """
        self.loop = None
        self.datapath = datapath
        self._event_listeners = []
        self.config = MassConfig(self)
        # init modules
        self.database = Database(self)
        self.cache = Cache(self)
        self.metadata = MetaData(self)
        self.web = Web(self)
        self.hass = HomeAssistant(self)
        self.music_manager = MusicManager(self)
        self.player_manager = PlayerManager(self)
        self.http_streamer = HTTPStreamer(self)
        # shared zeroconf instance
        self.zeroconf = zeroconf.Zeroconf()

    async def start(self):
        """start running the music assistant server"""
        self.loop = asyncio.get_event_loop()
        self.loop.set_exception_handler(self.__handle_exception)
        await self.database.setup()
        await self.cache.setup()
        await self.metadata.setup()
        await self.hass.setup()
        await self.music_manager.setup()
        await self.player_manager.async_setup()
        await self.web.setup()
        await self.http_streamer.setup()
        # wait for exit
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            LOGGER.info("Application shutdown")
            await self.signal_event(EVENT_SHUTDOWN)
            self.config.save()
            await self.database.close()
            await self.cache.close()

    @callback
    def signal_event(self, event_msg: str, event_details: Any = None):
        """
            Signal (systemwide) event.
                :param event_msg: the eventmessage to signal
                :param event_details: optional details to send with the event.
        """
        for cb_func, event_filter in self._event_listeners:
            if not event_filter or event_filter in event_msg:
                self.async_add_job(cb_func, event_msg, event_details)

    async def async_signal_event(self, event_msg: str, event_details: Any = None):
        """
            Signal (systemwide) event.
                :param event_msg: the eventmessage to signal
                :param event_details: optional details to send with the event.
        """
        await self.async_add_job(self.signal_event, event_msg, event_details)

    @callback
    def add_event_listener(self, cb_func: Callable[..., Union[None, Awaitable]], event_filter: Union[None, str, List] = None) -> Callable:
        """
            Add callback to event listeners.
            Returns function to remove the listener.
                :param cb_func: callback function or coroutine
                :param event_filter: Optionally only listen for these events
        """
        listener = (cb_func, event_filter)
        self._event_listeners.append(listener)

        def remove_listener():
            self._event_listeners.pop(listener)
        return remove_listener

    def add_job(self, target: Callable[..., Any], *args: Any) -> None:
        """Add job to the executor pool.
        target: target to call.
        args: parameters for method to call.
        """
        self.loop.call_soon_threadsafe(self.async_add_job, target, *args)

    async def async_add_job(
        self, target: Callable[..., Any], *args: Any
    ) -> Optional[asyncio.Future]:
        """Add a job from within the event loop.
        This method must be run in the event loop.
        target: target to call.
        args: parameters for method to call.
        """
        task = None

        # Check for partials to properly determine if coroutine function
        check_target = target
        while isinstance(check_target, functools.partial):
            check_target = check_target.func

        if asyncio.iscoroutine(check_target):
            task = self.loop.create_task(target)  # type: ignore
        elif asyncio.iscoroutinefunction(check_target):
            task = self.loop.create_task(target(*args))
        elif is_callback(check_target):
            self.loop.call_soon(target, *args)
        else:
            task = self.loop.run_in_executor(  # type: ignore
                None, target, *args
            )
        return task

    @callback
    def create_task(self, target: Coroutine) -> asyncio.tasks.Task:
        """Create a task from within the eventloop.
        This method must be run in the event loop.
        target: target to call.
        """
        task: asyncio.tasks.Task = self.loop.create_task(target)
        return task

    async def async_run_job(
        self, target: Callable[..., Union[None, Awaitable]], *args: Any
    ) -> None:
        """
            Run a job from within the event loop.
            This method must be run in the event loop.
                :param target: target to call.
                :param args: parameters for method to call.
        """
        if (
            not asyncio.iscoroutine(target)
            and not asyncio.iscoroutinefunction(target)
            and is_callback(target)
        ):
            target(*args)
        else:
            await self.async_add_job(target, *args)

    def __handle_exception(self, loop, context):
        """Global exception handler."""
        LOGGER.error("Caught exception: %s", context)
        loop.default_exception_handler(context)
