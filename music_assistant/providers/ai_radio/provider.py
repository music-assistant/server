"""AI Radio Plugin Provider for Music Assistant."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from music_assistant_models.auth import Scope
from music_assistant_models.enums import EventType
from music_assistant_models.errors import InvalidDataError, SetupFailedError

from music_assistant.helpers.plugin_engines import (
    select_ai_engine,
    select_tts_engine,
)
from music_assistant.models.plugin import PluginProvider

from .constants import (
    CONF_AI_ENGINE,
    CONF_TTS_ENGINE,
    DEFAULT_MAX_CONCURRENT_RUNS,
    ENGINE_DISCOVERY_TIMEOUT,
    ENGINE_RECHECK_GRACE,
    ENGINE_RETRY_DELAY,
    MAX_FINISHED_SESSIONS,
    SUPPORTED_FEATURES,
    TRANSLATION_OWNER,
)
from .helpers import utc_now_iso
from .models import SessionState
from .rendering import AIRadioRenderMixin
from .runtime import AIRadioRuntimeMixin
from .storage import AIRadioStorageMixin

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry, ProviderConfig
    from music_assistant_models.event import MassEvent
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return AIRadioProvider(mass, manifest, config, SUPPORTED_FEATURES)


class AIRadioProvider(AIRadioRuntimeMixin, AIRadioRenderMixin, AIRadioStorageMixin, PluginProvider):
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
        self._session_lock = asyncio.Lock()
        self._unregister_handles: list[Callable[[], None]] = []
        self._unloading = False
        self._engine_recheck_task: asyncio.Task[None] | None = None
        self._sessions: dict[str, SessionState] = {}
        self._stations: dict[str, dict[str, Any]] = {}
        self._sections: dict[str, dict[str, Any]] = {}
        self._storage_dir = Path(self.mass.storage_path) / "ai_radio" / self.instance_id
        self._stations_file = self._storage_dir / "stations.json"
        self._sections_file = self._storage_dir / "sections.json"

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to configure this provider."""
        from .config import get_config_entries as build_config_entries  # noqa: PLC0415

        return await build_config_entries(self.mass, self.instance_id)

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        await asyncio.to_thread(self._storage_dir.mkdir, parents=True, exist_ok=True)
        await self._load_sections()
        await self._load_stations()
        await self._wait_for_engines()
        self.logger.info(
            "AI Radio initialized for instance '%s' with %d stations and %d sections",
            self.instance_id,
            len(self._stations),
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
            ("ai_radio/start", self.start_run),
            ("ai_radio/stop", self.stop_run),
            ("ai_radio/status", self.get_status),
        )
        for command, handler in api_handlers:
            required_scope = (
                Scope.CONFIG_PROVIDERS_READ
                if command.endswith(("/list", "/get", "/template", "/validate", "/status"))
                else Scope.CONFIG_PROVIDERS_WRITE
            )
            self._unregister_handles.append(
                self.mass.register_api_command(command, handler, required_scope=required_scope)
            )
        self._unregister_handles.append(
            self.mass.subscribe(self._on_providers_updated, EventType.PROVIDERS_UPDATED)
        )
        self.logger.info(
            "AI Radio API routes registered (%d handlers)",
            len(api_handlers),
        )

    async def unload(self, is_removed: bool = False) -> None:
        """Handle close/cleanup of the provider."""
        self._unloading = True
        if self._engine_recheck_task and not self._engine_recheck_task.done():
            self._engine_recheck_task.cancel()
        cancelled = 0
        for session in self._sessions.values():
            if session.task and not session.task.done():
                session.task.cancel()
                cancelled += 1
        for handle in self._unregister_handles:
            handle()
        self._unregister_handles.clear()
        self.logger.info(
            "AI Radio unloaded (removed=%s, cancelled_sessions=%d)",
            is_removed,
            cancelled,
        )
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
            # validate against a scratch copy so a rejected station
            # cannot leave partial section edits behind
            sections_scratch = deepcopy(self._sections)
            sections_changed = self._upsert_embedded_sections_from_station(
                station_payload, sections_scratch
            )
            normalized = self._normalize_station(station_payload, sections_scratch)
            self._stations[normalized["id"]] = normalized
            if sections_changed:
                self._sections = sections_scratch
                await self._write_sections()
            await self._write_stations()
        self.logger.info("AI Radio station saved: %s (%s)", normalized["id"], normalized["name"])
        return deepcopy(normalized)

    async def delete_station(self, station_id: str) -> None:
        """Delete a station."""
        async with self._station_lock:
            if station_id not in self._stations:
                raise KeyError(f"Unknown station id: {station_id}")
            self._stations.pop(station_id)
            await self._write_stations()
        self.logger.info("AI Radio station deleted: %s", station_id)

    async def validate_station(self, station: dict[str, Any]) -> dict[str, Any]:
        """Validate station payload and return the normalized profile."""
        station_payload = deepcopy(station)
        # validation must be side-effect free: resolve embedded sections
        # against a scratch copy instead of the shared section store
        sections_scratch = deepcopy(self._sections)
        self._upsert_embedded_sections_from_station(station_payload, sections_scratch)
        return self._normalize_station(station_payload, sections_scratch)

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
        normalized = self._normalize_section(section)
        async with self._station_lock:
            self._sections[normalized["id"]] = normalized
            self._refresh_station_sections()
            await self._write_sections()
            await self._write_stations()
        self.logger.info("AI Radio section saved: %s", normalized["id"])
        return deepcopy(normalized)

    async def delete_section(self, section_id: str) -> None:
        """Delete a shared section when unused by stations."""
        async with self._station_lock:
            if section_id not in self._sections:
                raise KeyError(f"Unknown section id: {section_id}")
            used_by = [
                station["id"]
                for station in self._stations.values()
                if section_id in station.get("section_ids", [])
            ]
            if used_by:
                used_list = ", ".join(sorted(used_by))
                raise InvalidDataError(
                    f"Section '{section_id}' is used by stations: {used_list}. "
                    "Remove it from those stations first."
                )
            self._sections.pop(section_id)
            await self._write_sections()
        self.logger.info("AI Radio section deleted: %s", section_id)

    async def section_template(self) -> dict[str, Any]:
        """Return default section template."""
        defaults = self._default_sections_template()
        return deepcopy(defaults[0])

    async def start_run(
        self,
        station_id: str,
        source_playlist_id_override: str | None = None,
        source_playlist_provider_override: str | None = None,
        player_id_override: str | None = None,
        dynamic_source_playtime_cap_override: int | float | None = None,  # noqa: PYI041
    ) -> dict[str, Any]:
        """Start a new AI Radio run."""
        if station_id not in self._stations:
            raise KeyError(f"Unknown station id: {station_id}")

        station = deepcopy(self._stations[station_id])
        overrides = {
            "source_playlist_id": source_playlist_id_override,
            "source_playlist_provider": source_playlist_provider_override,
            "default_player_id": player_id_override,
        }
        for key, value in overrides.items():
            if value:
                station[key] = value
        if dynamic_source_playtime_cap_override is not None:
            if float(dynamic_source_playtime_cap_override) < 0:
                raise InvalidDataError("dynamic_source_playtime_cap_override must be >= 0")
            station["max_duration_minutes"] = float(dynamic_source_playtime_cap_override)
        player_id = str(station.get("default_player_id") or "").strip()
        if not player_id:
            raise InvalidDataError("AI Radio requires a target player")
        player = self.mass.players.get_player(player_id)
        if player is None:
            raise InvalidDataError(f"Unknown target player: {player_id}")
        if player.available is False:
            raise InvalidDataError(f"Target player is unavailable: {player_id}")
        if player.enabled is False:
            raise InvalidDataError(f"Target player is disabled: {player_id}")

        # the run guards and the session insert must stay one critical section, or a future
        # await between them would let concurrent callers slip past the concurrency limits
        async with self._session_lock:
            max_runs = DEFAULT_MAX_CONCURRENT_RUNS
            running = [
                session for session in self._sessions.values() if session.status == "running"
            ]
            if len(running) >= max_runs:
                raise InvalidDataError(
                    f"Max concurrent runs reached ({max_runs}). Stop an active run first."
                )
            if any(
                session.status == "running" and session.station_id == station_id
                for session in self._sessions.values()
            ):
                raise InvalidDataError(f"Station {station_id} already has an active run")

            session_id = uuid4().hex
            session = SessionState(
                session_id=session_id,
                station_id=station_id,
            )
            self._sessions[session_id] = session
            self._prune_finished_sessions()
            session.task = self.mass.create_task(
                self._run_session(session_id, station),
                task_id=f"ai_radio_session_{session_id}",
            )
        self.logger.debug(
            "AI Radio session started: session=%s station=%s",
            session_id,
            station_id,
        )
        return session.as_dict()

    async def stop_run(
        self,
        session_id: str | None = None,
        station_id: str | None = None,
    ) -> dict[str, Any]:
        """Stop an active run."""
        selected = self._resolve_session_for_stop(session_id=session_id, station_id=station_id)

        # cancel first so the run cannot queue another batch after playback stopped
        if selected.task and not selected.task.done():
            selected.task.cancel()
        selected.status = "stopped"
        selected.ended_at = utc_now_iso()
        await self._stop_session_queue(selected)
        self.logger.info(
            "AI Radio session stopped: session=%s station=%s",
            selected.session_id,
            selected.station_id,
        )
        return selected.as_dict()

    async def get_status(self, session_id: str | None = None) -> dict[str, Any]:
        """Return run status information."""
        if session_id:
            if session_id not in self._sessions:
                raise KeyError(f"Unknown session id: {session_id}")
            return {"sessions": [self._sessions[session_id].as_dict()]}
        sessions = sorted(self._sessions.values(), key=lambda item: item.created_at, reverse=True)
        return {"sessions": [session.as_dict() for session in sessions]}

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
        # engines going away with an instance that is itself going away needs no action
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

    def _prune_finished_sessions(self) -> None:
        """Drop the oldest finished sessions beyond the retention limit."""
        finished = sorted(
            (session for session in self._sessions.values() if session.status != "running"),
            key=lambda item: item.created_at,
            reverse=True,
        )
        for session in finished[MAX_FINISHED_SESSIONS:]:
            self._sessions.pop(session.session_id, None)

    def _resolve_session_for_stop(
        self,
        session_id: str | None,
        station_id: str | None,
    ) -> SessionState:
        """Resolve which running session should be stopped."""
        if session_id:
            selected = self._sessions.get(session_id)
            if selected is None:
                raise KeyError(f"Unknown session id: {session_id}")
            if selected.status != "running":
                raise InvalidDataError(
                    f"Session {session_id} is not running (status={selected.status})"
                )
            return selected

        running = [session for session in self._sessions.values() if session.status == "running"]
        if station_id:
            running = [session for session in running if session.station_id == station_id]
            if not running:
                raise KeyError(f"No active run found for station: {station_id}")
        elif not running:
            raise KeyError("No active AI Radio run found")

        return max(running, key=lambda item: item.created_at)
