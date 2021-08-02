"""Main Music Assistant class."""

import asyncio
import importlib
import logging
import os
from typing import Dict, Optional, Tuple

import aiohttp
import music_assistant.helpers.util as util
from music_assistant.constants import (
    CONF_ENABLED,
    EVENT_PROVIDER_REGISTERED,
    EVENT_PROVIDER_UNREGISTERED,
    EVENT_SHUTDOWN,
)
from music_assistant.helpers.cache import Cache
from music_assistant.helpers.migration import check_migrations
from music_assistant.helpers.util import callback, create_task, get_ip_pton
from music_assistant.managers.config import ConfigManager
from music_assistant.managers.database import DatabaseManager
from music_assistant.managers.events import EventBus
from music_assistant.managers.library import LibraryManager
from music_assistant.managers.metadata import MetaDataManager
from music_assistant.managers.music import MusicManager
from music_assistant.managers.players import PlayerManager
from music_assistant.managers.tasks import TaskManager
from music_assistant.models.provider import Provider, ProviderType
from music_assistant.web import WebServer
from zeroconf import InterfaceChoice, NonUniqueNameException, ServiceInfo, Zeroconf

LOGGER = logging.getLogger("mass")


def global_exception_handler(loop: asyncio.AbstractEventLoop, context: Dict) -> None:
    """Global exception handler."""
    LOGGER.debug("Caught exception: %s", context.get("exception", context["message"]))
    if "Broken pipe" in str(context.get("exception")):
        # fix for the spamming subprocess
        return
    loop.default_exception_handler(context)


