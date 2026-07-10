"""
Diagnostics controller: on-demand, privacy-safe troubleshooting reports.

Serves a single downloadable report (JSON or markdown) combining the always-on
exception/log capture (see helpers/diagnostics.py) with system info, an install
census and pluggable sections contributed by core controllers, providers and
external registrants. All report building happens on request only; every string
that ends up in the report is sanitized so it can safely be attached to a public
GitHub issue.
"""

from __future__ import annotations

import asyncio
import inspect
import platform
import resource
import shutil
import sys
import threading
import time
from collections import Counter
from typing import TYPE_CHECKING, Any

from aiohttp import web

from music_assistant.controllers.webserver.helpers.auth_middleware import require_admin
from music_assistant.helpers.api import api_command
from music_assistant.helpers.datetime import from_utc_timestamp, utc
from music_assistant.helpers.diagnostics import (
    REDACTION_NOTICE,
    install_diagnostics_log_handler,
    sanitize_data,
    sanitize_text,
)
from music_assistant.helpers.json import json_dumps, json_loads
from music_assistant.models.core_controller import CoreController
from music_assistant.models.provider import Provider

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from music_assistant_models.config_entries import CoreConfig

    from music_assistant.mass import MusicAssistant

SCHEMA_VERSION = 1
# maximum time one section contributor may take before it is dropped from the report
SECTION_TIMEOUT = 2.0

# core controller attributes on MusicAssistant that may implement get_diagnostics
CORE_CONTROLLER_ATTRS = (
    "cache",
    "discovery",
    "metadata",
    "music",
    "player_queues",
    "players",
    "streams",
    "tasks",
    "translations",
    "webserver",
)

type DiagnosticsSectionCallback = Callable[
    [], dict[str, Any] | None | Awaitable[dict[str, Any] | None]
]


