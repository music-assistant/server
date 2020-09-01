"""Main Music Assistant class."""

import asyncio
import functools
import importlib
import logging
import os
import threading
from typing import Any, Awaitable, Callable, List, Optional, Union

import zeroconf
from music_assistant.cache import Cache
from music_assistant.config import MassConfig
from music_assistant.constants import CONF_ENABLED, EVENT_SHUTDOWN
from music_assistant.database import Database
from music_assistant.http_streamer import HTTPStreamer
from music_assistant.metadata import MetaData
from music_assistant.models.provider import Provider, ProviderType
from music_assistant.music_manager import MusicManager
from music_assistant.player_manager import PlayerManager
from music_assistant.utils import callback, is_callback
from music_assistant.web import Web

LOGGER = logging.getLogger("mass")


#pylint: disable=too-many-instance-attributes
class MusicAssistant:
    """Main MusicAssistant object."""

    def __init__(self, datapath):
        """
            Create an instance of MusicAssistant
                :param datapath: file location to store the data
        """

        self.loop = None
        self._event_listeners = []
        self._providers = {}
        self.config = MassConfig(self, datapath)
        # init modules
        self.database = Database(self)
        self.cache = Cache(self)
        self.metadata = MetaData(self)
        self.web = Web(self)
        self.music_manager = MusicManager(self)
        self.player_manager = PlayerManager(self)
        self.http_streamer = HTTPStreamer(self)
        # shared zeroconf instance
        self.zeroconf = zeroconf.Zeroconf()

    async def async_start(self):
        """Start running the music assistant server."""
        self.loop = asyncio.get_event_loop()
        self.loop.set_exception_handler(self.__handle_exception)
        self.loop.set_debug(True)
        await self.database.async_setup()
        await self.cache.async_setup()
        await self.metadata.async_setup()
        await self.music_manager.async_setup()
        await self.player_manager.async_setup()
        await self.web.async_setup()
        await self.http_streamer.async_setup()
        await self.async_preload_providers()

    async def async_stop(self):
        """stop running the music assistant server"""
        LOGGER.info("Application shutdown")
        self.signal_event(EVENT_SHUTDOWN)
        await self.config.async_save()
        for prov in self._providers.values():
            await prov.async_on_stop()
        await self.player_manager.async_close()

    async def async_register_provider(self, provider: Provider):
        """Register a new Provider/Plugin."""
        assert provider.id and provider.name
        assert provider.id not in self._providers  # provider id's must be unique!
        provider.mass = self  # make sure we have the mass object
        provider.available = False
        self._providers[provider.id] = provider
        if self.config.providers[provider.id][CONF_ENABLED]:
            if await provider.async_on_start():
                provider.available = True
                LOGGER.info("New provider registered: %s", provider.name)
        else:
            LOGGER.debug("Not loading provider %s as it is disabled:", provider.name)

    async def register_provider(self, provider: Provider):
        """Register a new Provider/Plugin."""
        self.add_job(self.async_register_provider(provider))

    @callback
    def get_provider(self, provider_id: str) -> Provider:
        """Return provider/plugin by id."""
        if not provider_id in self._providers:
            raise KeyError("Provider %s is not available" % provider_id)
        return self._providers[provider_id]

    @callback
    def get_providers(self, filter_type: Optional[ProviderType] = None) -> List[Provider]:
        """Return all providers, optionally filtered by type."""
        return [item for item in self._providers.values()
                if (filter_type is None or item.type == filter_type)
                and item.available]

    async def async_preload_providers(self):
        """Dynamically load all providermodules."""
        base_dir = os.path.dirname(os.path.abspath(__file__))
        modules_path = os.path.join(base_dir, "providers")
        # load modules
        for dir_str in os.listdir(modules_path):
            dir_path = os.path.join(modules_path, dir_str)
            if not os.path.isdir(dir_path):
                continue
            # get files in directory
            for file_str in os.listdir(dir_path):
                file_path = os.path.join(dir_path, file_str)
                if not os.path.isfile(file_path):
                    continue
                if not file_str == "__init__.py":
                    continue
                module_name = dir_str
                if module_name in [i.id for i in self._providers.values()]:
                    continue
                # try to load the module
                try:
                    prov_mod = importlib.import_module(
                        f".{module_name}", "music_assistant.providers"
                    )
                    await prov_mod.async_setup(self)
                # pylint: disable=broad-except
                except Exception as exc:
                    LOGGER.exception("Error preloading module %s: %s", module_name, exc)
                else:
                    LOGGER.info("Successfully preloaded module %s", module_name)

    @callback
    def signal_event(self, event_msg: str, event_details: Any = None):
        """
            Signal (systemwide) event.
                :param event_msg: the eventmessage to signal
                :param event_details: optional details to send with the event.
        """
        for cb_func, event_filter in self._event_listeners:
            if not event_filter or event_filter in event_msg:
                self.add_job(cb_func, event_msg, event_details)

    async def async_signal_event(self, event_msg: str, event_details: Any = None):
        """
            Signal (systemwide) event.
                :param event_msg: the eventmessage to signal
                :param event_details: optional details to send with the event.
        """
        self.add_job(self.signal_event, event_msg, event_details)

    @callback
    def add_event_listener(self, cb_func: Callable[..., Union[None, Awaitable]],
                           event_filter: Union[None, str, List] = None) -> Callable:
        """
            Add callback to event listeners.
            Returns function to remove the listener.
                :param cb_func: callback function or coroutine
                :param event_filter: Optionally only listen for these events
        """
        listener = (cb_func, event_filter)
        self._event_listeners.append(listener)

        def remove_listener():
            self._event_listeners.remove(listener)
        return remove_listener

    def add_job(
        self, target: Callable[..., Any], *args: Any
    ) -> Optional[asyncio.Future]:
        """Add a job/task to the event loop.
            target: target to call.
            args: parameters for method to call.
        """
        task = None

        # Check for partials to properly determine if coroutine function
        check_target = target
        while isinstance(check_target, functools.partial):
            check_target = check_target.func

        if threading.current_thread() is not threading.main_thread():
            # called from other thread
            if asyncio.iscoroutine(check_target):
                task = asyncio.run_coroutine_threadsafe(target, self.loop)  # type: ignore
            elif asyncio.iscoroutinefunction(check_target):
                task = asyncio.run_coroutine_threadsafe(target(*args), self.loop)
            elif is_callback(check_target):
                task = self.loop.call_soon_threadsafe(target, *args)
            else:
                task = self.loop.run_in_executor(  # type: ignore
                    None, target, *args
                )
        else:
            # called from mainthread
            if asyncio.iscoroutine(check_target):
                task = self.loop.create_task(target)  # type: ignore
            elif asyncio.iscoroutinefunction(check_target):
                task = self.loop.create_task(target(*args))
            elif is_callback(check_target):
                task = self.loop.call_soon(target, *args)
            else:
                task = self.loop.run_in_executor(  # type: ignore
                    None, target, *args
                )
        return task

    def __handle_exception(self, loop, context):
        """Global exception handler."""
        LOGGER.error("Caught exception: %s", context)
        loop.default_exception_handler(context)