class MusicAssistant:
    """Main MusicAssistant object."""

    def __init__(self, datapath: str, debug: bool = False, port: int = 8095) -> None:
        """
        Create an instance of MusicAssistant.

            :param datapath: file location to store the data
        """

        self._exit = False
        self._loop = None
        self.debug = debug
        self._http_session = None

        self._providers = {}

        # init core managers/controllers
        self._eventbus = EventBus(self)
        self._config = ConfigManager(self, datapath)
        self._tasks = TaskManager(self)
        self._database = DatabaseManager(self)
        self._cache = Cache(self)
        self._metadata = MetaDataManager(self)
        self._web = WebServer(self, port)
        self._music = MusicManager(self)
        self._library = LibraryManager(self)
        self._players = PlayerManager(self)
        # shared zeroconf instance
        self.zeroconf = Zeroconf(interfaces=InterfaceChoice.All)

    async def start(self) -> None:
        """Start running the music assistant server."""
        # initialize loop
        self._loop = asyncio.get_event_loop()
        util.DEFAULT_LOOP = self._loop
        self._loop.set_exception_handler(global_exception_handler)
        self._loop.set_debug(self.debug)
        # create shared aiohttp ClientSession
        self._http_session = aiohttp.ClientSession(
            loop=self.loop,
            connector=aiohttp.TCPConnector(enable_cleanup_closed=True, ssl=False),
        )
        # run migrations if needed
        await check_migrations(self)
        await self._tasks.setup()
        await self._config.setup()
        await self._cache.setup()
        await self._music.setup()
        await self._players.setup()
        await self._preload_providers()
        await self.setup_discovery()
        await self._web.setup()
        await self._library.setup()
        self.tasks.add("Save config", self.config.save)

    async def stop(self) -> None:
        """Stop running the music assistant server."""
        self._exit = True
        LOGGER.info("Application shutdown")
        self._eventbus.signal(EVENT_SHUTDOWN)
        await self.config.close()
        await self._web.stop()
        for prov in self._providers.values():
            await prov.on_stop()
        await self._players.close()
        await self._http_session.connector.close()
        self._http_session.detach()

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        """Return the running event loop."""
        return self._loop

    @property
    def exit(self) -> bool:
        """Return bool if the main process is exiting."""
        return self._exit

    @property
    def players(self) -> PlayerManager:
        """Return the Players controller/manager."""
        return self._players

    @property
    def music(self) -> MusicManager:
        """Return the Music controller/manager."""
        return self._music

    @property
    def library(self) -> LibraryManager:
        """Return the Library controller/manager."""
        return self._library

    @property
    def config(self) -> ConfigManager:
        """Return the Configuration controller/manager."""
        return self._config

    @property
    def cache(self) -> Cache:
        """Return the Cache instance."""
        return self._cache

    @property
    def database(self) -> DatabaseManager:
        """Return the Database controller/manager."""
        return self._database

    @property
    def metadata(self) -> MetaDataManager:
        """Return the Metadata controller/manager."""
        return self._metadata

    @property
    def tasks(self) -> TaskManager:
        """Return the Tasks controller/manager."""
        return self._tasks

    @property
    def eventbus(self) -> EventBus:
        """Return the EventBus."""
        return self._eventbus

    @property
    def web(self) -> WebServer:
        """Return the webserver instance."""
        return self._web

    @property
    def http_session(self) -> aiohttp.ClientSession:
        """Return the default http session."""
        return self._http_session

    async def register_provider(self, provider: Provider) -> None:
        """Register a new Provider/Plugin."""
        assert provider.id and provider.name
        if provider.id in self._providers:
            LOGGER.debug("Provider %s is already registered.", provider.id)
            return
        provider.mass = self  # make sure we have the mass object
        provider.available = False
        self._providers[provider.id] = provider
        if self.config.get_provider_config(provider.id, provider.type)[CONF_ENABLED]:
            if await provider.on_start() is not False:
                provider.available = True
                LOGGER.debug("Provider registered: %s", provider.name)
                self.eventbus.signal(EVENT_PROVIDER_REGISTERED, provider.id)
            else:
                LOGGER.debug(
                    "Provider registered but loading failed: %s", provider.name
                )
        else:
            LOGGER.debug("Not loading provider %s as it is disabled", provider.name)

    async def unregister_provider(self, provider_id: str) -> None:
        """Unregister an existing Provider/Plugin."""
        if provider_id in self._providers:
            # unload it if it's loaded
            await self._providers[provider_id].on_stop()
            LOGGER.debug("Provider unregistered: %s", provider_id)
            self.eventbus.signal(EVENT_PROVIDER_UNREGISTERED, provider_id)
        return self._providers.pop(provider_id, None)

    async def reload_provider(self, provider_id: str) -> None:
        """Reload an existing Provider/Plugin."""
        provider = await self.unregister_provider(provider_id)
        if provider is not None:
            # simply re-register the same provider again
            await self.register_provider(provider)
        else:
            # try preloading all providers
            self.tasks.add("Reload providers", self._preload_providers)

    @callback
    def get_provider(self, provider_id: str) -> Provider:
        """Return provider/plugin by id."""
        if provider_id not in self._providers:
            LOGGER.warning("Provider %s is not available", provider_id)
            return None
        return self._providers[provider_id]

    @callback
    def get_providers(
        self,
        filter_type: Optional[ProviderType] = None,
        include_unavailable: bool = False,
    ) -> Tuple[Provider]:
        """Return all providers, optionally filtered by type."""
        return tuple(
            item
            for item in self._providers.values()
            if (filter_type is None or item.type == filter_type)
            and (include_unavailable or item.available)
        )

    async def setup_discovery(self) -> None:
        """Make this Music Assistant instance discoverable on the network."""

        def _setup_discovery():
            zeroconf_type = "_music-assistant._tcp.local."

            info = ServiceInfo(
                zeroconf_type,
                name=f"{self.web.server_id}.{zeroconf_type}",
                addresses=[get_ip_pton()],
                port=self.web.port,
                properties=self.web.discovery_info,
                server=f"mass_{self.web.server_id}.local.",
            )
            LOGGER.debug("Starting Zeroconf broadcast...")
            try:
                existing = getattr(self, "mass_zc_service_set", None)
                if existing:
                    self.zeroconf.update_service(info)
                else:
                    self.zeroconf.register_service(info)
                setattr(self, "mass_zc_service_set", True)
            except NonUniqueNameException:
                LOGGER.error(
                    "Music Assistant instance with identical name present in the local network!"
                )

        create_task(_setup_discovery)

    async def _preload_providers(self) -> None:
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
                    await prov_mod.setup(self)
                # pylint: disable=broad-except
                except Exception as exc:
                    LOGGER.exception("Error preloading module %s: %s", module_name, exc)
                else:
                    LOGGER.debug("Successfully preloaded module %s", module_name)
