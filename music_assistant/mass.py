"""Main Music Assistant class."""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import pathlib
import threading
import time
from base64 import b64encode
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Coroutine
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self, TypeGuard, TypeVar, cast, overload
from uuid import uuid4

import aiofiles
from aiofiles.os import wrap
from music_assistant_models.api import ServerInfoMessage
from music_assistant_models.auth import Scope
from music_assistant_models.config_entries import ProviderError
from music_assistant_models.enums import (
    CoreState,
    EventType,
    ProviderFeature,
    ProviderIconVariant,
    ProviderType,
)
from music_assistant_models.errors import (
    AuthenticationFailed,
    AuthenticationRequired,
    InvalidToken,
    LoginFailed,
    MusicAssistantError,
    SetupFailedError,
    UnsupportedSystemError,
)
from music_assistant_models.event import MassEvent
from music_assistant_models.helpers import set_global_cache_values
from music_assistant_models.provider import ProviderManifest

from music_assistant.constants import (
    API_SCHEMA_VERSION,
    CONF_DEFAULT_PROVIDERS_SETUP,
    CONF_PROVIDERS,
    CONF_SERVER_ID,
    CONFIGURABLE_CORE_CONTROLLERS,
    DEFAULT_PROVIDERS,
    MASS_LOGGER_NAME,
    MIN_SCHEMA_VERSION,
    VERBOSE_LOG_LEVEL,
)
from music_assistant.controllers.cache import CacheController
from music_assistant.controllers.config import ConfigController
from music_assistant.controllers.dashboard import DashboardController
from music_assistant.controllers.diagnostics import DiagnosticsController
from music_assistant.controllers.discovery import DiscoveryController
from music_assistant.controllers.metadata import MetaDataController
from music_assistant.controllers.music import MusicController
from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.players import PlayerController
from music_assistant.controllers.streams import StreamsController
from music_assistant.controllers.tasks import TasksController
from music_assistant.controllers.translations import TranslationController
from music_assistant.controllers.webserver import WebserverController
from music_assistant.controllers.webserver.helpers.auth_middleware import (
    get_current_user,
    has_scope,
)
from music_assistant.helpers.aiohttp_client import create_clientsession
from music_assistant.helpers.api import APICommandHandler, api_command
from music_assistant.helpers.diagnostics import install_diagnostics_log_handler
from music_assistant.helpers.images import detect_provider_icons
from music_assistant.helpers.util import (
    TaskManager,
    get_package_version,
    is_hass_supervisor,
    load_provider_module,
    warn_if_missing_x86_64_v2,
)
from music_assistant.models import ProviderInstanceType
from music_assistant.models.audio_analysis_provider import AudioAnalysisProvider
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
    EventCallBackType, tuple[EventType, ...] | None, tuple[str, ...] | None, bool
]

LOGGER = logging.getLogger(MASS_LOGGER_NAME)

BASE_DIR = str(Path(__file__).resolve().parent)
PROVIDERS_PATH = os.path.join(BASE_DIR, "providers")
# These bounds guard against a wedged provider, they are not a performance budget: several
# providers load at once on a busy event loop, so a step can take much longer in wall clock
# time than it takes on its own. Keep them generous enough that a slow host never trips them.
PROVIDER_SETUP_TIMEOUT = 120
# Generous enough for the slowest hosts to load their ML models, but bounded so a wedged
# provider fails to load instead of holding up startup forever.
PROVIDER_ASYNC_INIT_TIMEOUT = 300
PROVIDER_LOAD_CONCURRENCY = 8
# Provider teardown may involve third-party network clients or subprocesses. Keep shutdown
# bounded without changing the timeout behavior of normal provider reloads and removals.
PROVIDER_SHUTDOWN_TIMEOUT = 30

_R = TypeVar("_R")
_ProviderT = TypeVar("_ProviderT", bound=ProviderInstanceType)


def is_music_provider(provider: ProviderInstanceType) -> TypeGuard[MusicProvider]:
    """Type guard that returns true if a provider is a music provider."""
    return provider.type == ProviderType.MUSIC


def is_player_provider(provider: ProviderInstanceType) -> TypeGuard[PlayerProvider]:
    """Type guard that returns true if a provider is a player provider."""
    return provider.type == ProviderType.PLAYER


def is_audio_analysis_provider(
    provider: ProviderInstanceType,
) -> TypeGuard[AudioAnalysisProvider]:
    """Type guard that returns true if a provider is an audio analysis provider."""
    return provider.type == ProviderType.AUDIO_ANALYSIS


def _provider_error_from_exc(exc: BaseException) -> ProviderError:
    """Build a serializable, localizable ProviderError from a provider setup exception."""
    message = str(exc) or type(exc).__name__
    if isinstance(exc, MusicAssistantError):
        return ProviderError(
            error_code=exc.error_code,
            message=message,
            translation_key=exc.translation_key,
            translation_args=list(exc.translation_args),
            translation_owner=exc.translation_owner,
        )
    return ProviderError(error_code=999, message=message)


def _provider_error_traceback(exc: BaseException) -> BaseException | None:
    """Return the exception to log a traceback for, or None when its message says enough."""
    # a handled condition (auth required, unsupported system, ...) explains itself, but anything
    # unexpected - or a setup failure wrapping an underlying error - can only be diagnosed from a
    # traceback, and by the time it is reported the user rarely still has verbose logging on
    if not isinstance(exc, MusicAssistantError) or exc.__cause__ is not None:
        return exc
    return exc if LOGGER.isEnabledFor(VERBOSE_LOG_LEVEL) else None


