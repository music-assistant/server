"""Main Music Assistant class."""

from __future__ import annotations

import asyncio
import logging
import os
import pathlib
import threading
from collections.abc import AsyncGenerator, Awaitable, Callable, Coroutine
from typing import TYPE_CHECKING, Any, Self, TypeGuard, TypeVar, cast, overload
from uuid import uuid4

import aiofiles
from aiofiles.os import wrap
from music_assistant_models.api import ServerInfoMessage
from music_assistant_models.enums import EventType, ProviderType
from music_assistant_models.errors import MusicAssistantError, SetupFailedError
from music_assistant_models.event import MassEvent
from music_assistant_models.helpers import set_global_cache_values
from music_assistant_models.provider import ProviderManifest
from zeroconf import (
    InterfaceChoice,
    IPVersion,
    NonUniqueNameException,
    ServiceStateChange,
    Zeroconf,
)
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

from music_assistant.constants import (
    API_SCHEMA_VERSION,
    CONF_PROVIDERS,
    CONF_SERVER_ID,
    CONFIGURABLE_CORE_CONTROLLERS,
    MASS_LOGGER_NAME,
    MIN_SCHEMA_VERSION,
    VERBOSE_LOG_LEVEL,
)
from music_assistant.controllers.cache import CacheController
from music_assistant.controllers.config import ConfigController
from music_assistant.controllers.metadata import MetaDataController
from music_assistant.controllers.music import MusicController
from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.players.player_controller import PlayerController
from music_assistant.controllers.streams import StreamsController
from music_assistant.controllers.webserver import WebserverController
from music_assistant.controllers.webserver.helpers.auth_middleware import get_current_user
from music_assistant.helpers.aiohttp_client import create_clientsession
from music_assistant.helpers.api import APICommandHandler, api_command
from music_assistant.helpers.images import get_icon_string
from music_assistant.helpers.util import (
    TaskManager,
    get_ip_pton,
    get_package_version,
    is_hass_supervisor,
    load_provider_module,
)
from music_assistant.models import ProviderInstanceType
from music_assistant.models.music_provider import MusicProvider
from music_assistant.models.player_provider import PlayerProvider

if TYPE_CHECKING:
    from types import TracebackType

    from aiohttp import ClientSession
    from music_assistant_models.config_entries import ProviderConfig

    from music_assistant.models.core_controller import CoreController

isdir = wrap(os.path.isdir)
isfile = wrap(os.path.isfile)
mkdirs = wrap(os.makedirs)
rmfile = wrap(os.remove)
listdir = wrap(os.listdir)
rename = wrap(os.rename)

EventCallBackType = Callable[[MassEvent], None] | Callable[[MassEvent], Coroutine[Any, Any, None]]
EventSubscriptionType = tuple[
    EventCallBackType, tuple[EventType, ...] | None, tuple[str, ...] | None
]

LOGGER = logging.getLogger(MASS_LOGGER_NAME)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROVIDERS_PATH = os.path.join(BASE_DIR, "providers")

_R = TypeVar("_R")
_ProviderT = TypeVar("_ProviderT", bound=ProviderInstanceType)


def is_music_provider(provider: ProviderInstanceType) -> TypeGuard[MusicProvider]:
    """Type guard that returns true if a provider is a music provider."""
    return provider.type == ProviderType.MUSIC


def is_player_provider(provider: ProviderInstanceType) -> TypeGuard[PlayerProvider]:
    """Type guard that returns true if a provider is a player provider."""
    return provider.type == ProviderType.PLAYER


