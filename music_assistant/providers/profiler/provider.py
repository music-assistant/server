"""Profiler provider implementation."""

from __future__ import annotations

import asyncio
import collections
import logging
import os
import platform
import shutil
import time
import tracemalloc
from collections import deque
from typing import TYPE_CHECKING, Any

import psutil
import yappi
from music_assistant_models.auth import Scope
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

from music_assistant.helpers.datetime import utc
from music_assistant.models.plugin import PluginProvider

from .helpers import (
    LogErrorCounter,
    collect_memory_stats,
    collect_object_census,
    collect_task_stats,
    collect_tracemalloc_stats,
    extract_cpu_profile,
    finalize_recorder_entry,
    persist_report,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from music_assistant_models.event import MassEvent

CONF_CPU_PROFILE_ENABLED = "cpu_profile_enabled"
CONF_CPU_PROFILE_DURATION = "cpu_profile_duration"
CONF_CPU_PROFILE_INTERVAL = "cpu_profile_interval"
CONF_TRACEMALLOC_ENABLED = "tracemalloc_enabled"
CONF_ACTION_RUN_CPU_PROFILE = "action_run_cpu_profile"

REPORT_FORMAT_VERSION = 1
LAG_MONITOR_INTERVAL = 0.5
RECORDER_INTERVAL = 10
# 24 hours of history at the 10 second sample interval (roughly 1-2 MB of memory)
RECORDER_MAX_ENTRIES = 8640
CPU_PROFILE_FIRST_DELAY = 60
CPU_PROFILE_MIN_DURATION = 10
CPU_PROFILE_MAX_DURATION = 300
CPU_PROFILE_MIN_INTERVAL = 5  # minutes
CPU_PROFILE_MAX_INTERVAL = 1440  # minutes


class ProfilerProvider(PluginProvider):
    """
    Plugin provider that continuously records performance diagnostics.

    While loaded it runs a lightweight flight recorder, an event-loop lag
    monitor and event/error counters, plus (optional) periodic CPU profile
    windows. All collected data is aggregated into a shareable report via
    the `profiler/report` API command.
    """

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to configure this provider."""
        return (
            ConfigEntry(
                key="profiler_note",
                type=ConfigEntryType.LABEL,
                required=False,
            ),
            ConfigEntry(
                key=CONF_CPU_PROFILE_ENABLED,
                type=ConfigEntryType.BOOLEAN,
                default_value=True,
                required=False,
            ),
            ConfigEntry(
                key=CONF_CPU_PROFILE_DURATION,
                type=ConfigEntryType.INTEGER,
                default_value=60,
                range=(CPU_PROFILE_MIN_DURATION, CPU_PROFILE_MAX_DURATION),
                required=False,
                advanced=True,
                depends_on=CONF_CPU_PROFILE_ENABLED,
            ),
            ConfigEntry(
                key=CONF_CPU_PROFILE_INTERVAL,
                type=ConfigEntryType.INTEGER,
                default_value=30,
                range=(CPU_PROFILE_MIN_INTERVAL, CPU_PROFILE_MAX_INTERVAL),
                required=False,
                advanced=True,
                depends_on=CONF_CPU_PROFILE_ENABLED,
            ),
            ConfigEntry(
                key=CONF_ACTION_RUN_CPU_PROFILE,
                type=ConfigEntryType.ACTION,
                action=CONF_ACTION_RUN_CPU_PROFILE,
                required=False,
            ),
            ConfigEntry(
                key=CONF_TRACEMALLOC_ENABLED,
                type=ConfigEntryType.BOOLEAN,
                default_value=False,
                required=False,
            ),
        )

    async def handle_config_action(self, action: str) -> tuple[ConfigEntry, ...]:
        """Handle a one-shot config action button press and re-render the entries."""
        if action == CONF_ACTION_RUN_CPU_PROFILE:
            self.start_cpu_profile()
            return await self.get_config_entries()
        return await super().handle_config_action(action)

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self._proc = psutil.Process()
        # prime the cpu percent counter so subsequent calls return meaningful values
        self._proc.cpu_percent()
        self._out_dir = os.path.join(self.mass.storage_path, "profiler")
        await asyncio.to_thread(os.makedirs, self._out_dir, exist_ok=True)
        self._loaded_at = time.time()
        self._recorder_ring: deque[dict[str, Any]] = deque(maxlen=RECORDER_MAX_ENTRIES)
        self._lag_samples: deque[float] = deque(maxlen=120)
        self._lag_window_max = 0.0
        self._lag_max_since_load = 0.0
        self._event_counts: collections.Counter[str] = collections.Counter()
        self._events_since_sample = 0
        self._errors_at_last_sample = 0
        self._log_counter = LogErrorCounter()
        self._last_cpu_profile: dict[str, Any] | None = None
        self._cpu_profile_running = False
        self._tracemalloc_prev: tracemalloc.Snapshot | None = None
        self._started_tracemalloc = False
        self._unsubscribe_events: Callable[[], None] | None = None
        self._unregister_api: Callable[[], None] | None = None

    async def loaded_in_mass(self) -> None:
        """Start all measurements once the provider is fully loaded."""
        await super().loaded_in_mass()
        logging.getLogger().addHandler(self._log_counter)
        self._unsubscribe_events = self.mass.subscribe(self._on_mass_event)
        self._unregister_api = self.mass.register_api_command(
            "profiler/report", self.get_report, required_scope=Scope.SYSTEM_MANAGE
        )
        if (
            self.get_config_value(CONF_TRACEMALLOC_ENABLED, False, return_type=bool)
            and not tracemalloc.is_tracing()
        ):
            tracemalloc.start(15)
            self._started_tracemalloc = True
        self.mass.create_task(self._loop_lag_monitor(), task_id="profiler_lag_monitor")
        self.mass.create_task(self._flight_recorder(), task_id="profiler_flight_recorder")
        if self.get_config_value(CONF_CPU_PROFILE_ENABLED, True, return_type=bool):
            self.mass.create_task(self._cpu_profile_scheduler(), task_id="profiler_cpu_scheduler")

    async def unload(self, is_removed: bool = False) -> None:
        """Stop all measurements and clean up."""
        for task_id in (
            "profiler_lag_monitor",
            "profiler_flight_recorder",
            "profiler_cpu_scheduler",
            "profiler_cpu_window",
        ):
            self.mass.cancel_task(task_id)
        if self._unregister_api is not None:
            self._unregister_api()
            self._unregister_api = None
        if self._unsubscribe_events is not None:
            self._unsubscribe_events()
            self._unsubscribe_events = None
        logging.getLogger().removeHandler(self._log_counter)
        if yappi.is_running():
            yappi.stop()
        yappi.clear_stats()
        if self._started_tracemalloc and tracemalloc.is_tracing():
            tracemalloc.stop()
            self._started_tracemalloc = False
        self._tracemalloc_prev = None
        if is_removed:
            await asyncio.to_thread(shutil.rmtree, self._out_dir, ignore_errors=True)
        await super().unload(is_removed)

    async def get_report(
        self,
        markdown: bool = False,
        include_object_census: bool = False,
        recorder_minutes: int = 30,
    ) -> dict[str, Any] | str:
        """
        Generate the full diagnostics report (also written to the profiler storage folder).

        :param markdown: Return the report as markdown text instead of a JSON object.
        :param include_object_census: Include a census of all live Python objects by type.
            This walks the entire object heap, which can stall the server for several
            seconds on large installations - only use this when hunting a memory leak.
        :param recorder_minutes: Minutes of flight-recorder history to include (1-1440).
        """
        report = await self._build_report(include_object_census, recorder_minutes)
        report_md = await asyncio.to_thread(persist_report, self._out_dir, report)
        return report_md if markdown else report

    def start_cpu_profile(self) -> None:
        """Start a single on-demand CPU profile window (ignored if one is already running)."""
        self.mass.create_task(self._run_cpu_profile_window(), task_id="profiler_cpu_window")

    async def _build_report(
        self, include_object_census: bool, recorder_minutes: int
    ) -> dict[str, Any]:
        """Assemble the report dict from all collectors."""
        memory = await asyncio.to_thread(collect_memory_stats, self._proc)
        memory["asyncio_tasks"] = len(asyncio.all_tasks(self.mass.loop))
        memory["tracked_tasks"] = len(self.mass._tracked_tasks)
        memory["tracked_timers"] = len(self.mass._tracked_timers)
        memory["event_subscribers"] = len(self.mass._subscribers)
        if include_object_census:
            memory["object_census_top"] = await asyncio.to_thread(collect_object_census)
        if tracemalloc.is_tracing():
            memory["tracemalloc"], self._tracemalloc_prev = await asyncio.to_thread(
                collect_tracemalloc_stats, self._tracemalloc_prev
            )
        recorder_minutes = max(1, min(recorder_minutes, 1440))
        cutoff = time.time() - recorder_minutes * 60
        lag_samples = list(self._lag_samples)
        return {
            "report_format_version": REPORT_FORMAT_VERSION,
            "generated_at": utc().isoformat(timespec="seconds"),
            "server": {
                "version": self.mass.version,
                "python": platform.python_version(),
                "platform": platform.platform(),
                "machine": platform.machine(),
                "uptime_s": int(time.time() - self._proc.create_time()),
                "profiler_loaded_for_s": int(time.time() - self._loaded_at),
                "running_as_hass_addon": self.mass.running_as_hass_addon,
                "tracemalloc_active": tracemalloc.is_tracing(),
            },
            "config_summary": {
                "providers_by_type": dict(
                    collections.Counter(prov.type.value for prov in self.mass.providers)
                ),
                "provider_domains": sorted({prov.domain for prov in self.mass.providers}),
                "players_total": len(self.mass.players.all_players(True, True)),
                "players_available": len(self.mass.players.all_players(False, False)),
                "web_clients_connected": len(self.mass.webserver.clients),
                "library_counts": await self._get_library_counts(),
            },
            "memory": memory,
            "event_loop": {
                "sample_interval_s": LAG_MONITOR_INTERVAL,
                "lag_avg_ms_1min": (
                    round(sum(lag_samples) / len(lag_samples), 2) if lag_samples else None
                ),
                "lag_max_ms_1min": round(max(lag_samples), 2) if lag_samples else None,
                "lag_max_ms_since_load": round(self._lag_max_since_load, 2),
            },
            "asyncio_tasks": collect_task_stats(asyncio.all_tasks(self.mass.loop)),
            "events": {
                "total_since_load": sum(self._event_counts.values()),
                "per_type_top": [
                    {"event": event, "count": count}
                    for event, count in self._event_counts.most_common(30)
                ],
            },
            "log_errors": self._log_counter.summarize(),
            "cpu_profile": self._last_cpu_profile,
            "flight_recorder": {
                "sample_interval_s": RECORDER_INTERVAL,
                "window_minutes": recorder_minutes,
                "entries": [entry for entry in self._recorder_ring if entry["ts_unix"] >= cutoff],
            },
        }

    async def _get_library_counts(self) -> dict[str, int]:
        """Return the number of library items per media type."""
        counts: dict[str, int] = {}
        for controller in (
            self.mass.music.artists,
            self.mass.music.albums,
            self.mass.music.tracks,
            self.mass.music.playlists,
            self.mass.music.radio,
            self.mass.music.audiobooks,
            self.mass.music.podcasts,
        ):
            counts[controller.media_type.value] = await controller.library_count()
        return counts

    def _on_mass_event(self, event: MassEvent) -> None:
        """Count the event (cheap increment, runs for every event on the bus)."""
        self._event_counts[event.event.value] += 1
        self._events_since_sample += 1

    async def _loop_lag_monitor(self) -> None:
        """Continuously sample event-loop scheduling delay via sleep drift."""
        while True:
            start = time.monotonic()
            await asyncio.sleep(LAG_MONITOR_INTERVAL)
            lag_ms = max((time.monotonic() - start - LAG_MONITOR_INTERVAL) * 1000, 0.0)
            self._lag_samples.append(lag_ms)
            self._lag_window_max = max(self._lag_window_max, lag_ms)
            self._lag_max_since_load = max(self._lag_max_since_load, lag_ms)

    async def _flight_recorder(self) -> None:
        """Sample server health every few seconds into the bounded in-memory ring."""
        csv_path = os.path.join(self._out_dir, "stats.csv")
        while True:
            await asyncio.sleep(RECORDER_INTERVAL)
            try:
                entry = self._collect_recorder_entry()
                entry = await asyncio.to_thread(
                    finalize_recorder_entry, self._proc, entry, csv_path
                )
                self._recorder_ring.append(entry)
            except Exception as err:
                self.logger.debug("Flight recorder sample failed: %s", err)

    def _collect_recorder_entry(self) -> dict[str, Any]:
        """Collect the event-loop side of a flight-recorder sample."""
        lag_samples = list(self._lag_samples)
        entry = {
            "ts_unix": int(time.time()),
            "loop_lag_avg_ms": (
                round(sum(lag_samples) / len(lag_samples), 2) if lag_samples else 0.0
            ),
            "loop_lag_max_ms": round(self._lag_window_max, 2),
            "asyncio_tasks": len(asyncio.all_tasks(self.mass.loop)),
            "tracked_tasks": len(self.mass._tracked_tasks),
            "tracked_timers": len(self.mass._tracked_timers),
            "event_subscribers": len(self.mass._subscribers),
            "ws_clients": len(self.mass.webserver.clients),
            "events_per_s": round(self._events_since_sample / RECORDER_INTERVAL, 2),
            "log_errors_per_s": round(
                (self._log_counter.total - self._errors_at_last_sample) / RECORDER_INTERVAL, 2
            ),
        }
        self._lag_window_max = 0.0
        self._events_since_sample = 0
        self._errors_at_last_sample = self._log_counter.total
        return entry

    async def _cpu_profile_scheduler(self) -> None:
        """Periodically capture a CPU profile window."""
        interval_minutes = min(
            max(
                self.get_config_value(CONF_CPU_PROFILE_INTERVAL, 30, return_type=int),
                CPU_PROFILE_MIN_INTERVAL,
            ),
            CPU_PROFILE_MAX_INTERVAL,
        )
        # small initial delay so a fresh install has profile data available quickly
        await asyncio.sleep(CPU_PROFILE_FIRST_DELAY)
        while True:
            await self._run_cpu_profile_window()
            await asyncio.sleep(interval_minutes * 60)

    async def _run_cpu_profile_window(self) -> None:
        """Capture a single CPU profile window and store the extracted result."""
        if self._cpu_profile_running or yappi.is_running():
            return
        duration = min(
            max(
                self.get_config_value(CONF_CPU_PROFILE_DURATION, 60, return_type=int),
                CPU_PROFILE_MIN_DURATION,
            ),
            CPU_PROFILE_MAX_DURATION,
        )
        self.logger.info("Capturing CPU profile window of %s seconds...", duration)
        self._cpu_profile_running = True
        started = time.monotonic()
        try:
            yappi.set_clock_type("cpu")
            # clear any leftovers from a previously aborted window as yappi accumulates
            yappi.clear_stats()
            yappi.start(builtins=False)
            await asyncio.sleep(duration)
        finally:
            yappi.stop()
            self._cpu_profile_running = False
        self._last_cpu_profile = await asyncio.to_thread(
            extract_cpu_profile, time.monotonic() - started, self._out_dir
        )
        self.logger.info("CPU profile window completed")
