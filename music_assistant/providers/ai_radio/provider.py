"""AI Radio Plugin Provider for Music Assistant."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import TYPE_CHECKING, Any

from music_assistant_models.auth import Scope
from music_assistant_models.enums import EventType
from music_assistant_models.errors import InvalidDataError, SetupFailedError

from music_assistant.helpers.plugin_engines import (
    get_tts_engines,
    select_ai_engine,
    select_tts_engine,
)
from music_assistant.models.plugin import PluginProvider

from .constants import (
    CONF_AI_ENGINE,
    CONF_TTS_ENGINE,
    ENGINE_DISCOVERY_TIMEOUT,
    ENGINE_RECHECK_GRACE,
    ENGINE_RETRY_DELAY,
    SUPPORTED_FEATURES,
    TRANSLATION_OWNER,
)
from .hosts import AIRadioHostsMixin
from .media import AIRadioMediaMixin
from .models import DJQueueState
from .queue_dj import AIRadioQueueDJMixin
from .rendering import AIRadioRenderMixin
from .runtime import AIRadioRuntimeMixin
from .storage import AIRadioStorageMixin

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry, ProviderConfig
    from music_assistant_models.event import MassEvent
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

    from .media import _ShowRun


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return AIRadioProvider(mass, manifest, config, SUPPORTED_FEATURES)


class AIRadioProvider(
    AIRadioRuntimeMixin,
    AIRadioRenderMixin,
    AIRadioHostsMixin,
    AIRadioQueueDJMixin,
    AIRadioStorageMixin,
    AIRadioMediaMixin,
    PluginProvider,
):
    """Implementation of the AI Radio plugin provider."""

    def __init__(
        self,
        mass: MusicAssistant,
        manifest: ProviderManifest,
        config: ProviderConfig,
        supported_features: set[Any],
    ) -> None:
        """Initialize the AI Radio provider."""
        super().__init__(mass, manifest, config, supported_features)
        self._station_lock = asyncio.Lock()
        self._unregister_handles: list[Callable[[], None]] = []
        self._unloading = False
        self._engine_recheck_task: asyncio.Task[None] | None = None
        self._stations: dict[str, dict[str, Any]] = {}
        self._sections: dict[str, dict[str, Any]] = {}
        self._hosts: dict[str, dict[str, Any]] = {}
        self._dj_queues: dict[str, DJQueueState] = {}
        self._dj_lock = asyncio.Lock()
        self._show_runs: dict[str, _ShowRun] = {}
        self._show_runs_lock = asyncio.Lock()
        self._show_library_ids: dict[str, str] = {}
        # render contracts of the clips woven into a show's feed, by clip id (see media)
        self._feed_clip_contracts: dict[str, dict[str, Any]] = {}
        self._storage_dir = Path(self.mass.storage_path) / "ai_radio" / self.instance_id
        self._stations_file = self._storage_dir / "stations.json"
        self._sections_file = self._storage_dir / "sections.json"
        self._hosts_file = self._storage_dir / "hosts.json"
        self._dj_file = self._storage_dir / "queue_dj.json"

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to configure this provider."""
        from .config import get_config_entries as build_config_entries  # noqa: PLC0415

        return await build_config_entries(self.mass, self.instance_id)

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        await asyncio.to_thread(self._storage_dir.mkdir, parents=True, exist_ok=True)
        await self._load_sections()
        await self._load_hosts()
        await self._load_stations()
        # after loading, so a v2 stations file has had its chance to migrate its own hosts
        await self._seed_preset_hosts()
        await self._load_queue_dj()
        await self._wait_for_engines()
        await self._sync_show_library_items()
        self.logger.info(
            "AI Radio initialized for instance '%s' with %d stations, %d hosts and %d sections",
            self.instance_id,
            len(self._stations),
            len(self._hosts),
            len(self._sections),
        )

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        api_handlers = (
            ("ai_radio/stations/list", self.list_stations),
            ("ai_radio/stations/get", self.get_station),
            ("ai_radio/stations/save", self.save_station),
            ("ai_radio/stations/delete", self.delete_station),
            ("ai_radio/stations/validate", self.validate_station),
            ("ai_radio/stations/template", self.station_template),
            ("ai_radio/sections/list", self.list_sections),
            ("ai_radio/sections/get", self.get_section),
            ("ai_radio/sections/save", self.save_section),
            ("ai_radio/sections/delete", self.delete_section),
            ("ai_radio/sections/template", self.section_template),
            ("ai_radio/hosts/list", self.list_hosts),
            ("ai_radio/hosts/get", self.get_host),
            ("ai_radio/hosts/save", self.save_host),
            ("ai_radio/hosts/delete", self.delete_host),
            ("ai_radio/hosts/template", self.host_template),
            ("ai_radio/hosts/presets/list", self.list_host_presets),
            ("ai_radio/engines/tts/list", self.list_tts_engines),
            ("ai_radio/queue_dj/set", self.set_queue_dj),
            ("ai_radio/queue_dj/status", self.get_queue_dj_status),
        )
        for command, handler in api_handlers:
            # the queue DJ menu is queue state, not provider config: a client allowed to
            # arm it must also be allowed to read back what is armed
            if command.startswith("ai_radio/queue_dj/"):
                required_scope = Scope.QUEUES_CONTROL
            else:
                required_scope = (
                    Scope.CONFIG_PROVIDERS_READ
                    if command.endswith(("/list", "/get", "/template", "/validate"))
                    else Scope.CONFIG_PROVIDERS_WRITE
                )
            self._unregister_handles.append(
                self.mass.register_api_command(command, handler, required_scope=required_scope)
            )
        self._unregister_handles.append(
            self.mass.subscribe(self._on_providers_updated, EventType.PROVIDERS_UPDATED)
        )
        self._unregister_handles.append(
            self.mass.subscribe(
                self._on_dj_queue_event,
                (
                    EventType.QUEUE_ADDED,
                    EventType.QUEUE_ITEMS_UPDATED,
                    EventType.QUEUE_UPDATED,
                    EventType.PLAYER_REMOVED,
                ),
            )
        )
        # resume injection on the queues that were armed before this (re)start, without
        # waiting for a queue event that a paused or idle queue may never send
        for queue_id in list(self._dj_queues):
            self._schedule_replan(queue_id)
        self.logger.info(
            "AI Radio API routes registered (%d handlers)",
            len(api_handlers),
        )

    async def unload(self, is_removed: bool = False) -> None:
        """Handle close/cleanup of the provider."""
        self._unloading = True
        if self._engine_recheck_task and not self._engine_recheck_task.done():
            self._engine_recheck_task.cancel()
        for state in self._dj_queues.values():
            if state.task and not state.task.done():
                state.task.cancel()
        for handle in self._unregister_handles:
            handle()
        self._unregister_handles.clear()
        self.logger.info("AI Radio unloaded (removed=%s)", is_removed)
        await super().unload(is_removed)

    async def list_stations(self) -> list[dict[str, Any]]:
        """Return all configured AI Radio stations."""
        return sorted(
            (deepcopy(station) for station in self._stations.values()),
            key=lambda station: station["name"],
        )

    async def get_station(self, station_id: str) -> dict[str, Any]:
        """Return one station by id."""
        if station_id not in self._stations:
            raise KeyError(f"Unknown station id: {station_id}")
        return deepcopy(self._stations[station_id])

    async def save_station(self, station: dict[str, Any]) -> dict[str, Any]:
        """Create or update a station."""
        station_payload = deepcopy(station)
        async with self._station_lock:
            normalized = self._normalize_station(station_payload)
            self._stations[normalized["id"]] = normalized
            await self._write_stations()
        await self._sync_show_library_items()
        self.logger.info("AI Radio station saved: %s (%s)", normalized["id"], normalized["name"])
        return deepcopy(normalized)

    async def delete_station(self, station_id: str) -> None:
        """Delete a station."""
        async with self._station_lock:
            if station_id not in self._stations:
                raise KeyError(f"Unknown station id: {station_id}")
            self._stations.pop(station_id)
            await self._write_stations()
        await self._sync_show_library_items()
        self.logger.info("AI Radio station deleted: %s", station_id)

    async def validate_station(self, station: dict[str, Any]) -> dict[str, Any]:
        """Validate station payload and return the normalized profile."""
        return self._normalize_station(deepcopy(station))

    async def station_template(self) -> dict[str, Any]:
        """Return a default station template."""
        return self._default_station_template()

    async def list_sections(self) -> list[dict[str, Any]]:
        """Return all shared section definitions."""
        return sorted(
            (deepcopy(section) for section in self._sections.values()),
            key=lambda section: section["id"].lower(),
        )

    async def get_section(self, section_id: str) -> dict[str, Any]:
        """Return one shared section by id."""
        if section_id not in self._sections:
            raise KeyError(f"Unknown section id: {section_id}")
        return deepcopy(self._sections[section_id])

    async def save_section(self, section: dict[str, Any]) -> dict[str, Any]:
        """Create or update a shared section."""
        async with self._station_lock:
            normalized = self._normalize_section(section)
            self._sections[normalized["id"]] = normalized
            await self._write_sections()
        self.logger.info("AI Radio section saved: %s", normalized["id"])
        return deepcopy(normalized)

    async def delete_section(self, section_id: str) -> None:
        """Delete a shared section when no host uses it."""
        async with self._station_lock:
            if section_id not in self._sections:
                raise KeyError(f"Unknown section id: {section_id}")
            used_by = sorted(
                host["id"]
                for host in self._hosts.values()
                if section_id in host.get("section_ids", [])
            )
            if used_by:
                used_list = ", ".join(used_by)
                raise InvalidDataError(
                    f"Section '{section_id}' is used by hosts: {used_list}. "
                    "Remove it from those hosts first."
                )
            self._sections.pop(section_id)
            await self._write_sections()
        self.logger.info("AI Radio section deleted: %s", section_id)

    async def section_template(self) -> dict[str, Any]:
        """Return default section template."""
        defaults = self._default_sections_template()
        return deepcopy(defaults[0])

    async def list_hosts(self) -> list[dict[str, Any]]:
        """Return all configured AI Radio hosts."""
        return sorted(
            (deepcopy(host) for host in self._hosts.values()),
            key=lambda host: host["name"],
        )

    async def get_host(self, host_id: str) -> dict[str, Any]:
        """Return one host by id."""
        if host_id not in self._hosts:
            raise KeyError(f"Unknown host id: {host_id}")
        return deepcopy(self._hosts[host_id])

    async def save_host(self, host: dict[str, Any]) -> dict[str, Any]:
        """Create or update a host."""
        async with self._station_lock:
            normalized = self._normalize_host(deepcopy(host))
            self._hosts[normalized["id"]] = normalized
            await self._write_hosts()
        self.logger.info("AI Radio host saved: %s (%s)", normalized["id"], normalized["name"])
        return deepcopy(normalized)

    async def delete_host(self, host_id: str) -> None:
        """Delete a host when no station uses it and it is not the active DJ on a queue."""
        async with self._station_lock:
            if host_id not in self._hosts:
                raise KeyError(f"Unknown host id: {host_id}")
            used_by = [
                station["id"]
                for station in self._stations.values()
                if station.get("host_id") == host_id
            ]
            if used_by:
                used_list = ", ".join(sorted(used_by))
                raise InvalidDataError(
                    f"Host '{host_id}' is used by stations: {used_list}. "
                    "Remove it from those stations first."
                )
            dj_users = sorted(
                queue_id for queue_id, state in self._dj_queues.items() if state.host_id == host_id
            )
            if dj_users:
                raise InvalidDataError(
                    f"Host '{host_id}' is the active DJ on queues: {', '.join(dj_users)}. "
                    "Disable the DJ there first."
                )
            self._hosts.pop(host_id)
            await self._write_hosts()
        self.logger.info("AI Radio host deleted: %s", host_id)

    async def host_template(self) -> dict[str, Any]:
        """Return a default host template."""
        return self._default_host_template()

    async def list_host_presets(self) -> list[dict[str, Any]]:
        """Return the bundled preset hosts as templates a client can add from."""
        return [
            {"host": deepcopy(host), "sections": deepcopy(sections)}
            for host, sections in self._default_preset_hosts()
        ]

    async def list_tts_engines(self) -> list[dict[str, str]]:
        """Return the available TTS engines for host voice selection."""
        engines = await get_tts_engines(self.mass)
        return [{"uid": engine.uid, "name": engine.name} for engine in engines]

    async def _wait_for_engines(self, timeout: float | None = None) -> None:
        """
        Wait (bounded) until a concrete AI and TTS engine are selected for this instance.

        :param timeout: How long to wait, defaulting to the engine discovery timeout.
        :raises SetupFailedError: When either engine is still unavailable at the deadline.
        """
        engines_changed = asyncio.Event()
        unsubscribe = self.mass.subscribe(
            lambda _event: engines_changed.set(), EventType.PROVIDERS_UPDATED
        )
        try:
            async with asyncio.timeout(ENGINE_DISCOVERY_TIMEOUT if timeout is None else timeout):
                while (error := await self._engine_selection_error()) is not None:
                    await engines_changed.wait()
                    # clearing only after the wait keeps an update that lands during the
                    # probe above signalled, so that wakeup is never lost
                    engines_changed.clear()
        except TimeoutError:
            error = await self._engine_selection_error()
        finally:
            unsubscribe()
        if error is not None:
            raise error

    async def _engine_selection_error(self) -> SetupFailedError | None:
        """
        Seed a concrete engine selection where none is stored yet.

        :return: The error for the first engine that cannot be selected or no longer
            resolves, or None when both engines are settled.
        """
        if await select_ai_engine(self, CONF_AI_ENGINE, in_setup_data=True) is None:
            return SetupFailedError(
                "AI Radio has no AI engine available",
                translation_key="ai_radio_no_ai_engine",
                translation_owner=TRANSLATION_OWNER,
            )
        if await select_tts_engine(self, CONF_TTS_ENGINE, in_setup_data=True) is None:
            return SetupFailedError(
                "AI Radio has no text-to-speech engine available",
                translation_key="ai_radio_no_tts_engine",
                translation_owner=TRANSLATION_OWNER,
            )
        return None

    async def _on_providers_updated(self, _event: MassEvent) -> None:
        """Re-check the engine selection whenever the set of loaded providers changes."""
        # nothing to watch when this instance, or the whole server, is shutting down anyway
        if self._unloading or self.mass.closing:
            return
        if self._engine_recheck_task and not self._engine_recheck_task.done():
            return
        if await self._engine_selection_error() is None:
            return
        self._engine_recheck_task = self.mass.create_task(self._unload_when_engines_stay_missing())

    async def _unload_when_engines_stay_missing(self) -> None:
        """Unload with an error when a vanished engine does not come back in time."""
        # a plugin reload or a Home Assistant restart takes its engines with it for a
        # while, so wait that out instead of tearing the provider down right away
        try:
            await self._wait_for_engines(ENGINE_RECHECK_GRACE)
        except SetupFailedError as err:
            # a shutdown (or our own unload) landing during the wait can surface as the
            # timeout instead of a cancellation, and needs no error for the user
            if self._unloading or self.mass.closing:
                return
            self.logger.warning("%s - unloading the provider", err)
            self.unload_with_error(err)
            # unloading records the error but arms no retry of its own, so schedule the
            # reload that picks the provider back up once the engines return. Armed under
            # the load path's task id, so any (re)load starting before it fires cancels it.
            self.mass.call_later(
                ENGINE_RETRY_DELAY,
                self.mass.load_provider,
                self.instance_id,
                allow_retry=True,
                task_id=f"load_provider_{self.instance_id}",
            )
