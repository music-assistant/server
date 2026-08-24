"""
FastMCP sub-server for debug / troubleshooting tools.

Spec: ``specs/inprogress/0005-debug-namespace.md``.

All tools in this module are gated by off-by-default ConfigEntries
(see ``provider/config.py``). With no tag enabled the entire namespace
is invisible to MCP clients via ``TagFilterMiddleware``.
"""
# ruff: noqa: TID252  -- relative imports are the canonical MA-provider pattern.

from __future__ import annotations

import asyncio
import importlib.metadata
import logging
import time
from typing import TYPE_CHECKING, Any

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from ..debug.event_buffer import EventBuffer
from ..debug.inspect_serializer import dump
from ..debug.log_reader import SafeLogTail
from ..models import (
    ConfigValueDump,
    EventBufferStats,
    EventSnapshot,
    HealthSummary,
    LogStatsResult,
    LogTailResult,
    PackageVersions,
    PlayerInspect,
    ProviderConfigDump,
    ProviderInspect,
    ProviderList,
    ProviderSummary,
    QueueInspect,
    ReloadResult,
    RouteEntry,
    RouteList,
)
from ..tags import Tag
from ._common import (
    TIMEOUT_FAST,
    TIMEOUT_INTERACTIVE,
    confirm_or_raise,
    lean_schema_view,
)

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


LOGGER = logging.getLogger("music_assistant.providers.fastmcp_server.debug")

_PAYLOAD_CAP_BYTES = 256 * 1024

_TRACKED_PACKAGES = (
    "music_assistant",
    "music_assistant_models",
    "fastmcp",
    "aiohttp",
    "mashumaro",
)

_RELOAD_POLL_SECONDS = 5.0
_RELOAD_POLL_INTERVAL = 0.1


def _safe_get(obj: Any, name: str, default: Any = None) -> Any:
    """
    Like getattr(obj, name, default) but also swallows property exceptions.

    The inspect tools claim to work on broken/unavailable entities — a raising
    @property on the inspected object must not crash the tool. The serializer
    itself is defensive at the dataclass-field level; this helper covers the
    one-shot tool-level accesses (state, manifest, current_item).
    """
    try:
        return getattr(obj, name, default)
    except Exception:
        return default


def _readonly(title: str) -> ToolAnnotations:
    return ToolAnnotations(
        title=title,
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )


def _register_reload_tool(
    sub: FastMCP, mass: MusicAssistant, *, require_confirmation: bool, reload_lock: asyncio.Lock
) -> None:
    @sub.tool(
        tags={Tag.DEBUG_RELOAD},
        annotations=ToolAnnotations(
            title="Reload provider",
            destructiveHint=True,
            idempotentHint=False,
        ),
        timeout=TIMEOUT_INTERACTIVE,
    )
    async def reload_provider(instance_id: str, ctx: Context | None = None) -> ReloadResult:
        """
        Unload and reload a configured provider instance.

        INTERRUPTS ACTIVE STREAMS on the affected provider. Confirmation is
        required by default. See also: debug_inspect_provider to verify the
        reload landed; debug_tail_log for the reload's own log lines.

        :param instance_id: Provider instance identifier.
        :param ctx: FastMCP context — populated automatically by the server.
        """
        await confirm_or_raise(
            ctx,
            (
                f"Reload provider {instance_id!r}? "
                "Active playback on this provider will be interrupted."
            ),
            enabled=require_confirmation,
        )
        try:
            conf = await mass.config.get_provider_config(instance_id)
        except Exception as exc:
            raise ToolError(f"provider instance_id={instance_id!r} not configured") from exc

        async with reload_lock:
            LOGGER.info(
                "MCP debug_reload_provider triggered: instance_id=%s",
                instance_id,
            )
            t0 = time.monotonic()
            load_error: Exception | None = None
            try:
                # Private but the only reload primitive — see spec 0005 "Known private-API carve-outs".
                await mass._load_provider(conf)
            except Exception as exc:
                load_error = exc

            if load_error is not None:
                return ReloadResult(
                    instance_id=instance_id,
                    duration_ms=(time.monotonic() - t0) * 1000,
                    new_available=False,
                    last_error=str(load_error),
                )

            deadline = time.monotonic() + _RELOAD_POLL_SECONDS
            prov = None
            while time.monotonic() < deadline:
                prov = mass.get_provider(instance_id)
                if prov and getattr(prov, "available", False):
                    break
                await asyncio.sleep(_RELOAD_POLL_INTERVAL)
            available = bool(prov and getattr(prov, "available", False))
            last_error = getattr(prov, "last_error", None) if prov else "reload timed out"
            return ReloadResult(
                instance_id=instance_id,
                duration_ms=(time.monotonic() - t0) * 1000,
                new_available=available,
                last_error=last_error,
            )


