"""Bounded response dataclasses retained by resources and native MA commands."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PlayerBrief:
    """Compact player state exposed by player resources."""

    player_id: str
    name: str
    state: str
    volume_level: int | None = None
    powered: bool = True
    current_item: str | None = None
    available: bool = True
    enabled: bool = True
    needs_setup: bool = False
    active_group: str | None = None
    synced_to: str | None = None
    volume_muted: bool | None = None
    group_volume: int | None = None
    group_volume_muted: bool | None = None
    external_source: str | None = None


@dataclass
class QueueItemBrief:
    """Compact queue item exposed by queue resources."""

    item_id: str
    name: str
    index: int
    duration: int | None = None
    artists: list[str] = field(default_factory=list)


@dataclass
class QueueBrief:
    """Compact queue snapshot exposed by queue resources."""

    queue_id: str
    current_index: int | None
    item_count: int | None
    shuffle: bool
    repeat: str
    items: list[QueueItemBrief] = field(default_factory=list)
    available: bool = True
    index_in_buffer: int | None = None
    next_insertable_index: int | None = None
    items_start_index: int = 0


@dataclass
class RemoveFromQueueResult:
    """Per-item outcome for the provider's safe queue removal command."""

    removed: list[str] = field(default_factory=list)
    skipped_played: list[str] = field(default_factory=list)
    skipped_buffered: list[str] = field(default_factory=list)
    not_found: list[str] = field(default_factory=list)


@dataclass(frozen=True, kw_only=True)
class LogLine:
    """One parsed, redacted Music Assistant log record."""

    timestamp: str | None
    level: str | None
    component: str | None
    message: str


@dataclass(frozen=True, kw_only=True)
class LogTailResult:
    """Bounded page of parsed Music Assistant log records."""

    log_path: str
    lines: list[LogLine]
    bytes_scanned: int
    truncated: bool
    has_more: bool = False
    response_truncated: bool = False
    next_call_hint: str | None = None


@dataclass(frozen=True, kw_only=True)
class ComponentCount:
    """Record count for one log component."""

    component: str
    count: int


@dataclass(frozen=True, kw_only=True)
class LogStatsResult:
    """Bounded aggregate statistics for Music Assistant logs."""

    log_path: str
    window_seconds: int | None
    total_records: int
    level_counts: dict[str, int]
    top_components: list[ComponentCount]
    first_timestamp: str | None
    last_timestamp: str | None
    bytes_scanned: int
    truncated: bool


@dataclass(frozen=True, kw_only=True)
class EventRecord:
    """One event retained by the optional in-memory event buffer."""

    timestamp: str
    event_type: str
    object_id: str | None
    data: Any


@dataclass(frozen=True, kw_only=True)
class EventSnapshot:
    """Bounded snapshot of retained Music Assistant events."""

    events: list[EventRecord]
    buffer_capacity: int
    total_seen: int


@dataclass(frozen=True, kw_only=True)
class EventBufferStats:
    """Counters for the optional in-memory event buffer."""

    capacity: int
    current_size: int
    total_seen: int
    dropped: int
    subscribed_since: str | None
    by_type: dict[str, int]


@dataclass(frozen=True, kw_only=True)
class ProviderSummary:
    """Compact provider row used by health diagnostics."""

    instance_id: str
    domain: str
    type: str
    name: str
    available: bool
    last_error: str | None


@dataclass(frozen=True, kw_only=True)
class RouteEntry:
    """One registered Music Assistant HTTP route."""

    method: str
    path: str
    registered_by: str | None


@dataclass(frozen=True, kw_only=True)
class RouteList:
    """Registered Music Assistant HTTP routes."""

    routes: list[RouteEntry]


@dataclass(frozen=True, kw_only=True)
class PackageVersions:
    """Versions of the bounded diagnostics package allowlist."""

    packages: dict[str, str]


@dataclass(frozen=True, kw_only=True)
class HealthSummary:
    """Provider, queue, event, log, and dynamic-catalog health rollup."""

    providers_loaded: int
    providers_disabled: int
    providers_error: int
    providers_error_details: list[ProviderSummary]
    queues_total: int
    queues_with_active_playback: int
    queues_with_errors: int
    events_per_min_by_type: dict[str, float] | None
    log_errors_last_5min: int | None
    disabled_capabilities: list[str]
    dynamic_catalog: dict[str, Any] | None = None
    policy_schema_version: int = 2
    policy_profile: str = "Safe queries"
    token_resolution_failures: int = 0
    event_buffer_active: bool = False
    performance: dict[str, int | float] = field(default_factory=dict)