class MusicAssistant:
    """Main MusicAssistant (Server) object."""

    loop: asyncio.AbstractEventLoop
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

    def __init__(self, storage_path: str, cache_path: str, safe_mode: bool = False) -> None:
        """Initialize the MusicAssistant Server."""
        self.storage_path = storage_path
        self.cache_path = cache_path
        self.safe_mode = safe_mode
        # we dynamically register command handlers which can be consumed by the apis
        self.command_handlers: dict[str, APICommandHandler] = {}
        self._subscribers: set[EventSubscriptionType] = set()
        self._provider_manifests: dict[str, ProviderManifest] = {}
        self._providers: dict[str, ProviderInstanceType] = {}
        self._tracked_tasks: dict[str, asyncio.Task[Any]] = {}
        self._tracked_timers: dict[str, asyncio.TimerHandle] = {}
        self.closing = False
        self.running_as_hass_addon: bool = False
        self.version: str = "0.0.0"
        self.dev_mode = (
            os.environ.get("PYTHONDEVMODE") == "1"
            or pathlib.Path(__file__).parent.resolve().parent.resolve().joinpath(".venv").exists()
        )
        self._http_session: ClientSession | None = None
        self._http_session_no_ssl: ClientSession | None = None

    async def start(self) -> None:
        """Start running the Music Assistant server."""
        self.loop = asyncio.get_running_loop()
        self.loop_thread_id = getattr(self.loop, "_thread_id")  # noqa: B009
        self.running_as_hass_addon = await is_hass_supervisor()
        self.version = await get_package_version("music_assistant") or "0.0.0"
        # create shared zeroconf instance
        # TODO: enumerate interfaces and enable IPv6 support
        self.aiozc = AsyncZeroconf(ip_version=IPVersion.V4Only, interfaces=InterfaceChoice.Default)
        # load all available providers from manifest files
        await self.__load_provider_manifests()
        # setup config controller first and fetch important config values
        self.config = ConfigController(self)
        await self.config.setup()
        # setup/migrate storage
        await self._setup_storage()
        LOGGER.info(
            "Starting Music Assistant Server (%s) version %s - HA add-on: %s - Safe mode: %s",
            self.server_id,
            self.version,
            self.running_as_hass_addon,
            self.safe_mode,
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
        # load streams controller early so we can abort if we can't load it
        await self.streams.setup(await self.config.get_core_config("streams"))
        await self.music.setup(await self.config.get_core_config("music"))
        await self.metadata.setup(await self.config.get_core_config("metadata"))
        await self.players.setup(await self.config.get_core_config("players"))
        await self.player_queues.setup(await self.config.get_core_config("player_queues"))
        # load webserver/api last so the api/frontend is
        # not yet available while we're starting (or performing migrations)
        self._register_api_commands()
        await self.webserver.setup(await self.config.get_core_config("webserver"))
        # setup discovery
        await self._setup_discovery()
        # load providers
        if not self.safe_mode:
            await self._load_providers()

    async def stop(self) -> None:
        """Stop running the music assistant server."""
        LOGGER.info("Stop called, cleaning up...")
        self.signal_event(EventType.SHUTDOWN)
        self.closing = True
        # cancel all running tasks
        for task in self._tracked_tasks.values():
            task.cancel()
        # cleanup all providers
        await asyncio.gather(
            *[self.unload_provider(prov_id) for prov_id in list(self._providers.keys())],
            return_exceptions=True,
        )
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
        if self._http_session:
            self._http_session.detach()
            if self._http_session.connector:
                await self._http_session.connector.close()
        if self._http_session_no_ssl:
            self._http_session_no_ssl.detach()
            if self._http_session_no_ssl.connector:
                await self._http_session_no_ssl.connector.close()

    @property
    def server_id(self) -> str:
        """Return unique ID of this server."""
        if not self.config.initialized:
            return ""
        return self.config.get(CONF_SERVER_ID)  # type: ignore[no-any-return]

    @property
    def http_session(self) -> ClientSession:
        """
        Return the shared HTTP Client session (with SSL).

        NOTE: May only be called from the event loop.
        """
        if self._http_session is None:
            self._http_session = create_clientsession(self, verify_ssl=True)
        return self._http_session

    @property
    def http_session_no_ssl(self) -> ClientSession:
        """
        Return the shared HTTP Client session (without SSL).

        NOTE: May only be called from the event loop thread.
        """
        if self._http_session_no_ssl is None:
            self._http_session_no_ssl = create_clientsession(self, verify_ssl=False)
        return self._http_session_no_ssl

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
            onboard_done=self.config.onboard_done,
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
        user = get_current_user()
        user_provider_filter = user.provider_filter if user else None

        return [
            x
            for x in self._providers.values()
            if (provider_type is None or provider_type == x.type)
            # handle optional user (music) provider filter
            and (
                not user_provider_filter
                or x.instance_id in user_provider_filter
                or x.type != ProviderType.MUSIC
            )
        ]

    @api_command("logging/get")
    async def get_application_log(self) -> str:
        """Return the application log from file."""
        logfile = os.path.join(self.storage_path, "musicassistant.log")
        async with aiofiles.open(logfile) as _file:
            return str(await _file.read())

    @property
    def providers(self) -> list[ProviderInstanceType]:
        """Return all loaded/running Providers (instances)."""
        return list(self._providers.values())

    @overload
    def get_provider(
        self,
        provider_instance_or_domain: str,
        return_unavailable: bool = False,
        provider_type: None = None,
    ) -> ProviderInstanceType | None: ...

    @overload
    def get_provider(
        self,
        provider_instance_or_domain: str,
        return_unavailable: bool = False,
        *,
        provider_type: type[_ProviderT],
    ) -> _ProviderT | None: ...

    def get_provider(
        self,
        provider_instance_or_domain: str,
        return_unavailable: bool = False,
        provider_type: type[_ProviderT] | None = None,
    ) -> ProviderInstanceType | _ProviderT | None:
        """Return provider by instance id or domain.

        :param provider_instance_or_domain: Instance ID or domain of the provider.
        :param return_unavailable: Also return unavailable providers.
        :param provider_type: Optional type hint for the expected provider type (unused at runtime).
        """
        # lookup by instance_id first
        if prov := self._providers.get(provider_instance_or_domain):
            if return_unavailable or prov.available:
                return prov
            if not getattr(prov, "is_streaming_provider", None):
                # no need to lookup other instances because this provider has unique data
                return None
            provider_instance_or_domain = prov.domain
        # fallback to match on domain
        for prov in self._providers.values():
            if prov.domain != provider_instance_or_domain:
                continue
            if return_unavailable or prov.available:
                return prov
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

        self.verify_event_loop_thread("signal_event")

        if LOGGER.isEnabledFor(VERBOSE_LOG_LEVEL):
            # do not log queue time updated events because that is too chatty
            LOGGER.getChild("event").log(VERBOSE_LOG_LEVEL, "%s %s", event.value, object_id or "")

        event_obj = MassEvent(event=event, object_id=object_id, data=data)
        for cb_func, event_filter, id_filter in self._subscribers:
            if not (event_filter is None or event in event_filter):
                continue
            if not (id_filter is None or object_id in id_filter):
                continue
            if asyncio.iscoroutinefunction(cb_func):
                if TYPE_CHECKING:
                    cb_func = cast("Callable[[MassEvent], Coroutine[Any, Any, None]]", cb_func)
                self.create_task(cb_func, event_obj)
            else:
                if TYPE_CHECKING:
                    cb_func = cast("Callable[[MassEvent], None]", cb_func)
                self.loop.call_soon_threadsafe(cb_func, event_obj)

    def subscribe(
        self,
        cb_func: EventCallBackType,
        event_filter: EventType | tuple[EventType, ...] | None = None,
        id_filter: str | tuple[str, ...] | None = None,
    ) -> Callable[[], None]:
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
        target: Callable[..., Coroutine[Any, Any, _R]] | Awaitable[_R],
        *args: Any,
        task_id: str | None = None,
        abort_existing: bool = False,
        **kwargs: Any,
    ) -> asyncio.Task[_R]:
        """Create Task on (main) event loop from Coroutine(function).

        Tasks created by this helper will be properly cancelled on stop.
        """
        if task_id and (existing := self._tracked_tasks.get(task_id)) and not existing.done():
            # prevent duplicate tasks if task_id is given and already present
            if abort_existing:
                existing.cancel()
            else:
                return existing
        self.verify_event_loop_thread("create_task")

        if asyncio.iscoroutinefunction(target):
            # coroutine function
            task = self.loop.create_task(target(*args, **kwargs))
        elif asyncio.iscoroutine(target):
            # coroutine
            task = self.loop.create_task(target)
        elif callable(target):
            raise RuntimeError("Function is not a coroutine or coroutine function")
        else:
            raise RuntimeError("Target is missing")

        if task_id is None:
            task_id = uuid4().hex

        def task_done_callback(_task: asyncio.Task[Any]) -> None:
            self._tracked_tasks.pop(task_id, None)
            # log unhandled exceptions
            if (
                LOGGER.isEnabledFor(logging.DEBUG)
                and not _task.cancelled()
                and (err := _task.exception())
            ):
                task_name = _task.get_name() if hasattr(_task, "get_name") else str(_task)
                LOGGER.warning(
                    "Exception in task %s - target: %s: %s",
                    task_name,
                    str(target),
                    str(err),
                    exc_info=err if LOGGER.isEnabledFor(logging.DEBUG) else None,
                )

        self._tracked_tasks[task_id] = task
        task.add_done_callback(task_done_callback)
        return task

    def call_later(
        self,
        delay: float,
        target: Coroutine[Any, Any, _R] | Awaitable[_R] | Callable[..., _R],
        *args: Any,
        task_id: str | None = None,
        **kwargs: Any,
    ) -> asyncio.TimerHandle:
        """
        Run callable/awaitable after given delay.

        Use task_id for debouncing.
        """
        self.verify_event_loop_thread("call_later")

        if not task_id:
            task_id = uuid4().hex

        if existing := self._tracked_timers.get(task_id):
            existing.cancel()

        def _create_task(_target: Coroutine[Any, Any, _R]) -> None:
            self._tracked_timers.pop(task_id)
            self.create_task(_target, *args, task_id=task_id, abort_existing=True, **kwargs)

        if asyncio.iscoroutinefunction(target) or asyncio.iscoroutine(target):
            # coroutine function
            if TYPE_CHECKING:
                target = cast("Coroutine[Any, Any, _R]", target)
            handle = self.loop.call_later(delay, _create_task, target)
        else:
            # regular callable
            if TYPE_CHECKING:
                target = cast("Callable[..., _R]", target)
            handle = self.loop.call_later(delay, target, *args)
        self._tracked_timers[task_id] = handle
        return handle

    def get_task(self, task_id: str) -> asyncio.Task[Any] | None:
        """Get existing scheduled task."""
        if existing := self._tracked_tasks.get(task_id):
            # prevent duplicate tasks if task_id is given and already present
            return existing
        return None

    def cancel_task(self, task_id: str) -> None:
        """Cancel existing scheduled task."""
        if existing := self._tracked_tasks.pop(task_id, None):
            existing.cancel()

    def cancel_timer(self, task_id: str) -> None:
        """Cancel existing scheduled timer."""
        if existing := self._tracked_timers.pop(task_id, None):
            existing.cancel()

    def register_api_command(
        self,
        command: str,
        handler: Callable[..., Coroutine[Any, Any, Any] | AsyncGenerator[Any, Any]],
        authenticated: bool = True,
        required_role: str | None = None,
        alias: bool = False,
    ) -> Callable[[], None]:
        """Dynamically register a command on the API.

        :param command: The command name/path.
        :param handler: The function to handle the command.
        :param authenticated: Whether authentication is required (default: True).
        :param required_role: Required user role ("admin" or "user")
            None means any authenticated user.
        :param alias: Whether this is an alias for backward compatibility (default: False).
            Aliases are not shown in API documentation but remain functional.

        Returns handle to unregister.
        """
        if command in self.command_handlers:
            msg = f"Command {command} is already registered"
            raise RuntimeError(msg)
        self.command_handlers[command] = APICommandHandler.parse(
            command, handler, authenticated, required_role, alias
        )

        def unregister() -> None:
            self.command_handlers.pop(command, None)

        return unregister

    async def load_provider_config(
        self,
        prov_conf: ProviderConfig,
    ) -> None:
        """Try to load a provider and catch errors."""
        # cancel existing (re)load timer if needed
        task_id = f"load_provider_{prov_conf.instance_id}"
        if existing := self._tracked_timers.pop(task_id, None):
            existing.cancel()

        await self._load_provider(prov_conf)

        # (re)load any dependants
        prov_configs = await self.config.get_provider_configs(include_values=True)
        for dep_prov_conf in prov_configs:
            if not dep_prov_conf.enabled:
                continue
            manifest = self.get_provider_manifest(dep_prov_conf.domain)
            if not manifest.depends_on:
                continue
            if manifest.depends_on == prov_conf.domain:
                await self._load_provider(dep_prov_conf)

    async def load_provider(
        self,
        instance_id: str,
        allow_retry: bool = False,
    ) -> None:
        """Try to load a provider and catch errors."""
        try:
            prov_conf = await self.config.get_provider_config(instance_id)
        except KeyError:
            # Was deleted before we could run
            return

        if not prov_conf.enabled:
            # Was disabled before we could run
            return

        # cancel existing (re)load timer if needed
        task_id = f"load_provider_{instance_id}"
        if existing := self._tracked_timers.pop(task_id, None):
            existing.cancel()

        try:
            await self.load_provider_config(prov_conf)
        except Exception as exc:
            # if loading failed, we store the error in the config object
            # so we can show something useful to the user
            prov_conf.last_error = str(exc)
            self.config.set(f"{CONF_PROVIDERS}/{instance_id}/last_error", str(exc))

            # auto schedule a retry if the (re)load failed (handled exceptions only)
            if isinstance(exc, MusicAssistantError) and allow_retry:
                self.call_later(
                    120,
                    self.load_provider,
                    instance_id,
                    allow_retry,
                    task_id=task_id,
                )
                LOGGER.warning(
                    "Error loading provider(instance) %s: %s (will be retried later)",
                    prov_conf.name or prov_conf.instance_id,
                    str(exc) or exc.__class__.__name__,
                    # log full stack trace if verbose logging is enabled
                    exc_info=exc if LOGGER.isEnabledFor(VERBOSE_LOG_LEVEL) else None,
                )
                return
            # raise in all other situations
            raise

        # (re)load any dependents if needed
        for dep_prov in self.providers:
            if dep_prov.available:
                continue
            if dep_prov.manifest.depends_on == prov_conf.domain:
                await self.unload_provider(dep_prov.instance_id)

    async def unload_provider(self, instance_id: str, is_removed: bool = False) -> None:
        """Unload a provider."""
        self.music.unschedule_provider_sync(instance_id)
        if provider := self._providers.get(instance_id):
            # remove mdns discovery if needed
            if provider.manifest.mdns_discovery:
                for mdns_type in provider.manifest.mdns_discovery:
                    self._aiobrowser.types.discard(mdns_type)
            if isinstance(provider, PlayerProvider):
                await self.players.on_provider_unload(provider)
            if isinstance(provider, MusicProvider):
                await self.music.on_provider_unload(provider)
            # check if there are no other providers dependent of this provider
            for dep_prov in self.providers:
                if dep_prov.manifest.depends_on == provider.domain:
                    await self.unload_provider(dep_prov.instance_id)
            if is_player_provider(provider):
                # unregister all players of this provider
                for player in provider.players:
                    await self.players.unregister(player.player_id, permanent=is_removed)
            try:
                await provider.unload(is_removed)
            except Exception as err:
                LOGGER.warning(
                    "Error while unloading provider %s: %s", provider.name, str(err), exc_info=err
                )
            finally:
                self._providers.pop(instance_id, None)
                await self._update_available_providers_cache()
                self.signal_event(EventType.PROVIDERS_UPDATED, data=self.get_providers())

    async def unload_provider_with_error(self, instance_id: str, error: str) -> None:
        """Unload a provider when it got into trouble which needs user interaction."""
        self.config.set(f"{CONF_PROVIDERS}/{instance_id}/last_error", error)
        await self.unload_provider(instance_id)

    def verify_event_loop_thread(self, what: str) -> None:
        """Report and raise if we are not running in the event loop thread."""
        if self.loop_thread_id != threading.get_ident():
            raise RuntimeError(
                f"Non-Async operation detected: {what} may only be called from the eventloop."
            )

    def _register_api_commands(self) -> None:
        """Register all methods decorated as api_command within a class(instance)."""
        for cls in (
            self,
            self.config,
            self.metadata,
            self.music,
            self.players,
            self.player_queues,
            self.webserver,
            self.webserver.auth,
        ):
            for attr_name in dir(cls):
                if attr_name.startswith("__"):
                    continue
                try:
                    obj = getattr(cls, attr_name)
                except (AttributeError, RuntimeError):
                    # Skip properties that fail during initialization
                    continue
                if hasattr(obj, "api_cmd"):
                    # method is decorated with our api decorator
                    authenticated = getattr(obj, "api_authenticated", True)
                    required_role = getattr(obj, "api_required_role", None)
                    self.register_api_command(obj.api_cmd, obj, authenticated, required_role)

    async def _load_providers(self) -> None:
        """Load providers from config."""
        # create default config for any 'builtin' providers (e.g. URL provider)
        for prov_manifest in self._provider_manifests.values():
            if prov_manifest.type == ProviderType.CORE:
                # core controllers are not real providers
                continue
            if not prov_manifest.builtin:
                continue
            await self.config.create_builtin_provider_config(prov_manifest.domain)

        # load all configured (and enabled) providers
        prov_configs = await self.config.get_provider_configs(include_values=True)
        for prov_conf in prov_configs:
            if not prov_conf.enabled:
                continue
            # Use a task so we can load multiple providers at once.
            # If a provider fails, that will not block the loading of other providers.
            self.create_task(self.load_provider(prov_conf.instance_id, allow_retry=True))

    async def _load_provider(self, conf: ProviderConfig) -> None:
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
        if existing and prov_manifest and not prov_manifest.multi_instance:
            msg = f"Provider {domain} already loaded and only one instance allowed."
            raise SetupFailedError(msg)
        # check valid manifest (just in case)
        if not prov_manifest:
            msg = f"Provider {domain} manifest not found"
            raise SetupFailedError(msg)

        # handle dependency on other provider
        if prov_manifest.depends_on and not self.get_provider(prov_manifest.depends_on):
            # we can safely ignore this completely as the setup will be retried later
            # automatically when the dependency is loaded
            return

        # try to setup the module
        prov_mod = await load_provider_module(domain, prov_manifest.requirements)
        try:
            async with asyncio.timeout(30):
                provider = await prov_mod.setup(self, prov_manifest, conf)
        except TimeoutError as err:
            msg = f"Provider {domain} did not load within 30 seconds"
            raise SetupFailedError(msg) from err

        # run async setup
        await provider.handle_async_init()

        # if we reach this point, the provider loaded successfully
        self._providers[provider.instance_id] = provider
        LOGGER.info(
            "Loaded %s provider %s",
            provider.type.value,
            provider.name,
        )
        provider.available = True

        self.create_task(provider.loaded_in_mass())
        self.config.set(f"{CONF_PROVIDERS}/{conf.instance_id}/last_error", None)
        self.signal_event(EventType.PROVIDERS_UPDATED, data=self.get_providers())
        await self._update_available_providers_cache()
        if isinstance(provider, MusicProvider):
            await self.music.on_provider_loaded(provider)
        if isinstance(provider, PlayerProvider):
            await self.players.on_provider_loaded(provider)

    async def __load_provider_manifests(self) -> None:
        """Preload all available provider manifest files."""

        async def load_provider_manifest(provider_domain: str, provider_path: str) -> None:
            """Preload all available provider manifest files."""
            # get files in subdirectory
            for file_str in await asyncio.to_thread(os.listdir, provider_path):  # noqa: PTH208, RUF100
                file_path = os.path.join(provider_path, file_str)
                if not await isfile(file_path):
                    continue
                if file_str != "manifest.json":
                    continue
                try:
                    provider_manifest: ProviderManifest = await ProviderManifest.parse(file_path)
                    # check for icon.svg file
                    if not provider_manifest.icon_svg:
                        icon_path = os.path.join(provider_path, "icon.svg")
                        if await isfile(icon_path):
                            provider_manifest.icon_svg = await get_icon_string(icon_path)
                    # check for dark_icon file
                    if not provider_manifest.icon_svg_dark:
                        icon_path = os.path.join(provider_path, "icon_dark.svg")
                        if await isfile(icon_path):
                            provider_manifest.icon_svg_dark = await get_icon_string(icon_path)
                    # check for icon_monochrome file
                    if not provider_manifest.icon_svg_monochrome:
                        icon_path = os.path.join(provider_path, "icon_monochrome.svg")
                        if await isfile(icon_path):
                            provider_manifest.icon_svg_monochrome = await get_icon_string(icon_path)
                    # override Home Assistant provider if we're running as add-on
                    if provider_manifest.domain == "hass" and self.running_as_hass_addon:
                        provider_manifest.builtin = True
                        provider_manifest.allow_disable = False

                    self._provider_manifests[provider_manifest.domain] = provider_manifest
                    LOGGER.debug("Loaded manifest for provider %s", provider_manifest.name)
                except Exception as exc:
                    LOGGER.exception(
                        "Error while loading manifest for provider %s",
                        provider_domain,
                        exc_info=exc,
                    )

        async with TaskManager(self) as tg:
            for dir_str in await asyncio.to_thread(os.listdir, PROVIDERS_PATH):  # noqa: PTH208, RUF100
                if dir_str.startswith("."):
                    # skip hidden directories
                    continue
                dir_path = os.path.join(PROVIDERS_PATH, dir_str)
                if dir_str.startswith("_") and not self.dev_mode:
                    # only load demo/test providers if debug mode is enabled (e.g. for development)
                    continue
                if not await isdir(dir_path):
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
        zeroconf: Zeroconf,
        service_type: str,
        name: str,
        state_change: ServiceStateChange,
    ) -> None:
        """Handle MDNS service state callback."""

        async def process_mdns_state_change(prov: ProviderInstanceType) -> None:
            if state_change == ServiceStateChange.Removed:
                info = None
            else:
                info = AsyncServiceInfo(service_type, name)
                await info.async_request(zeroconf, 3000)
            await prov.on_mdns_service_state_change(name, state_change, info)

        LOGGER.log(
            VERBOSE_LOG_LEVEL,
            "Service %s of type %s state changed: %s",
            name,
            service_type,
            state_change,
        )
        for prov in self._providers.values():
            if not prov.manifest.mdns_discovery:
                continue
            if not prov.available:
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
        return None

    async def _update_available_providers_cache(self) -> None:
        """Update the global cache variable of loaded/available providers."""
        await set_global_cache_values(
            {
                "provider_domains": {x.domain for x in self.providers},
                "provider_instance_ids": {x.instance_id for x in self.providers},
                "available_providers": {
                    *{x.domain for x in self.providers},
                    *{x.instance_id for x in self.providers},
                },
                "unique_providers": self.music.get_unique_providers(),
                "streaming_providers": {
                    x.domain
                    for x in self.providers
                    if is_music_provider(x) and x.is_streaming_provider
                },
                "non_streaming_providers": {
                    x.instance_id
                    for x in self.providers
                    if not (is_music_provider(x) and x.is_streaming_provider)
                },
            }
        )

    async def _setup_storage(self) -> None:
        """Handle Setup of storage/cache folder(s)."""
        if not await isdir(self.storage_path):
            await mkdirs(self.storage_path)
        if not await isdir(self.cache_path):
            await mkdirs(self.cache_path)