@asynccontextmanager
async def _provider_load_step(
    domain: str, action: str, timeout: int | None = None
) -> AsyncIterator[None]:
    """
    Name a provider load step, so any failure in it surfaces as a usable setup failure.

    :param domain: Domain of the provider being loaded, used in the error message.
    :param action: Verb describing the step, used in the error message.
    :param timeout: Seconds to allow the step before it is treated as failed, if bounded.
    """
    timeout_cm: asyncio.Timeout | None = None
    try:
        if timeout is None:
            yield
        else:
            async with asyncio.timeout(timeout) as timeout_cm:
                yield
    except TimeoutError as err:
        if timeout_cm is not None and timeout_cm.expired():
            msg = f"Provider {domain} did not {action} within {timeout} seconds"
        else:
            # a timeout from the provider's own code (an http call, say) carries no message
            # of its own: name the step it happened in instead of blaming our own bound
            msg = f"Provider {domain} timed out while trying to {action}"
        raise SetupFailedError(msg) from err
    except MusicAssistantError:
        # already carries a message (and a translation key) meant for the user
        raise
    except Exception as err:
        if str(err):
            raise
        # an exception without a message (a bare TimeoutError from an http call, say) would
        # otherwise reach the user as nothing but its class name, with no hint of what failed
        msg = f"Provider {domain} failed to {action}: {type(err).__name__}"
        raise SetupFailedError(msg) from err


