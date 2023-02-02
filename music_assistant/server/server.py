"""Main Music Assistant class."""
from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
import os
from types import TracebackType
from typing import Any, Callable, Coroutine, Type

from aiohttp import ClientSession, TCPConnector, web
from music_assistant.common.helpers.util import select_free_port

from music_assistant.common.models.provider_manifest import ProviderManifest
from music_assistant.common.models.enums import EventType
from music_assistant.common.models.errors import (
    ProviderUnavailableError,
    SetupFailedError,
)
from music_assistant.common.models.event import MassEvent
from music_assistant.constants import (
    CONF_WEB_HOST,
    CONF_WEB_PORT,
    DEFAULT_HOST,
    DEFAULT_PORT,
    ROOT_LOGGER_NAME,
)
from music_assistant.common.models.config_entries import (
    CONF_KEY_ENABLED,
    CONFIG_ENTRY_ENABLED,
    ProviderConfig,
)
from music_assistant.server.controllers.cache import CacheController
from music_assistant.server.controllers.config import ConfigController
from music_assistant.server.controllers.metadata.metadata import MetaDataController
from music_assistant.server.controllers.music import MusicController
from music_assistant.server.controllers.players import PlayerController
from music_assistant.server.controllers.streams import StreamsController
from music_assistant.server.helpers.api import (
    APICommandHandler,
    api_command,
    mount_websocket,
)

from .models.metadata_provider import MetadataProvider
from .models.music_provider import MusicProvider
from .models.player_provider import PlayerProvider

ProviderType = MetadataProvider | MusicProvider | PlayerProvider
EventCallBackType = Callable[[EventType, Any], None]
EventSubscriptionType = tuple[
    EventCallBackType, tuple[EventType] | None, tuple[str] | None
]

LOGGER = logging.getLogger(ROOT_LOGGER_NAME)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROVIDERS_PATH = os.path.join(BASE_DIR, "providers")


