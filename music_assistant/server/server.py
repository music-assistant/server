"""Main Music Assistant class."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from collections.abc import Awaitable, Callable, Coroutine
from typing import TYPE_CHECKING, Any, Self
from uuid import uuid4

import aiofiles
from aiohttp import ClientSession, TCPConnector
from zeroconf import IPVersion, NonUniqueNameException, ServiceStateChange, Zeroconf
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

from music_assistant.common.helpers.util import get_ip_pton
from music_assistant.common.models.api import ServerInfoMessage
from music_assistant.common.models.enums import EventType, ProviderType
from music_assistant.common.models.errors import SetupFailedError
from music_assistant.common.models.event import MassEvent
from music_assistant.common.models.provider import ProviderManifest
from music_assistant.constants import (
    API_SCHEMA_VERSION,
    CONF_PROVIDERS,
    CONF_SERVER_ID,
    CONFIGURABLE_CORE_CONTROLLERS,
    MIN_SCHEMA_VERSION,
    ROOT_LOGGER_NAME,
)
from music_assistant.server.controllers.cache import CacheController
from music_assistant.server.controllers.config import ConfigController
from music_assistant.server.controllers.metadata import MetaDataController
from music_assistant.server.controllers.music import MusicController
from music_assistant.server.controllers.player_queues import PlayerQueuesController
from music_assistant.server.controllers.players import PlayerController
from music_assistant.server.controllers.streams import StreamsController
from music_assistant.server.controllers.webserver import WebserverController
from music_assistant.server.helpers.api import APICommandHandler, api_command
from music_assistant.server.helpers.images import get_icon_string
from music_assistant.server.helpers.util import (
    get_package_version,
    get_provider_module,
    install_package,
    is_hass_supervisor,
)

from .models import ProviderInstanceType

if TYPE_CHECKING:
    from types import TracebackType

    from music_assistant.common.models.config_entries import ProviderConfig
    from music_assistant.server.models.core_controller import CoreController


EventCallBackType = Callable[[MassEvent], None]
EventSubscriptionType = tuple[
    EventCallBackType, tuple[EventType, ...] | None, tuple[str, ...] | None
]

LOGGER = logging.getLogger(ROOT_LOGGER_NAME)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROVIDERS_PATH = os.path.join(BASE_DIR, "providers")

ENABLE_HTTP_CLEANUP_CLOSED = not (3, 11, 1) <= sys.version_info < (3, 11, 4)
# Enabling cleanup closed on python 3.11.1+ leaks memory relatively quickly
# see https://github.com/aio-libs/aiohttp/issues/7252
# aiohttp interacts poorly with https://github.com/python/cpython/pull/98540
# The issue was fixed in 3.11.4 via https://github.com/python/cpython/pull/104485


class MusicAssistant:
    """Main MusicAssistant (Server) object."""

    loop: asyncio.AbstractEventLoop
    http_session: ClientSession
    aiozc: AsyncZeroconf
    config: ConfigController
    webserver: WebserverController
    cache: CacheController
    metadata: MetaDataController
    music: MusicController
    players: PlayerController
    player_queues: PlayerQueuesController
    streams: StreamsController
    _aiobrowser: AsyncServiceBrowser

    def __init__(self, storage_path: str) -> None:
        """Initialize the MusicAssistant Server."""
        self.storage_path = storage_path
        # we dynamically register command handlers which can be consumed by the apis
        self.command_handlers: dict[str, APICommandHandler] = {}
        self._subscribers: set[EventSubscriptionType] = set()
        self._provider_manifests: dict[str, ProviderManifest] = {}
        self._providers: dict[str, ProviderInstanceType] = {}
        self._tracked_tasks: dict[str, asyncio.Task] = {}
        self.closing = False
        self.running_as_hass_addon: bool = False
        self.version: str = "0.0.0"

    async def start(self) -> None:
        """Start running the Music Assistant server."""
        self.loop = asyncio.get_running_loop()
        self.running_as_hass_addon = await is_hass_supervisor()
        self.version = await get_package_version("music_assistant")
        # create shared zeroconf instance
        # TODO: enumerate interfaces and enable IPv6 support
        self.aiozc = AsyncZeroconf(ip_version=IPVersion.V4Only)
        # create shared aiohttp ClientSession
        self.http_session = ClientSession(
            loop=self.loop,
            connector=TCPConnector(
                ssl=False,
                enable_cleanup_closed=ENABLE_HTTP_CLEANUP_CLOSED,
                limit=4096,
                limit_per_host=100,
            ),
        )
        # setup config controller first and fetch important config values
        self.config = ConfigController(self)
        await self.config.setup()
        LOGGER.info(
            "Starting Music Assistant Server (%s) version %s - uvloop: %s",
            self.server_id,
            self.version,
            "uvloop" in str(self.loop),
        )
        # setup other core controllers
        self.cache = CacheController(self)
        self.webserver = WebserverController(self)
        self.metadata = MetaDataController(self)
        self.music = MusicController(self)
        self.players = PlayerController(self)
        self.player_queues = PlayerQueuesController(self)
        self.streams = StreamsController(self)
        # add manifests for core controllers
        for controller_name in CONFIGURABLE_CORE_CONTROLLERS:
            controller: CoreController = getattr(self, controller_name)
            self._provider_manifests[controller.domain] = controller.manifest
        await self.cache.setup(await self.config.get_core_config("cache"))
        await self.webserver.setup(await self.config.get_core_config("webserver"))
        await self.music.setup(await self.config.get_core_config("music"))
        await self.metadata.setup(await self.config.get_core_config("metadata"))
        await self.players.setup(await self.config.get_core_config("players"))
        await self.player_queues.setup(await self.config.get_core_config("player_queues"))
        await self.streams.setup(await self.config.get_core_config("streams"))
        # register all api commands (methods with decorator)
        self._register_api_commands()
        # load providers
        await self._load_providers()
        # setup discovery
        self.create_task(self._setup_discovery())

    async def stop(self) -> None:
        """Stop running the music assistant server."""
        LOGGER.info("Stop called, cleaning up...")
        self.signal_event(EventType.SHUTDOWN)
        self.closing = True
        # cancel all running tasks
        for task in self._tracked_tasks.values():
            task.cancel()
        # cleanup all providers
        async with asyncio.TaskGroup() as tg:
            for prov_id in list(self._providers.keys()):
                tg.create_task(self.unload_provider(prov_id))
        # stop core controllers
        await self.streams.close()
        await self.webserver.close()
        await self.metadata.close()
        await self.music.close()
        await self.player_queues.close()
        await self.players.close()
        # cleanup cache and config
        await self.config.close()
        await self.cache.close()
        # close/cleanup shared http session
        if self.http_session:
            await self.http_session.close()

    @property
    def server_id(self) -> str:
        """Return unique ID of this server."""
        if not self.config.initialized:
            return ""
        return self.config.get(CONF_SERVER_ID)  # type: ignore[no-any-return]

    @api_command("info")
    def get_server_info(self) -> ServerInfoMessage:
        """Return Info of this server."""
        return ServerInfoMessage(
            server_id=self.server_id,
            server_version=self.version,
            schema_version=API_SCHEMA_VERSION,
            min_supported_schema_version=MIN_SCHEMA_VERSION,
            base_url=self.webserver.base_url,
            homeassistant_addon=self.running_as_hass_addon,
        )

    @api_command("providers/manifests")
    def get_provider_manifests(self) -> list[ProviderManifest]:
        """Return all Provider manifests."""
        return list(self._provider_manifests.values())

    @api_command("providers/manifests/get")
    def get_provider_manifest(self, domain: str) -> ProviderManifest:
        """Return Provider manifests of single provider(domain)."""
        return self._provider_manifests[domain]

    @api_command("providers")
    def get_providers(
        self, provider_type: ProviderType | None = None
    ) -> list[ProviderInstanceType]:
        """Return all loaded/running Providers (instances), optionally filtered by ProviderType."""
        return [
            x for x in self._providers.values() if provider_type is None or provider_type == x.type
        ]

    @api_command("logging/get")
    async def get_application_log(self) -> str:
        """Return the application log from file."""
        logfile = os.path.join(self.storage_path, "musicassistant.log")
        async with aiofiles.open(logfile, "r") as _file:
            return await _file.read()

    @property
    def providers(self) -> list[ProviderInstanceType]:
        """Return all loaded/running Providers (instances)."""
        return list(self._providers.values())

    def get_provider(
        self, provider_instance_or_domain: str, return_unavailable: bool = False
    ) -> ProviderInstanceType | None:
        """Return provider by instance id or domain."""
        # lookup by instance_id first
        if prov := self._providers.get(provider_instance_or_domain):
            if return_unavailable or prov.available:
                return prov
            if not prov.is_streaming_provider:
                # no need to lookup other instances because this provider has unique data
                return None
            provider_instance_or_domain = prov.domain
        # fallback to match on domain
        for prov in self._providers.values():
            if prov.domain != provider_instance_or_domain:
                continue
            if return_unavailable or prov.available:
                return prov
        LOGGER.debug("Provider %s is not available", provider_instance_or_domain)
        return None

    def signal_event(
        self,
        event: EventType,
        object_id: str | None = None,
        data: Any = None,
    ) -> None:
        """Signal event to subscribers."""
        if self.closing:
            return

        if LOGGER.isEnabledFor(logging.DEBUG) and event != EventType.QUEUE_TIME_UPDATED:
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
        """Add callback to event listeners.

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

        def remove_listener() -> None:
            self._subscribers.remove(listener)

        return remove_listener

    def create_task(
        self,
        target: Coroutine | Awaitable | Callable | asyncio.Future,
        *args: Any,
        task_id: str | None = None,
        **kwargs: Any,
    ) -> asyncio.Task | asyncio.Future:
        """Create Task on (main) event loop from Coroutine(function).

        Tasks created by this helper will be properly cancelled on stop.
        """
        if target is None:
            msg = "Target is missing"
            raise RuntimeError(msg)
        if existing := self._tracked_tasks.get(task_id):
            # prevent duplicate tasks if task_id is given and already present
            return existing
        if asyncio.iscoroutinefunction(target):
            task = self.loop.create_task(target(*args, **kwargs))
        elif asyncio.iscoroutine(target):
            task = self.loop.create_task(target)
        elif isinstance(target, asyncio.Future):
            task = target
        else:
            # assume normal callable (non coroutine or awaitable)
            task = self.loop.create_task(asyncio.to_thread(target, *args, **kwargs))

        def task_done_callback(_task: asyncio.Future | asyncio.Task) -> None:
            _task_id = task.task_id
            self._tracked_tasks.pop(_task_id)
            # print unhandled exceptions
            if LOGGER.isEnabledFor(logging.DEBUG) and not _task.cancelled() and _task.exception():
                task_name = _task.get_name() if hasattr(_task, "get_name") else _task
                LOGGER.exception(
                    "Exception in task %s - target: %s",
                    task_name,
                    str(target),
                    exc_info=task.exception(),
                )

        if task_id is None:
            task_id = uuid4().hex
        task.task_id = task_id
        self._tracked_tasks[task_id] = task
        task.add_done_callback(task_done_callback)
        return task

    def call_later(
        self,
        delay: float,
        target: Coroutine | Awaitable | Callable | asyncio.Future,
        *args: Any,
        task_id: str | None = None,
        **kwargs: Any,
    ) -> asyncio.Task | asyncio.Future:
        """Run callable/awaitable after given delay."""

        def _create_task() -> None:
            self.create_task(target, *args, task_id=task_id, **kwargs)

        self.loop.call_later(delay, _create_task)

    def get_task(self, task_id: str) -> asyncio.Task | asyncio.Future:
        """Get existing scheduled task."""
        if existing := self._tracked_tasks.get(task_id):
            # prevent duplicate tasks if task_id is given and already present
            return existing
        msg = "Task does not exist"
        raise KeyError(msg)

    def register_api_command(
        self,
        command: str,
        handler: Callable,
    ) -> None:
        """Dynamically register a command on the API."""
        if command in self.command_handlers:
            msg = f"Command {command} is already registered"
            raise RuntimeError(msg)
        self.command_handlers[command] = APICommandHandler.parse(command, handler)

    async def load_provider(self, conf: ProviderConfig) -> None:
        """Load (or reload) a provider."""
        # if provider is already loaded, stop and unload it first
        await self.unload_provider(conf.instance_id)
        LOGGER.debug("Loading provider %s", conf.name or conf.domain)
        if not conf.enabled:
            msg = "Provider is disabled"
            raise SetupFailedError(msg)

        # validate config
        try:
            conf.validate()
        except (KeyError, ValueError, AttributeError, TypeError) as err:
            msg = "Configuration is invalid"
            raise SetupFailedError(msg) from err

        domain = conf.domain
        prov_manifest = self._provider_manifests.get(domain)
        # check for other instances of this provider
        existing = next((x for x in self.providers if x.domain == domain), None)
        if existing and not prov_manifest.multi_instance:
            msg = f"Provider {domain} already loaded and only one instance allowed."
            raise SetupFailedError(msg)
        # check valid manifest (just in case)
        if not prov_manifest:
            msg = f"Provider {domain} manifest not found"
            raise SetupFailedError(msg)

        # handle dependency on other provider
        if prov_manifest.depends_on:
            for _ in range(30):
                if self.get_provider(prov_manifest.depends_on):
                    break
                await asyncio.sleep(1)
            else:
                msg = (
                    f"Provider {domain} depends on {prov_manifest.depends_on} "
                    "which is not available."
                )
                raise SetupFailedError(msg)

        # try to setup the module
        prov_mod = await get_provider_module(domain)
        try:
            async with asyncio.timeout(30):
                provider = await prov_mod.setup(self, prov_manifest, conf)
        except TimeoutError as err:
            msg = f"Provider {domain} did not load within 30 seconds"
            raise SetupFailedError(msg) from err
        # if we reach this point, the provider loaded successfully
        LOGGER.info(
            "Loaded %s provider %s",
            provider.type.value,
            conf.name or conf.domain,
        )
        provider.available = True
        self._providers[provider.instance_id] = provider
        self.create_task(provider.loaded_in_mass())
        self.config.set(f"{CONF_PROVIDERS}/{conf.instance_id}/last_error", None)
        self.signal_event(EventType.PROVIDERS_UPDATED, data=self.get_providers())
        # if this is a music provider, start sync
        if provider.type == ProviderType.MUSIC:
            self.music.start_sync(providers=[provider.instance_id])

    async def unload_provider(self, instance_id: str) -> None:
        """Unload a provider."""
        if provider := self._providers.get(instance_id):
            # remove mdns discovery if needed
            if provider.manifest.mdns_discovery:
                for mdns_type in provider.manifest.mdns_discovery:
                    self._aiobrowser.types.discard(mdns_type)
            # make sure to stop any running sync tasks first
            for sync_task in self.music.in_progress_syncs:
                if sync_task.provider_instance == instance_id:
                    sync_task.task.cancel()
                    await sync_task.task
            # check if there are no other providers dependent of this provider
            for dep_prov in self.providers:
                if dep_prov.manifest.depends_on == provider.domain:
                    await self.unload_provider(dep_prov.instance_id)
            try:
                await provider.unload()
            except Exception as err:
                LOGGER.warning("Error while unload provider %s: %s", provider.name, str(err))
            finally:
                self._providers.pop(instance_id, None)
                self.signal_event(EventType.PROVIDERS_UPDATED, data=self.get_providers())

    def _register_api_commands(self) -> None:
        """Register all methods decorated as api_command within a class(instance)."""
        for cls in (
            self,
            self.config,
            self.metadata,
            self.music,
            self.players,
            self.player_queues,
        ):
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
        await self.__load_provider_manifests()

        # create default config for any 'load_by_default' providers (e.g. URL provider)
        for prov_manifest in self._provider_manifests.values():
            if not prov_manifest.load_by_default:
                continue
            await self.config.create_default_provider_config(prov_manifest.domain)

        async def load_provider(prov_conf: ProviderConfig) -> None:
            """Try to load a provider and catch errors."""
            try:
                await self.load_provider(prov_conf)
            # pylint: disable=broad-except
            except Exception as exc:
                LOGGER.exception(
                    "Error loading provider(instance) %s",
                    prov_conf.name or prov_conf.domain,
                )
                # if loading failed, we store the error in the config object
                # so we can show something useful to the user
                prov_conf.last_error = str(exc)
                self.config.set(f"{CONF_PROVIDERS}/{prov_conf.instance_id}/last_error", str(exc))

        # load all configured (and enabled) providers
        prov_configs = await self.config.get_provider_configs(include_values=True)
        async with asyncio.TaskGroup() as tg:
            for prov_conf in prov_configs:
                if not prov_conf.enabled:
                    continue
                tg.create_task(load_provider(prov_conf))

    async def __load_provider_manifests(self) -> None:
        """Preload all available provider manifest files."""

        async def load_provider_manifest(provider_domain: str, provider_path: str) -> None:
            """Preload all available provider manifest files."""
            # get files in subdirectory
            for file_str in os.listdir(provider_path):
                file_path = os.path.join(provider_path, file_str)
                if not os.path.isfile(file_path):
                    continue
                if file_str != "manifest.json":
                    continue
                try:
                    provider_manifest: ProviderManifest = await ProviderManifest.parse(file_path)
                    # check for icon.svg file
                    if not provider_manifest.icon_svg:
                        icon_path = os.path.join(provider_path, "icon.svg")
                        if os.path.isfile(icon_path):
                            provider_manifest.icon_svg = await get_icon_string(icon_path)
                    # check for dark_icon file
                    if not provider_manifest.icon_svg_dark:
                        icon_path = os.path.join(provider_path, "icon_dark.svg")
                        if os.path.isfile(icon_path):
                            provider_manifest.icon_svg_dark = await get_icon_string(icon_path)
                    # try to load the module
                    try:
                        await get_provider_module(provider_manifest.domain)
                    except ImportError:
                        # install requirements
                        for requirement in provider_manifest.requirements:
                            await install_package(requirement)
                        # try loading the provider again to be safe
                        await get_provider_module(provider_manifest.domain)
                    self._provider_manifests[provider_manifest.domain] = provider_manifest
                    LOGGER.debug("Loaded manifest for provider %s", provider_manifest.name)
                except Exception as exc:  # pylint: disable=broad-except
                    LOGGER.exception(
                        "Error while loading manifest for provider %s",
                        provider_domain,
                        exc_info=exc,
                    )

        async with asyncio.TaskGroup() as tg:
            for dir_str in os.listdir(PROVIDERS_PATH):
                dir_path = os.path.join(PROVIDERS_PATH, dir_str)
                if not os.path.isdir(dir_path):
                    continue
                tg.create_task(load_provider_manifest(dir_str, dir_path))

    async def _setup_discovery(self) -> None:
        """Handle setup of MDNS discovery."""
        # create a global mdns browser
        all_types: set[str] = set()
        for prov_manifest in self._provider_manifests.values():
            if prov_manifest.mdns_discovery:
                all_types.update(prov_manifest.mdns_discovery)
        self._aiobrowser = AsyncServiceBrowser(
            self.aiozc.zeroconf,
            list(all_types),
            handlers=[self._on_mdns_service_state_change],
        )
        # register MA itself on mdns to be discovered
        zeroconf_type = "_mass._tcp.local."
        server_id = self.server_id
        LOGGER.debug("Starting Zeroconf broadcast...")
        info = AsyncServiceInfo(
            zeroconf_type,
            name=f"{server_id}.{zeroconf_type}",
            addresses=[await get_ip_pton(self.webserver.publish_ip)],
            port=self.webserver.publish_port,
            properties=self.get_server_info().to_dict(),
            server="mass.local.",
        )
        try:
            existing = getattr(self, "mass_zc_service_set", None)
            if existing:
                await self.aiozc.async_update_service(info)
            else:
                await self.aiozc.async_register_service(info)
            self.mass_zc_service_set = True
        except NonUniqueNameException:
            LOGGER.error(
                "Music Assistant instance with identical name present in the local network!"
            )

    def _on_mdns_service_state_change(
        self,
        zeroconf: Zeroconf,  # pylint: disable=unused-argument
        service_type: str,
        name: str,
        state_change: ServiceStateChange,
    ) -> None:
        """Handle MDNS service state callback."""

        async def process_mdns_state_change(prov: ProviderInstanceType):
            if state_change == ServiceStateChange.Removed:
                info = None
            else:
                info = AsyncServiceInfo(service_type, name)
                await info.async_request(zeroconf, 3000)
            await prov.on_mdns_service_state_change(name, state_change, info)

        LOGGER.debug(f"Service {name} of type {service_type} state changed: {state_change}")
        for prov in self._providers.values():
            if not prov.manifest.mdns_discovery:
                continue
            if service_type in prov.manifest.mdns_discovery:
                self.create_task(process_mdns_state_change(prov))

    async def __aenter__(self) -> Self:
        """Return Context manager."""
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> bool | None:
        """Exit context manager."""
        await self.stop()
        if exc_val:
            raise exc_val
        return exc_type