class MusicAssistant:
    """Main MusicAssistant (Server) object."""

    loop: asyncio.AbstractEventLoop
    config: ConfigController
    webserver: WebserverController
    cache: CacheController
    metadata: MetaDataController
    tasks: TasksController
    music: MusicController
    players: PlayerController
    player_queues: PlayerQueuesController
    discovery: DiscoveryController
    streams: StreamsController
    translations: TranslationController
    diagnostics: DiagnosticsController
    dashboard: DashboardController

    def __init__(self, storage_path: str, cache_path: str, safe_mode: bool = False) -> None:
        """Initialize the MusicAssistant Server."""
        self._state = CoreState.STARTING
        self.storage_path = storage_path
        self.cache_path = cache_path
        # Sqlite spills temp files (sort scratch, the VACUUM rebuild copy) to /tmp by
        # default, which is a RAM-backed tmpfs on HAOS - redirect to the data volume.
        os.environ.setdefault("SQLITE_TMPDIR", storage_path)
        self.safe_mode = safe_mode
        # we dynamically register command handlers which can be consumed by the apis
        self.command_handlers: dict[str, APICommandHandler] = {}
        self._subscribers: set[EventSubscriptionType] = set()
        self._provider_manifests: dict[str, ProviderManifest] = {}
        self._provider_icons: dict[str, dict[ProviderIconVariant, tuple[str, bytes]]] = {}
        self._providers: dict[str, ProviderInstanceType] = {}
        self._tracked_tasks: dict[str, asyncio.Task[Any]] = {}
        self._tracked_timers: dict[str, asyncio.TimerHandle] = {}
        self._provider_ready_events: dict[str, asyncio.Event] = {}
        self.running_as_hass_addon: bool = False
        self.version: str = "0.0.0"
        self.logger = LOGGER
        self.dev_mode = (
            os.environ.get("PYTHONDEVMODE") == "1"
            or pathlib.Path(__file__).parent.resolve().parent.resolve().joinpath(".venv").exists()
        )
        self._http_session: ClientSession | None = None
        self._http_session_no_ssl: ClientSession | None = None

    async def start(self) -> None:
        """Start running the Music Assistant server."""
        self.loop = asyncio.get_running_loop()
        # start() runs on the event loop thread, so this is the loop's thread id.
        self.loop_thread_id = threading.get_ident()
        # ensure the always-on diagnostics capture handler is installed as early as
        # possible so boot-time errors are captured (idempotent, also installed by
        # __main__ but embedded usage boots the server directly)
        install_diagnostics_log_handler()
        self.running_as_hass_addon = await is_hass_supervisor()
        self.version = await get_package_version("music_assistant") or "0.0.0"
        # setup config controller first and fetch important config values
        self.config = ConfigController(self)
        await self.config.setup()
        self.discovery = DiscoveryController(self)
        # load all available providers from manifest files
        await self.__load_provider_manifests()
        # setup/migrate storage
        await self._setup_storage()
        LOGGER.info(
            "Starting Music Assistant Server (%s) version %s - HA add-on: %s - Safe mode: %s",
            self.server_id,
            self.version,
            self.running_as_hass_addon,
            self.safe_mode,
        )
        await warn_if_missing_x86_64_v2(LOGGER)
        # setup other core controllers
        await self._load_core_controllers()

        # setup all core controllers in parallel
        async def setup_controller(controller: CoreController) -> None:
            config = await self.config.get_core_config(controller.domain)
            # keep the active config on the controller so internal code can read
            # config values without rebuilding the config entries
            controller.config = config
            await controller.setup(config)
            controller.initialized.set()

        # set up the translations catalog first so it is ready before any object is serialized
        await setup_controller(self.translations)

        async with asyncio.TaskGroup() as tg:
            tg.create_task(setup_controller(self.cache))
            tg.create_task(setup_controller(self.tasks))
            tg.create_task(setup_controller(self.streams))
            tg.create_task(setup_controller(self.music))
            tg.create_task(setup_controller(self.metadata))
            tg.create_task(setup_controller(self.players))
            tg.create_task(setup_controller(self.player_queues))
            tg.create_task(setup_controller(self.diagnostics))
            tg.create_task(setup_controller(self.dashboard))

        for controller_name in (
            "cache",
            "tasks",
            "streams",
            "music",
            "metadata",
            "players",
            "player_queues",
        ):
            await cast("CoreController", getattr(self, controller_name)).post_setup()

        # load webserver/api now that the core controllers are setup and ready to be used
        self._register_api_commands()
        webserver_config = await self.config.get_core_config("webserver")
        self.webserver.config = webserver_config
        await self.webserver.setup(webserver_config)
        await setup_controller(self.discovery)
        # load builtin providers (always needed, also in safe mode)
        await self._load_builtin_providers()
        # load regular providers (skip when in safe mode)
        # providers are loaded in background tasks so they won't block
        # the startup if they fail or take a long time to load
        if not self.safe_mode:
            await self._load_providers()
        # at this point we are fully up and running,
        # set state to running to signal we're ready
        self._set_state(CoreState.RUNNING)

    async def stop(self) -> None:
        """Stop running the music assistant server."""
        LOGGER.info("Stop called, cleaning up...")
        # set state to stopping to signal we're shutting down
        self._set_state(CoreState.STOPPING)
        # stop accepting new API/frontend connections before provider teardown starts
        if webserver := getattr(self, "webserver", None):
            try:
                await webserver.stop_accepting_connections()
            except OSError, RuntimeError:
                LOGGER.exception("Error while stopping the webserver listeners")
        # cancel all running tasks
        for task in list(self._tracked_tasks.values()):
            task.cancel()

        async def unload_provider_bounded(instance_id: str) -> None:
            provider = self._providers.get(instance_id)
            provider_name = provider.name if provider else instance_id
            timeout: asyncio.Timeout | None = None
            try:
                async with asyncio.timeout(PROVIDER_SHUTDOWN_TIMEOUT) as timeout:
                    await self.unload_provider(instance_id)
            except TimeoutError:
                if timeout is None or not timeout.expired():
                    raise
                LOGGER.warning(
                    "Provider %s (%s) did not unload within %s seconds; continuing shutdown",
                    provider_name,
                    instance_id,
                    PROVIDER_SHUTDOWN_TIMEOUT,
                )

        # cleanup all providers concurrently, without allowing one to block shutdown
        await asyncio.gather(
            *[unload_provider_bounded(prov_id) for prov_id in list(self._providers.keys())],
            return_exceptions=True,
        )
        # stop core controllers, cache and config last because the others rely on them.
        # a failed startup may not have created (or fully set up) every controller, so
        # each one is closed independently: leaving a database open here would keep its
        # worker thread alive and stop the process from ever exiting.
        for controller_name in (
            "discovery",
            "streams",
            "webserver",
            "tasks",
            "metadata",
            "music",
            "player_queues",
            "players",
            "translations",
            "diagnostics",
            "dashboard",
            "config",
            "cache",
        ):
            if (controller := getattr(self, controller_name, None)) is None:
                continue
            try:
                await controller.close()
            except Exception:
                LOGGER.exception("Error while closing the %s controller", controller_name)
        # close/cleanup shared http sessions
        if self._http_session and not self._http_session.closed:
            await self._http_session.close()
        if self._http_session_no_ssl and not self._http_session_no_ssl.closed:
            await self._http_session_no_ssl.close()
        self._set_state(CoreState.STOPPED)

    @property
    def state(self) -> CoreState:
        """Return current state of the core."""
        return self._state

    @property
    def closing(self) -> bool:
        """Return true if the server is (in the process of) closing."""
        return self._state in (CoreState.STOPPING, CoreState.STOPPED)

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
            status=self._state,
        )

    @api_command("time", authenticated=False)
    def get_server_time(self) -> float:
        """
        Return the current server time as UTC timestamp (seconds since epoch).

        Clients compare server-provided timestamps (such as `elapsed_time_last_updated`)
        against their own clock. Round-tripping this command lets a client estimate the
        offset between the two clocks and correct for it, so a device with an unsynced
        clock still renders playback progress and countdowns correctly.
        """
        return time.time()

    @api_command("providers/manifests", required_scope=Scope.PROVIDERS_READ)
    def get_provider_manifests(self) -> list[ProviderManifest]:
        """Return all Provider manifests."""
        return list(self._provider_manifests.values())

    @api_command("providers/manifests/get", required_scope=Scope.PROVIDERS_READ)
    def get_provider_manifest(self, instance_id_or_domain: str) -> ProviderManifest:
        """Return Provider manifests of single provider(domain)."""
        if instance_id_or_domain in self._provider_manifests:
            return self._provider_manifests[instance_id_or_domain]
        if provider := self.get_provider(instance_id_or_domain, return_unavailable=True):
            return provider.manifest
        raise KeyError(f"Provider manifest not found for {instance_id_or_domain}")

    @api_command("providers/icon", required_scope=Scope.PROVIDERS_READ)
    def get_provider_icon_data(
        self,
        provider: str,
        variant: ProviderIconVariant = ProviderIconVariant.DEFAULT,
    ) -> str | None:
        """
        Return a provider icon variant as a base64 data URI.

        :param provider: A provider domain or instance id.
        :param variant: Which icon variant to return.
        """
        icon = self.get_provider_icon(provider, variant)
        if icon is None:
            return None
        mime, data = icon
        return f"data:{mime};base64,{b64encode(data).decode('ascii')}"

    def get_provider_icon(
        self,
        provider: str,
        variant: ProviderIconVariant = ProviderIconVariant.DEFAULT,
    ) -> tuple[str, bytes] | None:
        """
        Return the (mime, bytes) for a provider icon variant.

        :param provider: A provider domain or instance id.
        :param variant: Which icon variant to return.
        """
        domain = provider
        if domain not in self._provider_icons:
            try:
                domain = self.get_provider_manifest(provider).domain
            except KeyError:
                return None
        icons = self._provider_icons.get(domain)
        if not icons:
            return None
        return icons.get(variant)

    @api_command("providers", required_scope=Scope.PROVIDERS_READ)
    def get_providers(
        self, provider_type: ProviderType | None = None
    ) -> list[ProviderInstanceType]:
        """
        Return all loaded/running Providers (instances).

        Optionally filtered by ProviderType.
        Note that this applies user filters for music providers (for non admin users).
        """
        user = get_current_user()
        user_provider_filter = (
            user.provider_filter if user and not has_scope(user, Scope.ALL) else None
        )
        return [
            x
            for x in list(self._providers.values())
            if (provider_type is None or provider_type == x.type)
            # apply user provider filter
            and (
                not user_provider_filter
                or x.instance_id in user_provider_filter
                or x.type != ProviderType.MUSIC
            )
        ]

    @api_command("logging/get", required_scope=Scope.SYSTEM_MANAGE)
    async def get_application_log(self) -> str:
        """Return the application log from file."""
        logfile = os.path.join(self.storage_path, "musicassistant.log")
        async with aiofiles.open(logfile) as _file:
            return str(await _file.read())

    @property
    def providers(self) -> list[ProviderInstanceType]:
        """
        Return all loaded/running Providers (instances).

        Note that this skips user filters so may only be called from internal code.
        """
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
        """
        Return provider by instance id or domain.

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
        for prov in list(self._providers.values()):
            if prov.domain != provider_instance_or_domain:
                continue
            if return_unavailable or prov.available:
                return prov
        return None

    def get_provider_ready_event(self, domain: str) -> asyncio.Event:
        """Get (or create) an asyncio.Event that is set when a provider of the given domain is loaded."""
        if domain not in self._provider_ready_events:
            self._provider_ready_events[domain] = asyncio.Event()
        return self._provider_ready_events[domain]

    def get_provider_instances(
        self,
        domain: str,
        return_unavailable: bool = False,
        provider_type: ProviderType | None = None,
    ) -> list[ProviderInstanceType]:
        """
        Return all provider instances for a given domain.

        Note that this skips user filters so may only be called from internal code.
        """
        return [
            prov
            for prov in list(self._providers.values())
            if (provider_type is None or provider_type == prov.type)
            and prov.domain == domain
            and (return_unavailable or prov.available)
        ]

    def get_providers_supporting_feature(
        self,
        feature: ProviderFeature,
        priority: tuple[ProviderType, ...] = (
            ProviderType.MUSIC,
            ProviderType.METADATA,
            ProviderType.PLUGIN,
        ),
    ) -> list[ProviderInstanceType]:
        """
        Return all available providers that support the given feature.

        Results are grouped by provider type in the order given by ``priority``,
        and sorted within each tier by the provider's ``priority`` attribute
        (lower value = higher priority).

        :param feature: The ProviderFeature to query for.
        :param priority: Ordered tuple of ProviderType values indicating tier order.
            Types omitted from this tuple are excluded from the results.
        """
        by_tier: dict[ProviderType, list[ProviderInstanceType]] = {ptype: [] for ptype in priority}
        for prov in self.get_providers():
            if not prov.available:
                continue
            if prov.type not in by_tier:
                continue
            if feature not in prov.supported_features:
                continue
            by_tier[prov.type].append(prov)
        result: list[ProviderInstanceType] = []
        for ptype in priority:
            result.extend(sorted(by_tier[ptype], key=lambda p: getattr(p, "priority", 50)))
        return result

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
        for cb_func, event_filter, id_filter, is_coro in list(self._subscribers):
            if not (event_filter is None or event in event_filter):
                continue
            if not (id_filter is None or object_id in id_filter):
                continue
            if is_coro:
                if TYPE_CHECKING:
                    cb_func = cast("Callable[[MassEvent], Coroutine[Any, Any, None]]", cb_func)
                self.create_task(cb_func, event_obj)
            else:
                if TYPE_CHECKING:
                    cb_func = cast("Callable[[MassEvent], None]", cb_func)
                self.loop.call_soon(cb_func, event_obj)

    def subscribe(
        self,
        cb_func: EventCallBackType,
        event_filter: EventType | tuple[EventType, ...] | None = None,
        id_filter: str | tuple[str, ...] | None = None,
    ) -> Callable[[], None]:
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
        # precompute whether the callback is a coroutine so signal_event does not have to
        # re-derive it via reflection for every subscriber on every (high-frequency) event
        listener = (cb_func, event_filter, id_filter, inspect.iscoroutinefunction(cb_func))
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
        eager_start: bool = True,
        log_exceptions: bool = True,
        **kwargs: Any,
    ) -> asyncio.Task[_R]:
        """
        Create Task on (main) event loop from Coroutine(function).

        Tasks created by this helper will be properly cancelled on stop.

        :param target: Coroutine function or awaitable to run as a task.
        :param args: Arguments to pass to the coroutine function.
        :param task_id: Optional ID to track and deduplicate tasks.
        :param abort_existing: If True, cancel existing task with same task_id.
        :param eager_start: If True (default), start task immediately without waiting
                           for next event loop iteration. This ensures proper ordering
                           when creating multiple tasks in sequence.
        :param log_exceptions: Set to False when the caller awaits the task and reports
                               its failures itself; the task then logs at debug level
                               instead of warning.
        :param kwargs: Keyword arguments to pass to the coroutine function.
        """
        if task_id and (existing := self._tracked_tasks.get(task_id)) and not existing.done():
            # prevent duplicate tasks if task_id is given and already present
            if abort_existing:
                existing.cancel()
            else:
                # close any already-constructed coroutine to avoid "never awaited" warning
                if inspect.iscoroutine(target):
                    target.close()
                return existing
        self.verify_event_loop_thread("create_task")

        if inspect.iscoroutinefunction(target):
            # coroutine function
            coro = target(*args, **kwargs)
        elif inspect.iscoroutine(target):
            # coroutine
            coro = target
        elif callable(target):
            raise RuntimeError("Function is not a coroutine or coroutine function")
        else:
            raise RuntimeError("Target is missing")

        # Use asyncio.Task directly with eager_start for immediate execution
        task: asyncio.Task[_R] = asyncio.Task(coro, loop=self.loop, eager_start=eager_start)

        if task_id is None:
            task_id = uuid4().hex

        def task_done_callback(_task: asyncio.Task[Any]) -> None:
            # done callbacks run one event loop iteration after the task finished, so a
            # caller may already have replaced the entry with a new task under the same
            # task_id - only untrack when the entry still points at this task
            if self._tracked_tasks.get(task_id) is _task:
                del self._tracked_tasks[task_id]
            if _task.cancelled():
                return
            # always retrieve the exception, otherwise asyncio logs a noisy
            # "Task exception was never retrieved" error at garbage collection time
            if err := _task.exception():
                task_name = _task.get_name() if hasattr(_task, "get_name") else str(_task)
                # a failure the waiters report themselves is demoted rather than dropped:
                # work that outlives every waiter (join_task keeps it running) would
                # otherwise fail without a trace anywhere
                LOGGER.log(
                    logging.WARNING if log_exceptions else logging.DEBUG,
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

        def _call_sync(_target: Callable[..., _R]) -> None:
            self._tracked_timers.pop(task_id)
            _target(*args, **kwargs)

        if inspect.iscoroutinefunction(target) or inspect.iscoroutine(target):
            # coroutine function
            if TYPE_CHECKING:
                target = cast("Coroutine[Any, Any, _R]", target)
            handle = self.loop.call_later(delay, _create_task, target)
        else:
            # regular sync callable
            if TYPE_CHECKING:
                target = cast("Callable[..., _R]", target)
            handle = self.loop.call_later(delay, _call_sync, target)
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
        required_scope: Scope | None = None,
        allow_impersonation: bool = False,
        alias: bool = False,
    ) -> Callable[[], None]:
        """
        Dynamically register a command on the API.

        :param command: The command name/path.
        :param handler: The function to handle the command.
        :param authenticated: Whether authentication is required (default: True).
        :param required_scope: Scope required to execute the command,
            None means any authenticated user.
        :param allow_impersonation: Whether the command accepts a 'user' argument
            to execute the command on behalf of another user (default: False).
        :param alias: Whether this is an alias for backward compatibility (default: False).
            Aliases are not shown in API documentation but remain functional.

        Returns handle to unregister.
        """
        if command in self.command_handlers:
            msg = f"Command {command} is already registered"
            raise RuntimeError(msg)
        self.command_handlers[command] = APICommandHandler.parse(
            command, handler, authenticated, required_scope, allow_impersonation, alias
        )

        def unregister() -> None:
            self.command_handlers.pop(command, None)

        return unregister

    async def load_provider_config(
        self,
        prov_conf: ProviderConfig,
    ) -> None:
        """Load (or reload) a provider from its config, recording any load failure."""
        # cancel existing (re)load timer if needed
        task_id = f"load_provider_{prov_conf.instance_id}"
        if existing := self._tracked_timers.pop(task_id, None):
            existing.cancel()

        try:
            await self._load_provider(prov_conf)
        except Exception as exc:
            # persist the failure so the provider surfaces a clear status (e.g. auth_required)
            # to the UI instead of appearing stuck loading, then propagate to the caller
            self.config.update_provider_last_error(
                prov_conf.instance_id, _provider_error_from_exc(exc)
            )
            raise

        # (re)load any dependents. The provider itself is loaded at this point, so a problem
        # in this scan belongs to a dependent (or to nothing at all) and must never be
        # recorded against - and thus flag - the provider we just loaded successfully.
        try:
            # resolving option values here would call get_config_entries() on every loaded
            # provider (some of which do network i/o), for values _load_provider does not
            # read: it seeds the stored raw values itself and rehydrates once the instance
            # exists. Only the manifest-related fields below are needed to spot a dependent.
            prov_configs = await self.config.get_provider_configs()
        except Exception as exc:
            LOGGER.warning(
                "Error looking up dependents of provider(instance) %s: %s",
                prov_conf.name or prov_conf.instance_id,
                str(exc) or exc.__class__.__name__,
                exc_info=_provider_error_traceback(exc),
            )
            return
        for dep_prov_conf in prov_configs:
            if not dep_prov_conf.enabled:
                continue
            manifest = self.get_provider_manifest(dep_prov_conf.domain)
            if not manifest.depends_on:
                continue
            if manifest.depends_on != prov_conf.domain:
                continue
            try:
                # the scan above skipped the config values, but the load path does need them:
                # a provider reads config (e.g. its log level) while it is being constructed.
                # Resolve them here, for this single dependent instead of for every provider.
                dep_conf = await self.config.get_provider_config(dep_prov_conf.instance_id)
            except KeyError:
                # config was removed while we were scanning
                continue
            try:
                await self._load_provider(dep_conf)
            except Exception as exc:
                # record the failure against the provider that hit it: attributing it to the
                # provider we just loaded (which is fine) flags the wrong one in the UI
                self.config.update_provider_last_error(
                    dep_prov_conf.instance_id, _provider_error_from_exc(exc)
                )
                LOGGER.warning(
                    "Error loading provider(instance) %s: %s",
                    dep_prov_conf.name or dep_prov_conf.instance_id,
                    str(exc) or exc.__class__.__name__,
                    exc_info=_provider_error_traceback(exc),
                )

    async def load_provider(
        self,
        instance_id: str,
        allow_retry: bool = False,
        remove_if_unsupported: bool = False,
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
        except UnsupportedSystemError as exc:
            # The host does not meet this provider's hardware requirements. This is a
            # permanent condition, so we never retry. For a provider that was just
            # auto-set-up as a default, drop the config again so it does not linger as a
            # broken provider (it stays marked done so it is not auto-created again).
            if remove_if_unsupported:
                LOGGER.info(
                    "Not enabling default provider %s: %s",
                    prov_conf.name or prov_conf.instance_id,
                    exc,
                )
                # The provider never loaded, so just drop its auto-created config key.
                # (remove_provider_config refuses builtin providers and runs loaded-provider
                # cleanup we don't need here; a direct remove persists and is guard-free.)
                self.config.remove(f"{CONF_PROVIDERS}/{instance_id}")
                return
            prov_conf.last_error = _provider_error_from_exc(exc)
            self.config.update_provider_last_error(instance_id, prov_conf.last_error)
            LOGGER.warning(
                "Provider(instance) %s can not run on this system: %s",
                prov_conf.name or prov_conf.instance_id,
                exc,
            )
            return
        except Exception as exc:
            # if loading failed, we store the error in the config object
            # so we can show something useful to the user
            prov_conf.last_error = _provider_error_from_exc(exc)
            self.config.update_provider_last_error(instance_id, prov_conf.last_error)

            # auto schedule a retry if the (re)load failed with a handled exception
            # unhandled exceptions (e.g. ValueError) are likely bugs that won't resolve themselves
            will_retry = (
                allow_retry
                and isinstance(exc, MusicAssistantError)
                and not isinstance(
                    exc,
                    (AuthenticationRequired, AuthenticationFailed, LoginFailed, InvalidToken),
                )
            )
            if will_retry:
                self.call_later(
                    120,
                    self.load_provider,
                    instance_id,
                    allow_retry,
                    task_id=task_id,
                )
            LOGGER.warning(
                "Error loading provider(instance) %s: %s%s",
                prov_conf.name or prov_conf.instance_id,
                str(exc) or exc.__class__.__name__,
                " (will be retried later)" if will_retry else "",
                exc_info=_provider_error_traceback(exc),
            )
            return

        # (re)load any dependents if needed
        for dep_prov in self.providers:
            if dep_prov.available:
                continue
            if dep_prov.manifest.depends_on == prov_conf.domain:
                await self.unload_provider(dep_prov.instance_id)

    async def unload_provider(self, instance_id: str, is_removed: bool = False) -> None:
        """Unload a provider."""
        # this waits (bounded) for a running sync to unwind: provider.unload() below tears
        # down state the sync may still be using, such as the mount of a network share
        await self.music.unschedule_provider_sync(instance_id, clear_persisted_state=is_removed)
        if provider := self._providers.get(instance_id):
            # mark the provider as on its way out before anything is torn down: the steps
            # below have await points, so without this a callback that is still in flight
            # could register a player back onto a provider that is already gone
            provider.unloading = True
            if isinstance(provider, PlayerProvider):
                await self.players.on_provider_unload(provider)
            if isinstance(provider, MusicProvider):
                await self.music.on_provider_unload(provider)
            # check if there are no other providers dependent of this provider
            for dep_prov in self.providers:
                if dep_prov.manifest.depends_on == provider.domain:
                    await self.unload_provider(dep_prov.instance_id)
            try:
                if is_player_provider(provider):
                    # unregister all players of this provider, straight from the registry: the
                    # provider's own players listing hides disabled and still-initializing
                    # players, which must be unregistered here too so their on_unload runs
                    # and no stale entry is left behind
                    for player in list(self.players):
                        if player.provider.instance_id != instance_id:
                            continue
                        await self.players.unregister(player.player_id, permanent=is_removed)
                await provider.unload(is_removed)
            except Exception as err:
                LOGGER.warning(
                    "Error while unloading provider %s: %s", provider.name, str(err), exc_info=err
                )
            finally:
                if provider.domain in self._provider_ready_events:
                    self._provider_ready_events[provider.domain].clear()
                self._providers.pop(instance_id, None)
                self.discovery.on_provider_unload(instance_id)
                await self._update_available_providers_cache()
                self.signal_event(EventType.PROVIDERS_UPDATED, data=self.get_providers())

    async def unload_provider_with_error(self, instance_id: str, error: str | Exception) -> None:
        """
        Unload a provider that hit a problem which needs user interaction.

        :param error: The originating exception (preferred, so e.g. a LoginFailed surfaces as an
            auth-required status with a localized message) or a plain string for a generic error.
        """
        prov_error = (
            _provider_error_from_exc(error)
            if isinstance(error, Exception)
            else ProviderError(error_code=999, message=error)
        )
        self.config.update_provider_last_error(instance_id, prov_error)
        await self.unload_provider(instance_id)

    async def run_provider_discovery(self, instance_id: str) -> None:
        """
        Run shared discovery for a given provider.

        In case of a PlayerProvider, will also call its own discovery method.
        """
        provider = self.get_provider(instance_id, return_unavailable=False)
        if not provider:
            raise KeyError(f"Provider with instance ID {instance_id} not found")
        await self.discovery.run_provider_discovery(provider)
        if isinstance(provider, PlayerProvider):
            await provider.discover_players()

    def verify_event_loop_thread(self, what: str) -> None:
        """Report and raise if we are not running in the event loop thread."""
        if self.loop_thread_id != threading.get_ident():
            raise RuntimeError(
                f"Non-Async operation detected: {what} may only be called from the eventloop."
            )

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

    def _register_api_commands(self) -> None:
        """Register all methods decorated as api_command within a class(instance)."""
        for cls in (
            self,
            self.config,
            self.metadata,
            self.tasks,
            self.music,
            self.players,
            self.player_queues,
            self.translations,
            self.webserver,
            self.webserver.auth,
            self.streams.audio_analysis,
            self.diagnostics,
            self.dashboard,
        ):
            for attr_name in dir(cls):
                if attr_name.startswith("__"):
                    continue
                # Skip properties to avoid triggering lazy initialization side effects
                # (e.g. http_session creating an aiohttp connector during registration)
                if isinstance(getattr(type(cls), attr_name, None), property):
                    continue
                try:
                    obj = getattr(cls, attr_name)
                except AttributeError, RuntimeError:
                    # Skip attributes that fail during initialization
                    continue
                if hasattr(obj, "api_cmd"):
                    # method is decorated with our api decorator
                    authenticated = getattr(obj, "api_authenticated", True)
                    required_scope = getattr(obj, "api_required_scope", None)
                    allow_impersonation = getattr(obj, "api_allow_impersonation", False)
                    alias = getattr(obj, "api_alias", False)
                    self.register_api_command(
                        obj.api_cmd, obj, authenticated, required_scope, allow_impersonation, alias
                    )

    async def _load_core_controllers(self) -> None:
        """Instantiate the core controllers and register their manifests and icons."""
        self.cache = CacheController(self)
        self.tasks = TasksController(self)
        self.webserver = WebserverController(self)
        self.metadata = MetaDataController(self)
        self.music = MusicController(self)
        self.players = PlayerController(self)
        self.player_queues = PlayerQueuesController(self)
        self.streams = StreamsController(self)
        self.translations = TranslationController(self)
        self.diagnostics = DiagnosticsController(self)
        self.dashboard = DashboardController(self)
        # add manifests for core controllers
        for controller_name in CONFIGURABLE_CORE_CONTROLLERS:
            controller: CoreController = getattr(self, controller_name)
            self._provider_manifests[controller.domain] = controller.manifest
            # load icon image(s) shipped alongside the controller module
            controller_dir = os.path.dirname(inspect.getfile(type(controller)))
            if icons := await detect_provider_icons(controller_dir):
                self._provider_icons[controller.domain] = icons
                controller.manifest.icon_images = list(icons)

    async def _load_builtin_providers(self) -> None:
        """
        Load all builtin providers.

        Builtin providers are always needed (also in safe mode) and are fully awaited.
        On error, setup will fail.
        """
        # create default config for any 'builtin' providers
        for prov_manifest in self._provider_manifests.values():
            if prov_manifest.type == ProviderType.CORE:
                # core controllers are not real providers
                continue
            if not prov_manifest.builtin:
                continue
            await self.config.create_builtin_provider_config(prov_manifest.domain)

        # load all configured (and enabled) builtin providers
        # (only manifest-related fields are read here, so the option values are not resolved)
        prov_configs = await self.config.get_provider_configs()
        builtin_configs: list[ProviderConfig] = [
            prov_conf
            for prov_conf in prov_configs
            if (manifest := self._provider_manifests.get(prov_conf.domain))
            and manifest.builtin
            and (prov_conf.enabled or manifest.allow_disable is False)
        ]

        # load builtin providers and wait for them to complete
        async with asyncio.TaskGroup() as tg:
            for conf in builtin_configs:
                tg.create_task(self.load_provider(conf.instance_id, allow_retry=True))

    async def _load_providers(self) -> None:
        """
        Load regular (non-builtin) providers from config.

        Regular providers are loaded in background tasks
        and can fail without affecting core setup.
        """
        # handle default providers setup
        self.config.set_default(CONF_DEFAULT_PROVIDERS_SETUP, set())
        default_providers_setup = set(self.config.get(CONF_DEFAULT_PROVIDERS_SETUP))
        changes_made = False
        newly_created_defaults: set[str] = set()
        for default_provider, require_mdns in DEFAULT_PROVIDERS:
            if default_provider in default_providers_setup:
                # already processed/setup before, skip
                continue
            if not (manifest := self._provider_manifests.get(default_provider)):
                continue
            if require_mdns:
                # if mdns discovery is required, check if we have seen any mdns entries
                # for this provider before setting it up
                for mdns_name in set(self.discovery.aiozc.zeroconf.cache.cache):
                    if manifest.mdns_discovery and any(
                        mdns_type in mdns_name for mdns_type in manifest.mdns_discovery
                    ):
                        break
                else:
                    continue
            await self.config.create_builtin_provider_config(manifest.domain)
            changes_made = True
            newly_created_defaults.add(manifest.domain)
            # TEMP: migration - to be removed after 2.8 release
            # enable all existing players of the default providers if they are not already enabled
            # due to the linked protocol feature we introduced
            for player_config in await self.config.get_player_configs(
                provider=default_provider, include_disabled=True
            ):
                if player_config.enabled:
                    continue
                await self.config.save_player_config(player_config.player_id, {"enabled": True})
            default_providers_setup.add(default_provider)
        if changes_made:
            self.config.set(CONF_DEFAULT_PROVIDERS_SETUP, default_providers_setup)
            self.config.save(True)
        # load all configured (and enabled) regular (non-builtin) providers
        # (only manifest-related fields are read here, so the option values are not resolved)
        prov_configs = await self.config.get_provider_configs()
        other_configs: list[ProviderConfig] = [
            prov_conf
            for prov_conf in prov_configs
            if prov_conf.enabled
            and (
                not (manifest := self._provider_manifests.get(prov_conf.domain))
                or not manifest.builtin
            )
        ]
        # load providers concurrently via tasks, bounded so a host with many providers does
        # not import every provider module at once (a torch-backed one costs hundreds of MB)
        async with TaskManager(self, PROVIDER_LOAD_CONCURRENCY) as tg:
            for prov_conf in other_configs:
                # Use a task so we can load multiple providers at once.
                # If a provider fails, that will not block the loading of other providers.
                # For providers just auto-set-up as a default, drop the config again if the
                # host does not meet their requirements (rather than retry a broken provider).
                await tg.create_task_with_limit(
                    self.load_provider(
                        prov_conf.instance_id,
                        allow_retry=True,
                        remove_if_unsupported=prov_conf.domain in newly_created_defaults,
                    )
                )

    async def _load_provider(self, conf: ProviderConfig) -> None:
        """Load (or reload) a provider."""
        # if provider is already loaded, stop and unload it first
        await self.unload_provider(conf.instance_id)
        LOGGER.debug("Loading provider %s", conf.name or conf.domain)
        if not conf.enabled:
            msg = "Provider is disabled"
            raise SetupFailedError(msg)

        # The config is validated after the instance is created and its config rehydrated
        # (see below): the full options entries - and thus which values are required - are
        # only known once the instance exists.

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

        # seed the config with its stored raw values so any construction-time option reads
        # in setup()/__init__ see them (the fully-typed entries are only resolvable once the
        # instance exists, and are applied by rehydrate_provider_config just below)
        self.config.seed_stored_config_values(conf)

        # try to setup the module
        # (unbounded: this may still have to install the provider's requirements)
        async with _provider_load_step(domain, "import its module"):
            prov_mod = await load_provider_module(domain, prov_manifest.requirements)
        async with _provider_load_step(domain, "load", PROVIDER_SETUP_TIMEOUT):
            provider = await prov_mod.setup(self, prov_manifest, conf)

        # The instance now exists, so its full (options) config entries can be resolved
        # (get_config_entries is an instance method). Rehydrate the config values from
        # storage against those entries and validate the complete config, before async
        # init so get_config_value reads there see the stored values.
        async with _provider_load_step(domain, "resolve its configuration", PROVIDER_SETUP_TIMEOUT):
            await self.config.rehydrate_provider_config(provider)
        try:
            provider.config.validate()
        except (KeyError, ValueError, AttributeError, TypeError) as err:
            # name the offending entry: the generic message alone gives no clue which
            # value is missing or malformed when a provider refuses to load
            msg = f"Configuration is invalid: {err}"
            raise SetupFailedError(msg) from err

        # run async setup
        async with _provider_load_step(domain, "initialize", PROVIDER_ASYNC_INIT_TIMEOUT):
            await provider.handle_async_init()

        await self._register_loaded_provider(provider, conf)

    async def _register_loaded_provider(
        self, provider: ProviderInstanceType, conf: ProviderConfig
    ) -> None:
        """Register a provider that finished its setup and run its post-load steps."""
        # the instance is now live: register it so the post-load steps below can resolve it
        self._providers[provider.instance_id] = provider
        provider.available = True

        # adapt logging name if needed
        provider._set_log_level_from_config(provider.config)

        try:
            async with _provider_load_step(
                provider.domain, "finish loading", PROVIDER_SETUP_TIMEOUT
            ):
                await self._update_available_providers_cache()
                if isinstance(provider, MusicProvider):
                    await self.music.on_provider_loaded(provider)
                if isinstance(provider, PlayerProvider):
                    await self.players.on_provider_loaded(provider)
        except Exception:
            # a provider that did not finish loading must not stay registered: it would
            # report status LOADED while an error is recorded against it, which leaves the
            # user with a warning they can only find by opening the provider's own settings
            try:
                await self.unload_provider(provider.instance_id)
            except Exception as unload_err:
                # the load failure is the one worth reporting, so keep it as the raised error
                LOGGER.warning(
                    "Error unloading provider %s: %s",
                    provider.name,
                    unload_err,
                    exc_info=unload_err,
                )
            raise

        # if we reach this point, the provider loaded successfully
        LOGGER.info(
            "Loaded %s provider %s",
            provider.type.value,
            provider.name,
        )

        # execute post load actions
        async def _on_provider_loaded() -> None:
            try:
                await provider.loaded_in_mass()
            except Exception as err:
                # the provider stays registered and available either way, so the steps
                # below still run: an event left unset makes every waiter pay the full
                # timeout, on every attempt, until the provider reloads
                LOGGER.warning(
                    "Error in the post load step of provider %s: %s",
                    provider.name,
                    str(err) or err.__class__.__name__,
                    exc_info=err,
                )
            provider.initialized.set()
            self.get_provider_ready_event(provider.domain).set()
            await self.run_provider_discovery(provider.instance_id)
            # push instance name to config (to persist it if it was autogenerated)
            if provider.default_name != conf.default_name:
                self.config.set_provider_default_name(provider.instance_id, provider.default_name)

        self.create_task(_on_provider_loaded())

        # clear any previous error in config and signal update
        self.config.set(f"{CONF_PROVIDERS}/{conf.instance_id}/last_error", None)
        self.signal_event(EventType.PROVIDERS_UPDATED, data=self.get_providers())

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
                    # detect provider icon image variants (svg preferred over png)
                    icons = await detect_provider_icons(provider_path)
                    if icons:
                        self._provider_icons[provider_manifest.domain] = icons
                        provider_manifest.icon_images = list(icons)
                    # detect a setup_flow.py module by its mere presence: importing it
                    # here would trigger installing the provider's requirements
                    provider_manifest.has_setup_flow = await isfile(
                        os.path.join(provider_path, "setup_flow.py")
                    )
                    # override Home Assistant provider if we're running as add-on
                    if provider_manifest.domain == "hass" and self.running_as_hass_addon:
                        provider_manifest.builtin = True
                        provider_manifest.allow_disable = False

                    self._provider_manifests[provider_manifest.domain] = provider_manifest
                    LOGGER.log(
                        VERBOSE_LOG_LEVEL, "Loaded manifest for provider %s", provider_manifest.name
                    )
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
        self.logger.debug("Loaded %s provider manifests", len(self._provider_manifests))

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

    def _set_state(self, new_state: CoreState) -> None:
        """Set new state and signal state change."""
        if self._state == new_state:
            return
        self._state = new_state
        if not hasattr(self, "webserver"):
            # a startup that failed before the core controllers were created has no
            # server info to report and no subscribers to report it to, while the state
            # itself must still change so that shutdown can run to completion
            return
        self.signal_event(EventType.CORE_STATE_UPDATED, data=self.get_server_info())
