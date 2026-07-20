"""AI Radio Plugin Provider for Music Assistant."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from music_assistant_models.auth import Scope
from music_assistant_models.errors import InvalidDataError

from music_assistant.models.plugin import PluginProvider

from .constants import (
    CONF_UI_AUTO_REFRESH_SECONDS,
    DEFAULT_MAX_CONCURRENT_RUNS,
    MAX_FINISHED_SESSIONS,
    SUPPORTED_FEATURES,
)
from .helpers import coerce_int, utc_now_iso
from .models import SessionState
from .runtime import AIRadioRuntimeMixin
from .storage import AIRadioStorageMixin

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return AIRadioProvider(mass, manifest, config, SUPPORTED_FEATURES)


class AIRadioProvider(AIRadioRuntimeMixin, AIRadioStorageMixin, PluginProvider):
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
        self._sessions: dict[str, SessionState] = {}
        self._stations: dict[str, dict[str, Any]] = {}
        self._sections: dict[str, dict[str, Any]] = {}
        self._storage_dir = Path(self.mass.storage_path) / "ai_radio" / self.instance_id
        self._stations_file = self._storage_dir / "stations.json"
        self._sections_file = self._storage_dir / "sections.json"

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        await asyncio.to_thread(self._storage_dir.mkdir, parents=True, exist_ok=True)
        await self._load_sections()
        await self._load_stations()
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
            ("ai_radio/ui_settings", self.get_ui_settings),
        )
        for command, handler in api_handlers:
            required_scope = (
                Scope.CONFIG_PROVIDERS_READ
                if command.endswith(
                    ("/list", "/get", "/template", "/validate", "/status", "/ui_settings")
                )
                else Scope.CONFIG_PROVIDERS_WRITE
            )
            self._unregister_handles.append(
                self.mass.register_api_command(command, handler, required_scope=required_scope)
            )
        self.logger.info(
            "AI Radio API routes registered (%d handlers)",
            len(self._unregister_handles),
        )

    async def unload(self, is_removed: bool = False) -> None:
        """Handle close/cleanup of the provider."""
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
        mode: str = "playlist",
        source_playlist_id_override: str | None = None,
        source_playlist_provider_override: str | None = None,
        player_id_override: str | None = None,
        dynamic_source_playtime_cap_override: int | float | None = None,  # noqa: PYI041
        dynamic_batch_size_override: int | None = None,
    ) -> dict[str, Any]:
        """Start a new AI Radio run."""
        selected_mode = mode.strip().lower()
        if selected_mode not in {"playlist", "dynamic"}:
            raise InvalidDataError("mode must be 'playlist' or 'dynamic'")
        if station_id not in self._stations:
            raise KeyError(f"Unknown station id: {station_id}")

        max_runs = DEFAULT_MAX_CONCURRENT_RUNS
        running = [session for session in self._sessions.values() if session.status == "running"]
        if len(running) >= max_runs:
            raise InvalidDataError(
                f"Max concurrent runs reached ({max_runs}). Stop an active run first."
            )
        if any(
            session.status == "running" and session.station_id == station_id
            for session in self._sessions.values()
        ):
            raise InvalidDataError(f"Station {station_id} already has an active run")

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
        if dynamic_batch_size_override is not None:
            if int(dynamic_batch_size_override) < 1:
                raise InvalidDataError("dynamic_batch_size_override must be >= 1")
            station["dynamic_batch_size"] = int(dynamic_batch_size_override)
        if selected_mode == "dynamic":
            player_id = str(station.get("default_player_id") or "").strip()
            if not player_id:
                raise InvalidDataError("Dynamic mode requires a target player_id")
            player = self.mass.players.get_player(player_id)
            if player is None:
                raise InvalidDataError(f"Unknown target player: {player_id}")
            if player.available is False:
                raise InvalidDataError(f"Target player is unavailable: {player_id}")
            if player.enabled is False:
                raise InvalidDataError(f"Target player is disabled: {player_id}")

        session_id = uuid4().hex
        session = SessionState(
            session_id=session_id,
            station_id=station_id,
            mode=selected_mode,
        )
        self._sessions[session_id] = session
        self._prune_finished_sessions()
        session.task = self.mass.create_task(
            self._run_session(session_id, station),
            task_id=f"ai_radio_session_{session_id}",
        )
        self.logger.info(
            "AI Radio session started: session=%s station=%s mode=%s",
            session_id,
            station_id,
            selected_mode,
        )
        return session.as_dict()

    async def stop_run(
        self,
        session_id: str | None = None,
        station_id: str | None = None,
    ) -> dict[str, Any]:
        """Stop an active run."""
        selected = self._resolve_session_for_stop(session_id=session_id, station_id=station_id)

        if selected.task and not selected.task.done():
            selected.task.cancel()
        selected.status = "stopped"
        selected.ended_at = utc_now_iso()
        self.logger.info(
            "AI Radio session stopped: session=%s station=%s mode=%s",
            selected.session_id,
            selected.station_id,
            selected.mode,
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

    async def get_ui_settings(self) -> dict[str, int]:
        """Return UI behavior settings for the plugin web page."""
        value = self.config.get_value(CONF_UI_AUTO_REFRESH_SECONDS)
        interval_seconds = max(1, coerce_int(value, 2))
        return {"auto_refresh_seconds": interval_seconds}

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
