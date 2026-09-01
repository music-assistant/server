"""Plain, bounded debug command handlers for MA's native API registry."""
# ruff: noqa: TID252 -- provider source is transplanted under the MA package.

from __future__ import annotations

import asyncio
import importlib.metadata
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from fastmcp.exceptions import ToolError

from ..debug.event_buffer import EventBuffer
from ..debug.log_reader import SafeLogTail
from ..models import (
    EventBufferStats,
    EventSnapshot,
    HealthSummary,
    LogStatsResult,
    LogTailResult,
    PackageVersions,
    ProviderSummary,
    RouteEntry,
    RouteList,
)

_TRACKED_PACKAGES = (
    "music_assistant",
    "music_assistant_models",
    "fastmcp",
    "aiohttp",
    "mashumaro",
)


async def tail_log(
    mass: Any,
    *,
    lines: int = 200,
    level: str | None = None,
    component_regex: str | None = None,
    search: str | None = None,
    since_seconds: int | None = None,
    before: str | None = None,
    name: str = "musicassistant.log",
) -> LogTailResult:
    """Tail a redacted, byte-bounded MA log page without blocking MA's loop."""
    return await asyncio.to_thread(
        SafeLogTail(mass).tail,
        lines=lines,
        level=level,
        component_regex=component_regex,
        search=search,
        since_seconds=since_seconds,
        before=before,
        name=name,
    )


async def log_stats(
    mass: Any, *, since_seconds: int | None = None, name: str = "musicassistant.log"
) -> LogStatsResult:
    """Return bounded log aggregates without blocking MA's loop."""
    return await asyncio.to_thread(SafeLogTail(mass).stats, since_seconds=since_seconds, name=name)


async def recent_events(
    buffer: EventBuffer | None,
    *,
    limit: int = 100,
    event_types: list[str] | None = None,
    id_filter: str | None = None,
    since_seconds: int | None = None,
) -> EventSnapshot:
    """Return a bounded snapshot of the optional event ring buffer."""
    if buffer is None:
        return EventSnapshot(events=[], buffer_capacity=0, total_seen=0)
    stats = buffer.stats()
    return EventSnapshot(
        events=buffer.snapshot(
            limit=limit,
            event_types=event_types,
            id_filter=id_filter,
            since_seconds=since_seconds,
        ),
        buffer_capacity=stats.capacity,
        total_seen=stats.total_seen,
    )


async def event_buffer_stats(buffer: EventBuffer | None) -> EventBufferStats:
    """Return ring-buffer counters, including a stable disabled result."""
    if buffer is not None:
        return buffer.stats()
    return EventBufferStats(
        capacity=0,
        current_size=0,
        total_seen=0,
        dropped=0,
        subscribed_since=None,
        by_type={},
    )


async def health(
    mass: Any,
    *,
    buffer: EventBuffer | None,
    logs_enabled: bool,
    dynamic_diagnostics_provider: Callable[[], Mapping[str, Any]] | None = None,
    policy_schema_version: int = 2,
    policy_profile: str = "Safe queries",
    token_resolution_failures: int = 0,
) -> HealthSummary:
    """Roll up provider, queue, event, and permitted log diagnostics."""
    providers = list(getattr(mass, "providers", []))
    loaded = sum(1 for provider in providers if getattr(provider, "available", False))
    disabled = sum(1 for provider in providers if not getattr(provider, "enabled", True))
    errors = [
        ProviderSummary(
            instance_id=getattr(provider, "instance_id", ""),
            domain=getattr(provider, "domain", ""),
            type=str(getattr(getattr(provider, "type", None), "value", "unknown")),
            name=getattr(provider, "name", "") or getattr(provider, "domain", ""),
            available=bool(getattr(provider, "available", False)),
            last_error=getattr(provider, "last_error", None),
        )
        for provider in providers
        if getattr(provider, "last_error", None)
    ]
    try:
        queues = list(mass.player_queues.all())
    except AttributeError, TypeError:
        queues = []
    disabled_capabilities: list[str] = []
    events_per_min: dict[str, float] | None = None
    stats = buffer.stats() if buffer is not None else None
    if stats is None or stats.subscribed_since is None:
        disabled_capabilities.append("DEBUG_EVENTS")
    else:
        subscribed_at = datetime.fromisoformat(stats.subscribed_since)
        from music_assistant.helpers.datetime import now as ma_now  # noqa: PLC0415

        elapsed = max(1.0 / 60, (ma_now() - subscribed_at).total_seconds() / 60.0)
        events_per_min = {kind: round(count / elapsed, 2) for kind, count in stats.by_type.items()}
    log_errors: int | None = None
    if not logs_enabled:
        disabled_capabilities.append("DEBUG_LOGS")
    else:
        try:
            log_errors = await asyncio.to_thread(SafeLogTail(mass).count_errors_last_5min)
        except Exception:
            disabled_capabilities.append("DEBUG_LOGS")
    dynamic_diagnostics = (
        dict(dynamic_diagnostics_provider()) if dynamic_diagnostics_provider is not None else None
    )
    performance = (
        dict(dynamic_diagnostics.get("performance", {}))
        if dynamic_diagnostics is not None
        and isinstance(dynamic_diagnostics.get("performance"), Mapping)
        else {}
    )
    return HealthSummary(
        providers_loaded=loaded,
        providers_disabled=disabled,
        providers_error=len(errors),
        providers_error_details=errors,
        queues_total=len(queues),
        queues_with_active_playback=sum(
            1 for queue in queues if getattr(queue, "state", None) == "playing"
        ),
        queues_with_errors=sum(
            1
            for queue in queues
            if getattr(queue, "state", None) == "error" or not getattr(queue, "available", True)
        ),
        events_per_min_by_type=events_per_min,
        log_errors_last_5min=log_errors,
        disabled_capabilities=disabled_capabilities,
        dynamic_catalog=dynamic_diagnostics,
        policy_schema_version=policy_schema_version,
        policy_profile=policy_profile,
        token_resolution_failures=token_resolution_failures,
        event_buffer_active=stats is not None and stats.subscribed_since is not None,
        performance=performance,
    )


async def routes(mass: Any) -> RouteList:
    """Return the MA webserver route table when that private surface exists."""
    try:
        app = mass.webserver._server.app
        result = []
        for route in app.router.routes():
            resource = getattr(route, "resource", None)
            path = str(getattr(resource, "canonical", "")) if resource else ""
            result.append(
                RouteEntry(
                    method=str(getattr(route, "method", "*")),
                    path=path,
                    registered_by=_attribute_route(path),
                )
            )
        return RouteList(routes=result)
    except AttributeError as exc:
        raise ToolError("webserver routes are unavailable in this MA build") from exc


async def packages() -> PackageVersions:
    """Return versions for the fixed diagnostics allowlist."""
    versions: dict[str, str] = {}
    for package in _TRACKED_PACKAGES:
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = "<not installed>"
    return PackageVersions(packages=versions)


def _attribute_route(path: str) -> str | None:
    if path.startswith("/mcp/"):
        return "fastmcp_server"
    if path.startswith("/.well-known/"):
        return "fastmcp_server (well-known)"
    if path.startswith("/api/"):
        return "music_assistant (api)"
    return None