class MusicAssistant:
    """Main MusicAssistant (Server) object."""

    loop: asyncio.AbstractEventLoop | None = None
    http_session: ClientSession | None = None
    _web_apprunner: web.AppRunner | None = None
    _web_tcp: web.TCPSite | None = None

    def __init__(self, storage_path: str, port: int | None = None) -> None:
        """
        Create an instance of the MusicAssistant Server."""
        self.storage_path = storage_path
        self.port = port
        self._subscribers: set[EventCallBackType] = set()
        self._available_providers: dict[str, ProviderManifest] = {}
        self._providers: dict[str, ProviderType] = {}

        # init core controllers
        self.config = ConfigController(self)
        self.cache = CacheController(self)
        self.metadata = MetaDataController(self)
        self.music = MusicController(self)
        self.players = PlayerController(self)
        self.streams = StreamsController(self)
        self._tracked_tasks: list[asyncio.Task] = []
        self.closed = False
        self.loop: asyncio.AbstractEventLoop | None = None
        # we dynamically register command handlers
        self.webapp = web.Application()
        self.command_handlers: dict[str, APICommandHandler] = {}
        self._register_api_commands()

    async def start(self) -> None:
        """Start running the Music Assistant server."""
        self.loop = asyncio.get_running_loop()
        # if port is None, we need to autoselect it, prefer 9000 (lms)
        if self.port is None:
            self.port = await select_free_port(9000, 9200)
        # create shared aiohttp ClientSession
        self.http_session = ClientSession(
            loop=self.loop,
            connector=TCPConnector(ssl=False),
        )
        # setup core controllers
        await self.config.setup()
        await self.cache.setup()
        await self.music.setup()
        await self.metadata.setup()
        await self.players.setup()
        await self.streams.setup()
        # load providers
        await self._load_providers()
        # setup web server
        mount_websocket(self, "/ws")
        self._web_apprunner = web.AppRunner(self.webapp, access_log=None)
        await self._web_apprunner.setup()
        # set host to None to bind to all addresses on both IPv4 and IPv6
        host = None
        self._http = web.TCPSite(self._web_apprunner, host=host, port=self.port)
        await self._http.start()

    async def stop(self) -> None:
        """Stop running the music assistant server."""
        LOGGER.info("Stop called, cleaning up...")
        self.signal_event(EventType.SHUTDOWN)
        self.closed = True
        # cancel all running tasks
        for task in self._tracked_tasks:
            task.cancel()
        # stop/clean streams controller
        await self.streams.close()
        # stop/clean webserver
        await self._http.stop()
        await self._web_apprunner.cleanup()
        await self.webapp.shutdown()
        await self.webapp.cleanup()
        # stop core controllers
        await self.metadata.close()
        await self.music.close()
        await self.players.close()
        # cleanup all providers
        for prov in self._providers.values():
            await prov.close()
        # cleanup cache and config
        await self.config.close()
        await self.cache.close()
        # close/cleanup shared http session
        if self.http_session:
            await self.http_session.close()

    @api_command("providers/available")
    def get_available_providers(self) -> list[ProviderManifest]:
        """Return all available Providers."""
        return list(self._available_providers.values())

    @property
    def providers(self) -> list[ProviderType]:
        """Return all loaded/running Providers (instances)."""
        return list(self._providers.values())

    def get_provider(self, provider_instance_or_domain: str) -> ProviderType:
        """Return provider by instance id (or domain)."""
        if prov := self._providers.get(provider_instance_or_domain):
            return prov
        for prov in self._providers.values():
            if prov.domain == provider_instance_or_domain:
                return prov
        raise ProviderUnavailableError(
            f"Provider {provider_instance_or_domain} is not available"
        )

    def get_providers(self, provider_type: ProviderType | None) -> list[ProviderType]:
        """Return all loaded/running Providers (instances), optionally filtered by ProviderType."""
        return [
            x
            for x in self._providers.values()
            if provider_type is None or provider_type == x.type
        ]

    def signal_event(
        self,
        event: EventType,
        object_id: str | None = None,
        data: Any = None,
    ) -> None:
        """Signal event to subscribers."""
        if self.closed:
            return

        if LOGGER.isEnabledFor(logging.DEBUG):
            if event != EventType.QUEUE_TIME_UPDATED:
                # do not log queue time updated events because that is too chatty
                LOGGER.getChild("event").debug("%s %s", event.value, object_id or "")

        event_obj = MassEvent(event=event, object_id=object_id, data=data)
        for cb_func, event_filter, id_filter in self._subscribers:
            if not (event_filter is None or event in event_filter):
                continue
            if not (id_filter is None or object_id in id_filter):
                continue
            if asyncio.iscoroutinefunction(cb_func):
                asyncio.run_coroutine_threadsafe(cb_func(event_obj), self.loop)
            else:
                self.loop.call_soon_threadsafe(cb_func, event_obj)

    def subscribe(
        self,
        cb_func: EventCallBackType,
        event_filter: EventType | tuple[EventType] | None = None,
        id_filter: str | tuple[str] | None = None,
    ) -> Callable:
        """
        Add callback to event listeners.

        Returns function to remove the listener.
            :param cb_func: callback function or coroutine
            :param event_filter: Optionally only listen for these events
            :param id_filter: Optionally only listen for these id's (player_id, queue_id, uri)
        """
        if isinstance(event_filter, EventType):
            event_filter = (event_filter,)
        if isinstance(id_filter, str):
            id_filter = (id_filter,)
        listener = (cb_func, event_filter, id_filter)
        self._subscribers.add(listener)

        def remove_listener():
            self._subscribers.remove(listener)

        return remove_listener

    def create_task(
        self,
        target: Coroutine,
        *args: Any,
        **kwargs: Any,
    ) -> asyncio.Task | asyncio.Future:
        """
        Create Task on (main) event loop from Callable or awaitable.

        Tasks created by this helper will be properly cancelled on stop.
        """
        if self.closed:
            return

        if asyncio.iscoroutinefunction(target):
            task = self.loop.create_task(target(*args, **kwargs))
        else:
            task = self.loop.create_task(target)

        def task_done_callback(*args, **kwargs):
            self._tracked_tasks.remove(task)

        self._tracked_tasks.append(task)
        task.add_done_callback(task_done_callback)
        return task

    def register_api_command(
        self,
        command: str,
        handler: Callable,
    ) -> None:
        """Dynamically register a command on the API."""
        assert command not in self.command_handlers, "Command already registered"
        self.command_handlers[command] = APICommandHandler.parse(command, handler)

    def _register_api_commands(self) -> None:
        """Register all methods decorated as api_command within a class(instance)."""
        for cls in (self, self.config, self.metadata, self.music, self.players):
            for attr_name in dir(cls):
                if attr_name.startswith("__"):
                    continue
                obj = getattr(cls, attr_name)
                if hasattr(obj, "api_cmd"):
                    # method is decorated with our api decorator
                    self.register_api_command(obj.api_cmd, obj)

    async def _load_providers(self) -> None:
        """Load providers from config."""
        # load all available providers from manifest files
        await self.__load_available_providers()
        # load all configured providers
        for prov_conf in self.config.get_provider_configs():
            await self._load_provider(prov_conf)
        # check for any 'load_by_default' providers (e.g. URL provider)
        for prov_manifest in self._available_providers.values():
            if not prov_manifest.load_by_default:
                continue
            loaded = any(
                (x for x in self.providers if x.domain == prov_manifest.domain)
            )
            if loaded:
                continue
            await self._load_provider(
                ProviderConfig(
                    type=prov_manifest.type,
                    domain=prov_manifest.domain,
                    instance_id=prov_manifest.domain,
                    values={},
                )
            )

        # listen to config change events
        async def handle_config_updated_event(event: MassEvent):
            """Handle ProviderConfig updated/created event."""
            await self._load_provider(event.data)

        self.subscribe(
            handle_config_updated_event,
            (EventType.PROVIDER_CONFIG_CREATED, EventType.PROVIDER_CONFIG_UPDATED),
        )

    async def _load_provider(self, conf: ProviderConfig) -> None:
        """Load (or reload) a provider."""
        if provider := self._providers.get(conf.instance_id):
            # provider is already loaded, stop and unload it first
            await provider.close()
            self._providers.pop(conf.instance_id)

        domain = conf.domain
        prov_manifest = self._available_providers.get(domain)
        # check for other instances of this provider
        existing = next((x for x in self.providers if x.domain == domain), None)
        if existing and not prov_manifest.multi_instance:
            raise SetupFailedError(
                f"Provider {domain} already loaded and only one instance allowed."
            )

        if not prov_manifest:
            raise SetupFailedError(f"Provider {domain} manifest not found")
        # try to load the module
        try:
            prov_mod = importlib.import_module(
                f".{domain}", "music_assistant.server.providers"
            )
            for name, obj in inspect.getmembers(prov_mod):
                if not inspect.isclass(obj):
                    continue
                # lookup class to initialize
                if name == prov_manifest.init_class or (
                    not prov_manifest.init_class
                    and issubclass(obj, (MusicProvider, PlayerProvider))
                    and obj != MusicProvider
                    and obj != PlayerProvider
                ):
                    prov_cls = obj
                    break
            else:
                SetupFailedError("Unable to locate Provider class")
            provider: ProviderType = prov_cls(self, prov_manifest, conf)
            self._providers[provider.instance_id] = provider
            await provider.setup()
        # pylint: disable=broad-except
        except Exception as exc:
            LOGGER.exception(
                "Error loading provider(instance) %s: %s",
                conf.title or conf.domain,
                str(exc),
            )
        else:
            LOGGER.debug(
                "Successfully loaded provider %s",
                conf.title or conf.domain,
            )

    async def __load_available_providers(self) -> None:
        """Preload all available provider manifest files."""
        for dir_str in os.listdir(PROVIDERS_PATH):
            dir_path = os.path.join(PROVIDERS_PATH, dir_str)
            if not os.path.isdir(dir_path):
                continue
            # get files in subdirectory
            for file_str in os.listdir(dir_path):
                file_path = os.path.join(dir_path, file_str)
                if not os.path.isfile(file_path):
                    continue
                if file_str != "manifest.json":
                    continue
                try:
                    provider_manifest = await ProviderManifest.parse(file_path)
                    # inject config entry to enable/disable the provider
                    conf_keys = (x.key for x in provider_manifest.config_entries)
                    if CONF_KEY_ENABLED not in conf_keys:
                        provider_manifest.config_entries = [
                            CONFIG_ENTRY_ENABLED,
                            *provider_manifest.config_entries,
                        ]
                    self._available_providers[
                        provider_manifest.domain
                    ] = provider_manifest
                    LOGGER.debug("Loaded manifest for provider %s", dir_str)
                except Exception as exc:  # pylint: disable=broad-except
                    LOGGER.exception(
                        "Error while loading manifest for provider %s",
                        dir_str,
                        exc_info=exc,
                    )

    async def __aenter__(self) -> "MusicAssistant":
        """Return Context manager."""
        await self.setup()
        return self

    async def __aexit__(
        self,
        exc_type: Type[BaseException],
        exc_val: BaseException,
        exc_tb: TracebackType,
    ) -> bool | None:
        """Exit context manager."""
        await self.stop()
        if exc_val:
            raise exc_val
        return exc_type