class DiagnosticsController(CoreController):
    """Core controller that assembles privacy-safe diagnostics reports on demand."""

    domain: str = "diagnostics"

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize the diagnostics controller."""
        super().__init__(mass)
        self.manifest.name = "Diagnostics"
        self.manifest.description = (
            "Provides a downloadable, privacy-safe diagnostics report for troubleshooting."
        )
        self.manifest.icon = "stethoscope"
        # adopt the always-on capture handler (installed even earlier when booted
        # through __main__, otherwise installed right here)
        self._log_handler = install_diagnostics_log_handler()
        self._sections: dict[str, DiagnosticsSectionCallback] = {}
        self._started_at = time.monotonic()

    async def setup(self, config: CoreConfig) -> None:
        """Async initialize of module."""

    async def close(self) -> None:
        """Handle logic on server stop."""
        self._sections.clear()

    def register_section(
        self, name: str, callback: DiagnosticsSectionCallback
    ) -> Callable[[], None]:
        """
        Register a callback that contributes a named section to the diagnostics report.

        The callback is invoked whenever a report is requested and may be sync or
        async. Async callbacks run with a timeout; sync callbacks run on the event
        loop and must return quickly without blocking I/O. Returns a function to
        unregister the section again.

        :param name: Unique name for the section within the report.
        :param callback: Callable returning the section data as a (JSON-safe) dict.
        """
        if name in self._sections:
            raise ValueError(f"A diagnostics section named '{name}' is already registered")
        self._sections[name] = callback

        def unregister() -> None:
            # only remove if this exact registration still owns the name
            if self._sections.get(name) is callback:
                self._sections.pop(name)

        return unregister

    @api_command("diagnostics/get", required_role="admin")
    async def get_report(self, include_log_tail: bool = False) -> dict[str, Any]:
        """
        Return a full (sanitized) diagnostics report.

        :param include_log_tail: Include the recent warning/error log excerpt.
        """
        return await self._build_report(include_log_tail=include_log_tail)

    async def handle_http_download(self, request: web.Request) -> web.Response:
        """
        Serve the diagnostics report as a downloadable file (webserver route handler).

        Supports query parameters `format` (json|md) and `include_log_tail` (true|false).

        :param request: The incoming aiohttp request.
        """
        await require_admin(request)
        include_log_tail = request.query.get("include_log_tail", "").lower() in ("1", "true")
        report = await self._build_report(include_log_tail=include_log_tail)
        filename = f"music-assistant-diagnostics-{utc().strftime('%Y-%m-%d-%H%M%S')}"
        if request.query.get("format", "json").lower() in ("md", "markdown"):
            body = self._render_markdown(report)
            content_type = "text/markdown"
            filename += ".md"
        else:
            body = json_dumps(report, indent=True)
            content_type = "application/json"
            filename += ".json"
        return web.Response(
            text=body,
            content_type=content_type,
            charset="utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    async def _build_report(self, include_log_tail: bool = False) -> dict[str, Any]:
        """Assemble the full report; every part is isolated so the report never fails."""
        report: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": utc().isoformat(timespec="seconds"),
            "redaction_notice": REDACTION_NOTICE,
        }
        for key, builder in (
            ("system", self._build_system_info),
            ("install", self._build_install_census),
            ("exceptions", self._build_exception_summary),
            ("sections", self._collect_sections),
        ):
            try:
                report[key] = await builder()
            except Exception as err:
                report[key] = {"error": sanitize_text(f"{type(err).__name__}: {err}")}
        if include_log_tail:
            report["log_tail"] = self._build_log_tail()
        return report

    async def _build_system_info(self) -> dict[str, Any]:
        """Collect a point-in-time snapshot of system/runtime info."""
        disk_info, memory_info = await asyncio.to_thread(self._probe_system_blocking)
        return {
            "version": self.mass.version,
            "python_version": platform.python_version(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "hass_addon": self.mass.running_as_hass_addon,
            "safe_mode": self.mass.safe_mode,
            "uptime_seconds": round(time.monotonic() - self._started_at),
            "event_loop_lag_ms": await self._measure_loop_lag(),
            "memory": memory_info,
            "data_dir_disk": disk_info,
            "counts": {
                "threads": threading.active_count(),
                "asyncio_tasks": len(asyncio.all_tasks()),
                "tracked_tasks": len(self.mass._tracked_tasks),
                "tracked_timers": len(self.mass._tracked_timers),
                "event_subscribers": len(self.mass._subscribers),
                "websocket_clients": len(self.mass.webserver.clients),
            },
        }

    async def _build_install_census(self) -> dict[str, Any]:
        """Collect install structure (never configuration values or credentials)."""
        census: dict[str, Any] = {}
        for key, builder in (
            ("providers", self._census_providers),
            ("players", self._census_players),
            ("library", self._census_library),
            ("core_config_non_default", self._census_core_config),
        ):
            try:
                result = builder()
                census[key] = await result if inspect.isawaitable(result) else result
            except Exception as err:
                census[key] = {"error": sanitize_text(f"{type(err).__name__}: {err}")}
        return census

    async def _census_providers(self) -> list[dict[str, Any]]:
        """Return the configured providers with their load/availability state."""
        providers: list[dict[str, Any]] = []
        for prov_conf in await self.mass.config.get_provider_configs():
            loaded = self.mass.get_provider(prov_conf.instance_id, return_unavailable=True)
            entry: dict[str, Any] = {
                "domain": prov_conf.domain,
                "instance_id": prov_conf.instance_id,
                "type": prov_conf.type.value,
                "enabled": prov_conf.enabled,
                "loaded": loaded is not None,
                "available": loaded.available if loaded else False,
            }
            if prov_conf.last_error is not None:
                entry["last_error"] = sanitize_text(prov_conf.last_error)
            providers.append(entry)
        providers.sort(key=lambda entry: (entry["domain"], entry["instance_id"]))
        return providers

    def _census_players(self) -> dict[str, Any]:
        """Return player counts grouped by provider and type."""
        by_provider: Counter[str] = Counter()
        by_type: Counter[str] = Counter()
        total = available = 0
        for player in self.mass.players.all_players(
            return_unavailable=True, return_disabled=True, return_protocol_players=True
        ):
            total += 1
            available += int(player.available)
            by_provider[player.provider.domain] += 1
            by_type[player.type.value] += 1
        return {
            "total": total,
            "available": available,
            "by_provider": dict(sorted(by_provider.items())),
            "by_type": dict(sorted(by_type.items())),
        }

    async def _census_library(self) -> dict[str, int]:
        """Return the library item counts per media type."""
        music = self.mass.music
        return {
            name: await controller.library_count()
            for name, controller in (
                ("albums", music.albums),
                ("artists", music.artists),
                ("audiobooks", music.audiobooks),
                ("genres", music.genres),
                ("playlists", music.playlists),
                ("podcasts", music.podcasts),
                ("radio", music.radio),
                ("tracks", music.tracks),
            )
        }

    async def _census_core_config(self) -> dict[str, list[str]]:
        """Return per core module which config keys differ from default (key names only)."""
        result: dict[str, list[str]] = {}
        for core_conf in await self.mass.config.get_core_configs(include_values=True):
            changed_keys = [
                key
                for key, entry in core_conf.values.items()
                if entry.value is not None and entry.value != entry.default_value
            ]
            if changed_keys:
                result[core_conf.domain] = sorted(changed_keys)
        return result

    async def _build_exception_summary(self) -> list[dict[str, Any]]:
        """Return the aggregated (sanitized) exceptions, most recent first."""
        _, exceptions = self._log_handler.snapshot()
        exceptions.sort(key=lambda entry: entry.last_seen, reverse=True)
        # rendering traceback text may read source files (linecache), so run off-loop
        rendered = await asyncio.get_running_loop().run_in_executor(
            None, lambda: [entry.render_traceback() for entry in exceptions]
        )
        return [
            {
                "type": entry.exc_type,
                "fingerprint": entry.fingerprint,
                "count": entry.count,
                "first_seen": _format_timestamp(entry.first_seen),
                "last_seen": _format_timestamp(entry.last_seen),
                "logger": sanitize_text(entry.logger_name),
                "level": entry.level,
                "origin": sanitize_text(entry.origin),
                "message": sanitize_text(entry.message),
                "traceback": sanitize_text(traceback_text),
            }
            for entry, traceback_text in zip(exceptions, rendered, strict=True)
        ]

    async def _collect_sections(self) -> dict[str, Any]:
        """Collect all pluggable sections (isolated, each bounded by a timeout)."""
        producers: list[tuple[str, DiagnosticsSectionCallback]] = []
        for attr_name in CORE_CONTROLLER_ATTRS:
            controller = getattr(self.mass, attr_name, None)
            if controller is None:
                continue
            # skip controllers that don't implement the optional hook
            if type(controller).get_diagnostics is CoreController.get_diagnostics:
                continue
            producers.append((f"core.{attr_name}", controller.get_diagnostics))
        for provider in sorted(self.mass.providers, key=lambda prov: prov.instance_id):
            if type(provider).get_diagnostics is Provider.get_diagnostics:
                continue
            producers.append((f"provider.{provider.instance_id}", provider.get_diagnostics))
        producers.extend(self._sections.items())
        results = await asyncio.gather(
            *(self._collect_section(name, producer) for name, producer in producers)
        )
        return {name: data for name, data in results if data is not None}

    async def _collect_section(
        self, name: str, producer: DiagnosticsSectionCallback
    ) -> tuple[str, Any]:
        """Run one section contributor with timeout/failure isolation and sanitize it."""
        try:
            async with asyncio.timeout(SECTION_TIMEOUT):
                raw_result = producer()
                result = await raw_result if inspect.isawaitable(raw_result) else raw_result
            if result is None:
                return name, None
            # roundtrip through JSON to normalize (and validate) the data,
            # then sanitize all string values in the result
            return name, sanitize_data(json_loads(json_dumps(result)))
        except Exception as err:
            return name, {"error": sanitize_text(f"{type(err).__name__}: {err}")}

    def _build_log_tail(self) -> list[dict[str, str]]:
        """Return the sanitized recent warning/error log excerpt."""
        records, _ = self._log_handler.snapshot()
        return [
            {
                "time": _format_timestamp(record.created),
                "level": record.level,
                "logger": sanitize_text(record.logger_name),
                "message": sanitize_text(record.message),
            }
            for record in records
        ]

    async def _measure_loop_lag(self) -> float:
        """Spot-sample the event loop lag in milliseconds."""
        loop = asyncio.get_running_loop()
        start = loop.time()
        await asyncio.sleep(0.1)
        return max(0.0, round((loop.time() - start - 0.1) * 1000, 1))

    def _probe_system_blocking(self) -> tuple[dict[str, Any], dict[str, Any]]:
        """Collect disk and memory info (blocking, run in executor)."""
        usage = shutil.disk_usage(self.mass.storage_path)
        disk_info = {
            "free_mb": usage.free // (1024 * 1024),
            "total_mb": usage.total // (1024 * 1024),
        }
        return disk_info, _get_memory_info()

    def _render_markdown(self, report: dict[str, Any]) -> str:
        """Render the report as markdown for direct pasting into (GitHub) issues."""
        lines = [
            "# Music Assistant diagnostics report",
            "",
            f"- Generated: {report.get('generated_at')}",
            f"- Schema version: {report.get('schema_version')}",
            "",
            f"> {report.get('redaction_notice')}",
            "",
        ]
        for key, title in (("system", "System"), ("install", "Install")):
            if (data := report.get(key)) is None:
                continue
            lines += [f"## {title}", "", "```json", json_dumps(data, indent=True), "```", ""]
        exceptions = report.get("exceptions") or []
        lines += [f"## Exceptions ({len(exceptions)} unique)", ""]
        for entry in exceptions:
            lines += [
                f"### {entry['type']} (seen {entry['count']}x)",
                "",
                f"- Logger: `{entry['logger']}` (level {entry['level']})",
                f"- Origin: `{entry['origin']}`",
                f"- First seen: {entry['first_seen']} — last seen: {entry['last_seen']}",
                "",
                "```",
                str(entry["traceback"]).rstrip(),
                "```",
                "",
            ]
        if sections := report.get("sections"):
            lines += ["## Sections", "", "```json", json_dumps(sections, indent=True), "```", ""]
        if log_tail := report.get("log_tail"):
            lines += ["## Log tail", "", "```"]
            lines += [
                f"{record['time']} {record['level']} {record['logger']}: {record['message']}"
                for record in log_tail
            ]
            lines += ["```", ""]
        return "\n".join(lines)


def _format_timestamp(timestamp: float) -> str:
    """Format a unix timestamp as a compact UTC ISO string."""
    return from_utc_timestamp(timestamp).isoformat(timespec="seconds")


def _get_memory_info() -> dict[str, Any]:
    """Return the process memory usage (rss on Linux, peak rss elsewhere)."""
    try:
        with open("/proc/self/status", encoding="ascii") as status_file:
            for line in status_file:
                if line.startswith("VmRSS:"):
                    return {"rss_mb": round(int(line.split()[1]) / 1024, 1)}
    except OSError, ValueError, IndexError:
        pass
    # ru_maxrss is in bytes on macOS, kilobytes on other platforms
    divisor = 1024 * 1024 if sys.platform == "darwin" else 1024
    return {"peak_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / divisor, 1)}