def _register_logs_tool(sub: FastMCP, mass: MusicAssistant) -> None:
    tail = SafeLogTail(mass)

    @sub.tool(
        tags={Tag.DEBUG_LOGS},
        annotations=_readonly("Tail MA log"),
        timeout=TIMEOUT_FAST,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def tail_log(
        lines: int = 200,
        level: str | None = None,
        component_regex: str | None = None,
        search: str | None = None,
        since_seconds: int | None = None,
        before: str | None = None,
        name: str = "musicassistant.log",
    ) -> LogTailResult:
        """
        Return the last N matching log records with optional filters.

        Multi-line records (tracebacks) are returned whole, and ``lines``
        counts *matching records* — filters apply before the limit, so
        ``lines=5, level="ERROR"`` is "the 5 most recent errors". When the
        page is incomplete, ``has_more`` / ``response_truncated`` are set and
        ``next_call_hint`` carries a ready-to-use follow-up call. To watch the
        log "live", re-call periodically with ``since_seconds`` covering the
        polling gap. Bearer tokens and common secret patterns are redacted.
        See also: debug_log_stats for a cheap aggregate view before pulling
        raw records; debug_recent_events for state transitions.

        :param lines: Number of matching records to return (clamped to [1, 2000]).
        :param level: Minimum severity, case-insensitive — e.g. ``"warning"``
            returns WARNING, ERROR and CRITICAL records.
        :param component_regex: Optional regex matched against the component name.
        :param search: Optional case-insensitive regex matched against the full
            record text, including traceback lines.
        :param since_seconds: When set, only records within this many seconds
            of "now" are returned.
        :param before: Paging cursor — an ISO timestamp, or the exact
            ``offset:<n>`` value from a previous result's ``next_call_hint``
            (the offset form is lossless when many records share a timestamp).
        :param name: Log file basename within ``$HOME/.musicassistant/``. Only the
            canonical log and its rotated siblings (``.log.1`` … ``.log.5``) are
            allowed.
        """
        # Offload the synchronous file scan (up to the 10 MB cap) to a worker
        # thread so it never stalls MA's single event loop.
        return await asyncio.to_thread(
            tail.tail,
            lines=lines,
            level=level,
            component_regex=component_regex,
            search=search,
            since_seconds=since_seconds,
            before=before,
            name=name,
        )

    @sub.tool(
        tags={Tag.DEBUG_LOGS},
        annotations=_readonly("Log statistics"),
        timeout=TIMEOUT_FAST,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def log_stats(
        since_seconds: int | None = None,
        name: str = "musicassistant.log",
    ) -> LogStatsResult:
        """
        Aggregate view of the log: record counts per level, top components, time range.

        Use this before ``debug_tail_log`` to scope a problem cheaply — the
        counts show whether (and where) errors exist without spending context
        on raw lines. Scans at most 10 MB from the end of the file
        (``truncated`` is set when the cap fires).

        :param since_seconds: Restrict the window to the last N seconds.
        :param name: Log file basename within ``$HOME/.musicassistant/``. Only the
            canonical log and its rotated siblings (``.log.1`` … ``.log.5``) are
            allowed.
        """
        return await asyncio.to_thread(tail.stats, since_seconds=since_seconds, name=name)


def build_debug_server(
    mass: MusicAssistant,
    *,
    require_confirmation: bool = True,
    event_buffer: EventBuffer | None = None,
    logs_enabled: bool = True,
    reload_lock: asyncio.Lock | None = None,
    lean_schema: bool = False,
) -> FastMCP:
    """
    Build the ``debug`` sub-server.

    :param mass: MusicAssistant instance.
    :param require_confirmation: When True (default), ``debug_reload_provider``
        elicits explicit confirmation from the MCP client before reloading.
    :param event_buffer: A started ``EventBuffer`` instance. When ``None``
        the events tools still mount but report ``current_size=0`` and
        empty snapshots — useful for tests that only exercise other groups.
    :param logs_enabled: Whether the ``DEBUG_LOGS`` capability is enabled. When
        ``False``, ``debug_health_summary`` skips its log-error read and reports
        ``DEBUG_LOGS`` as a disabled capability instead.
    :param reload_lock: Lock serialising ``debug_reload_provider`` for this
        runtime. Defaults to a fresh per-server lock so independent servers
        (e.g. test instances) never serialise against one another.
    :param lean_schema: When True, tools omit their ``outputSchema`` to shrink
        the namespace's context footprint for hosts without tool-search.
    """
    sub = FastMCP(name="debug")
    target = lean_schema_view(sub) if lean_schema else sub
    _register_inspect_tools(target, mass)
    _register_logs_tool(target, mass)
    _register_events_tools(target, mass, event_buffer)
    _register_providers_tools(target, mass)
    _register_reload_tool(
        target,
        mass,
        require_confirmation=require_confirmation,
        reload_lock=reload_lock if reload_lock is not None else asyncio.Lock(),
    )
    _register_health_tool(target, mass, buffer=event_buffer, logs_enabled=logs_enabled)
    return sub


def _register_inspect_tools(sub: FastMCP, mass: MusicAssistant) -> None:
    @sub.tool(
        tags={Tag.DEBUG_INSPECT},
        annotations=_readonly("Inspect raw player state"),
        timeout=TIMEOUT_FAST,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def inspect_player(player_id: str) -> PlayerInspect:
        """
        Return the raw runtime state of a player, including state.* fields the brief omits.

        Works for unavailable and disabled players — that is the point.
        See also: debug_recent_events with id_filter=<player_id> for transitions,
        debug_tail_log for the textual context.

        :param player_id: Identifier of the player to inspect.
        """
        player = mass.players.get_player(player_id)
        if player is None:
            raise ToolError(f"player_id={player_id!r} not found")
        state_obj = _safe_get(player, "state", None)
        raw, raw_trunc = dump(
            player,
            max_total_bytes=_PAYLOAD_CAP_BYTES,
            return_truncated=True,
        )
        if state_obj is not None:
            state, state_trunc = dump(
                state_obj,
                max_total_bytes=_PAYLOAD_CAP_BYTES,
                return_truncated=True,
            )
        else:
            state, state_trunc = {}, False
        # raw includes state; surface it again under .state for convenience.
        if isinstance(raw, dict) and "state" in raw:
            raw.pop("state", None)
        return PlayerInspect(
            player_id=player_id,
            raw=raw,
            state=state,
            truncated=bool(raw_trunc or state_trunc),
        )

    @sub.tool(
        tags={Tag.DEBUG_INSPECT},
        annotations=_readonly("Inspect raw queue state"),
        timeout=TIMEOUT_FAST,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def inspect_queue(queue_id: str) -> QueueInspect:
        """
        Return the raw runtime state of a PlayerQueue plus the current_item resolved.

        See also: debug_inspect_player for the queue's owning player,
        debug_recent_events with id_filter=<queue_id> for transitions.

        :param queue_id: Identifier of the queue to inspect.
        """
        queue = mass.player_queues.get(queue_id)
        if queue is None:
            raise ToolError(f"queue_id={queue_id!r} not found")
        raw, raw_trunc = dump(queue, max_total_bytes=_PAYLOAD_CAP_BYTES, return_truncated=True)
        current = _safe_get(queue, "current_item", None)
        current_payload, current_trunc = (
            dump(current, max_total_bytes=_PAYLOAD_CAP_BYTES, return_truncated=True)
            if current is not None
            else (None, False)
        )
        return QueueInspect(
            queue_id=queue_id,
            raw=raw,
            current_item=current_payload,
            truncated=bool(raw_trunc or current_trunc),
        )

    @sub.tool(
        tags={Tag.DEBUG_INSPECT},
        annotations=_readonly("Inspect raw provider state"),
        timeout=TIMEOUT_FAST,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def inspect_provider(instance_id: str) -> ProviderInspect:
        """
        Return the raw runtime state of a configured provider plus its manifest.

        See also: debug_inspect_provider_config for masked configuration,
        debug_list_webserver_routes for the routes this provider registered,
        debug_reload_provider to restart it.

        :param instance_id: Identifier of the provider instance to inspect.
        """
        prov = mass.get_provider(instance_id)
        if prov is None:
            raise ToolError(f"provider instance_id={instance_id!r} not configured")
        manifest = _safe_get(prov, "manifest", None)
        raw, raw_trunc = dump(prov, max_total_bytes=_PAYLOAD_CAP_BYTES, return_truncated=True)
        manifest_payload, manifest_trunc = (
            dump(manifest, max_total_bytes=_PAYLOAD_CAP_BYTES, return_truncated=True)
            if manifest is not None
            else ({}, False)
        )
        if isinstance(raw, dict):
            raw.pop("manifest", None)
        return ProviderInspect(
            instance_id=instance_id,
            raw=raw,
            manifest=manifest_payload,
            truncated=bool(raw_trunc or manifest_trunc),
        )


def _register_events_tools(
    sub: FastMCP,
    mass: MusicAssistant,  # noqa: ARG001 -- reserved for symmetry/future use
    buffer: EventBuffer | None,
) -> None:
    @sub.tool(
        tags={Tag.DEBUG_EVENTS},
        annotations=_readonly("Read recent MA events"),
        timeout=TIMEOUT_FAST,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def recent_events(
        limit: int = 100,
        event_types: list[str] | None = None,
        id_filter: str | None = None,
        since_seconds: int | None = None,
    ) -> EventSnapshot:
        """
        Return the most recent events captured into the in-memory ring buffer.

        See also: debug_tail_log for the textual context around an event timestamp.

        :param limit: Maximum events to return (clamped to [1, 1000]).
        :param event_types: Optional list of event-type strings to include.
        :param id_filter: Optional ``object_id`` to filter by.
        :param since_seconds: When set, only events within this many seconds of
            "now" are returned.
        """
        if buffer is None:
            return EventSnapshot(events=[], buffer_capacity=0, total_seen=0)
        events = buffer.snapshot(
            limit=limit,
            event_types=event_types,
            id_filter=id_filter,
            since_seconds=since_seconds,
        )
        stats = buffer.stats()
        return EventSnapshot(
            events=events,
            buffer_capacity=stats.capacity,
            total_seen=stats.total_seen,
        )

    @sub.tool(
        tags={Tag.DEBUG_EVENTS},
        annotations=_readonly("Event buffer stats"),
        timeout=TIMEOUT_FAST,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def event_buffer_stats() -> EventBufferStats:
        """
        Return introspection counters for the event ring buffer.

        Use this to distinguish "no events match" from "events were dropped
        before you asked". ``dropped`` is non-zero whenever the buffer overflowed.
        """
        if buffer is None:
            return EventBufferStats(
                capacity=0,
                current_size=0,
                total_seen=0,
                dropped=0,
                subscribed_since=None,
                by_type={},
            )
        return buffer.stats()


def _register_providers_tools(sub: FastMCP, mass: MusicAssistant) -> None:
    @sub.tool(
        tags={Tag.DEBUG_PROVIDERS},
        annotations=_readonly("List configured providers"),
        timeout=TIMEOUT_FAST,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def list_providers() -> ProviderList:
        """
        Roll-up of every configured provider.

        See also: debug_inspect_provider for the full runtime dump,
        debug_inspect_provider_config for the masked configuration,
        debug_health_summary for triage.
        """
        summaries: list[ProviderSummary] = []
        for prov in getattr(mass, "providers", []):
            ptype = getattr(getattr(prov, "type", None), "value", None) or str(
                getattr(prov, "type", "unknown")
            )
            summaries.append(
                ProviderSummary(
                    instance_id=getattr(prov, "instance_id", ""),
                    domain=getattr(prov, "domain", ""),
                    type=ptype,
                    name=getattr(prov, "name", "") or getattr(prov, "domain", ""),
                    available=bool(getattr(prov, "available", False)),
                    last_error=getattr(prov, "last_error", None),
                )
            )
        return ProviderList(providers=summaries)

    @sub.tool(
        tags={Tag.DEBUG_PROVIDERS},
        annotations=_readonly("Inspect provider config (masked)"),
        timeout=TIMEOUT_FAST,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def inspect_provider_config(instance_id: str) -> ProviderConfigDump:
        """
        Dump a provider's stored ConfigEntry values.

        SECURE_STRING values are replaced by MA's SECURE_STRING_SUBSTITUTE
        sentinel via ``__post_serialize__`` in ``music_assistant_models`` —
        this tool carries no masking logic of its own.

        :param instance_id: The provider instance identifier.
        """
        try:
            config = await mass.config.get_provider_config(instance_id)
        except Exception as exc:
            raise ToolError(f"provider instance_id={instance_id!r} not configured") from exc
        raw = config.to_dict()
        values: list[ConfigValueDump] = []
        truncated = False
        running_bytes = 0
        for key, entry in raw.get("values", {}).items():
            value = entry.get("value")
            etype = entry.get("type", "unknown")
            payload_size = len(str(value)) + len(key) + len(etype) + 16
            if running_bytes + payload_size > _PAYLOAD_CAP_BYTES:
                truncated = True
                break
            running_bytes += payload_size
            values.append(ConfigValueDump(key=str(key), type=str(etype), value=value))
        return ProviderConfigDump(
            instance_id=instance_id,
            domain=raw.get("domain", ""),
            values=values,
            truncated=truncated,
        )

    @sub.tool(
        tags={Tag.DEBUG_PROVIDERS},
        annotations=_readonly("List webserver routes"),
        timeout=TIMEOUT_FAST,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def list_webserver_routes() -> RouteList:
        """
        Enumerate the HTTP routes registered on MA's webserver.

        Includes both dynamic (provider-registered) and static routes.
        Reaches into ``webserver._server.app.router`` — single documented
        private-API touch. See spec 0005.
        """
        routes: list[RouteEntry] = []
        try:
            inner_app = mass.webserver._server.app  # type: ignore[attr-defined]
            for route in inner_app.router.routes():
                method = str(getattr(route, "method", "*"))
                resource = getattr(route, "resource", None)
                path = str(getattr(resource, "canonical", "")) if resource else ""
                routes.append(
                    RouteEntry(
                        method=method,
                        path=path,
                        registered_by=_attribute_route(path),
                    )
                )
        except AttributeError as exc:
            raise ToolError("webserver routes are unavailable in this MA build") from exc
        return RouteList(routes=routes)

    @sub.tool(
        tags={Tag.DEBUG_PROVIDERS},
        annotations=_readonly("List installed package versions"),
        timeout=TIMEOUT_FAST,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def list_package_versions() -> PackageVersions:
        """
        Return installed versions of the key packages backing the MCP provider and MA.

        Useful for upstream bug reports.
        """
        out: dict[str, str] = {}
        for pkg in _TRACKED_PACKAGES:
            try:
                out[pkg] = importlib.metadata.version(pkg)
            except importlib.metadata.PackageNotFoundError:
                out[pkg] = "<not installed>"
        return PackageVersions(packages=out)


def _register_health_tool(
    sub: FastMCP, mass: MusicAssistant, *, buffer: EventBuffer | None, logs_enabled: bool
) -> None:
    @sub.tool(
        tags={Tag.DEBUG_PROVIDERS},
        annotations=_readonly("Health summary roll-up"),
        timeout=TIMEOUT_FAST,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def health_summary() -> HealthSummary:
        """
        Entry-point triage tool — one read returns a roll-up of provider state, queue counts, event rate, and log error count.

        If a section flags errors, drill into: debug_inspect_provider for provider
        errors, debug_inspect_queue for queue errors, debug_tail_log for the
        recent ERROR log lines. Fields whose capability is disabled show as
        ``None`` with the tag name listed in ``disabled_capabilities``.
        """
        providers = list(getattr(mass, "providers", []))
        loaded = sum(1 for p in providers if getattr(p, "available", False))
        disabled = sum(1 for p in providers if not getattr(p, "enabled", True))
        error_details: list[ProviderSummary] = []
        for p in providers:
            if getattr(p, "last_error", None):
                ptype = getattr(getattr(p, "type", None), "value", "unknown")
                error_details.append(
                    ProviderSummary(
                        instance_id=getattr(p, "instance_id", ""),
                        domain=getattr(p, "domain", ""),
                        type=str(ptype),
                        name=getattr(p, "name", "") or getattr(p, "domain", ""),
                        available=bool(getattr(p, "available", False)),
                        last_error=getattr(p, "last_error", None),
                    )
                )

        try:
            queues = list(mass.player_queues.all())
        except AttributeError, TypeError:
            queues = []
        queues_active = sum(1 for q in queues if getattr(q, "state", None) == "playing")
        queues_errors = sum(
            1
            for q in queues
            if getattr(q, "state", None) == "error" or not getattr(q, "available", True)
        )

        disabled_capabilities: list[str] = []
        events_per_min: dict[str, float] | None = None
        if buffer is None:
            disabled_capabilities.append("DEBUG_EVENTS")
        else:
            stats = buffer.stats()
            if stats.subscribed_since is None:
                disabled_capabilities.append("DEBUG_EVENTS")
            else:
                from datetime import datetime  # noqa: PLC0415

                from music_assistant.helpers.datetime import now as ma_now  # noqa: PLC0415

                subscribed_at = datetime.fromisoformat(stats.subscribed_since)
                elapsed_min = max(
                    1.0 / 60,
                    (ma_now() - subscribed_at).total_seconds() / 60.0,
                )
                events_per_min = {
                    et: round(count / elapsed_min, 2) for et, count in stats.by_type.items()
                }

        log_errors: int | None = None
        if not logs_enabled:
            # DEBUG_LOGS is off — do not read the log file (mirrors the events
            # gate above; reading here would bypass the disabled permission).
            disabled_capabilities.append("DEBUG_LOGS")
        else:
            try:
                # Offload the synchronous scan off MA's event loop (see tail_log).
                log_errors = await asyncio.to_thread(SafeLogTail(mass).count_errors_last_5min)
            except Exception:
                disabled_capabilities.append("DEBUG_LOGS")

        return HealthSummary(
            providers_loaded=loaded,
            providers_disabled=disabled,
            providers_error=len(error_details),
            providers_error_details=error_details,
            queues_total=len(queues),
            queues_with_active_playback=queues_active,
            queues_with_errors=queues_errors,
            events_per_min_by_type=events_per_min,
            log_errors_last_5min=log_errors,
            disabled_capabilities=disabled_capabilities,
        )


def _attribute_route(path: str) -> str | None:
    """Best-effort: map a route path back to who registered it by path prefix."""
    if path.startswith("/mcp/"):
        return "fastmcp_server"
    if path.startswith("/.well-known/"):
        return "fastmcp_server (well-known)"
    if path.startswith("/api/"):
        return "music_assistant (api)"
    return None
