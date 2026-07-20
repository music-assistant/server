"""
Trimmed response dataclasses used in tool replies.

Tools that need to return a Music Assistant entity use these light-weight shapes
to keep payloads small for LLM context windows. Resources, by contrast, return
the full ``music_assistant_models`` types directly because clients usually
expect a complete object when they fetch a URI.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TrackBrief:
    """A track summary for tool responses."""

    uri: str
    name: str
    artists: list[str] = field(default_factory=list)
    album: str | None = None
    duration: int | None = None
    disc_number: int | None = None
    track_number: int | None = None


@dataclass
class AlbumTracksResult:
    """An album summary plus its track listing in disc/track order."""

    album: AlbumBrief
    tracks: list[TrackBrief] = field(default_factory=list)


@dataclass
class ArtistAlbumsResult:
    """An artist summary plus their album discography."""

    artist: ArtistBrief
    albums: list[AlbumBrief] = field(default_factory=list)


@dataclass
class AlbumBrief:
    """An album summary for tool responses."""

    uri: str
    name: str
    artist: str | None = None
    year: int | None = None


@dataclass
class ArtistBrief:
    """An artist summary for tool responses."""

    uri: str
    name: str


@dataclass
class PlaylistBrief:
    """A playlist summary for tool responses."""

    uri: str
    name: str
    track_count: int | None = None
    owner: str | None = None


@dataclass
class RadioBrief:
    """A radio summary for tool responses."""

    uri: str
    name: str
    description: str | None = None


@dataclass
class PlayerBrief:
    """A player summary for tool responses."""

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
    """A queue item summary."""

    item_id: str
    name: str
    index: int
    duration: int | None = None
    artists: list[str] = field(default_factory=list)


@dataclass
class QueueBrief:
    """
    A queue summary for tool responses.

    ``item_count`` is ``None`` when the upstream queue object exposes neither
    a canonical total nor an items-count field — better to say "unknown"
    than to silently return the truncated lookahead length, which would
    under-report a non-empty queue as ``0``.
    """

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
class AddToQueueResult:
    """Confirmation of a successful ``add_to_queue`` call."""

    item_id: str
    uri: str
    name: str
    option: str
    index: int | None = None


@dataclass
class RecommendationFolderBrief:
    """One curated recommendation row (e.g. "Mood: Focus"), without its items."""

    name: str
    provider: str
    item_id: str


@dataclass
class RecommendationItemBrief:
    """One item inside a recommendation row."""

    uri: str
    name: str
    media_type: str | None = None


# ---- Debug namespace response dataclasses (spec 0005) ----


@dataclass(frozen=True, kw_only=True)
class PlayerInspect:
    """Raw mirror of a Player dataclass with state.* surfaced separately."""

    player_id: str
    raw: dict[str, Any]
    state: dict[str, Any]
    truncated: bool


@dataclass(frozen=True, kw_only=True)
class QueueInspect:
    """Raw mirror of a PlayerQueue with current_item resolved."""

    queue_id: str
    raw: dict[str, Any]
    current_item: dict[str, Any] | None
    truncated: bool


@dataclass(frozen=True, kw_only=True)
class ProviderInspect:
    """Raw mirror of a runtime Provider object + its manifest."""

    instance_id: str
    raw: dict[str, Any]
    manifest: dict[str, Any]
    truncated: bool


@dataclass(frozen=True, kw_only=True)
class LogLine:
    """One parsed record from musicassistant.log (continuation lines joined into message)."""

    timestamp: str | None
    level: str | None
    component: str | None
    message: str


@dataclass(frozen=True, kw_only=True)
class LogTailResult:
    """Result of debug_tail_log."""

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
    """Result of debug_log_stats."""

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
    """One MA event captured into the ring buffer."""

    timestamp: str
    event_type: str
    object_id: str | None
    data: Any


@dataclass(frozen=True, kw_only=True)
class EventSnapshot:
    """Result of debug_recent_events."""

    events: list[EventRecord]
    buffer_capacity: int
    total_seen: int


@dataclass(frozen=True, kw_only=True)
class EventBufferStats:
    """Result of debug_event_buffer_stats."""

    capacity: int
    current_size: int
    total_seen: int
    dropped: int
    subscribed_since: str | None
    by_type: dict[str, int]


@dataclass(frozen=True, kw_only=True)
class ProviderSummary:
    """One row of debug_list_providers."""

    instance_id: str
    domain: str
    type: str
    name: str
    available: bool
    last_error: str | None


@dataclass(frozen=True, kw_only=True)
class ProviderList:
    """Result of debug_list_providers."""

    providers: list[ProviderSummary]


@dataclass(frozen=True, kw_only=True)
class ConfigValueDump:
    """One value from a provider ConfigEntry dump (SECURE_STRING already masked upstream)."""

    key: str
    type: str
    value: Any


@dataclass(frozen=True, kw_only=True)
class ProviderConfigDump:
    """Result of debug_inspect_provider_config."""

    instance_id: str
    domain: str
    values: list[ConfigValueDump]
    truncated: bool


@dataclass(frozen=True, kw_only=True)
class RouteEntry:
    """One row of debug_list_webserver_routes."""

    method: str
    path: str
    registered_by: str | None


@dataclass(frozen=True, kw_only=True)
class RouteList:
    """Result of debug_list_webserver_routes."""

    routes: list[RouteEntry]


@dataclass(frozen=True, kw_only=True)
class PackageVersions:
    """Result of debug_list_package_versions."""

    packages: dict[str, str]


@dataclass(frozen=True, kw_only=True)
class ReloadResult:
    """Result of debug_reload_provider."""

    instance_id: str
    duration_ms: float
    new_available: bool
    last_error: str | None


@dataclass(frozen=True, kw_only=True)
class HealthSummary:
    """Result of debug_health_summary — the LLM agent's triage entry point."""

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


# ---- Config namespace response dataclasses (spec 0006) ----


@dataclass(frozen=True, kw_only=True)
class ConfigTarget:
    """One configurable target (provider, core controller, or player)."""

    target_type: str
    target_id: str
    domain: str
    name: str
    enabled: bool


@dataclass(frozen=True, kw_only=True)
class ConfigTargetList:
    """Result of config_list_targets."""

    providers: list[ConfigTarget]
    core: list[ConfigTarget]
    players: list[ConfigTarget]


@dataclass(frozen=True, kw_only=True)
class CoreConfigDump:
    """Result of config_get_core."""

    domain: str
    values: list[ConfigValueDump]
    truncated: bool


@dataclass(frozen=True, kw_only=True)
class PlayerConfigDump:
    """Result of config_get_player."""

    player_id: str
    provider: str
    values: list[ConfigValueDump]
    truncated: bool


@dataclass(frozen=True, kw_only=True)
class ConfigEntryDump:
    """One editable ConfigEntry definition + current value."""

    key: str
    type: str
    label: str | None
    default_value: Any
    required: bool
    description: str | None
    options: list[Any] | None
    range: tuple[int, int] | None
    advanced: bool
    hidden: bool
    requires_reload: bool
    depends_on: str | None
    action: str | None
    current_value: Any


@dataclass(frozen=True, kw_only=True)
class ConfigEntryList:
    """Result of config_get_entries."""

    target_type: str
    target_id: str
    entries: list[ConfigEntryDump]
    truncated: bool


@dataclass(frozen=True, kw_only=True)
class DSPConfigDump:
    """Result of config_get_dsp — mirrors music_assistant_models.dsp.DSPConfig."""

    player_id: str
    enabled: bool
    input_gain: float
    output_gain: float
    filters: list[dict[str, Any]]


@dataclass(frozen=True, kw_only=True)
class ValueChange:
    """One key's before/after in a config diff (secrets masked both sides)."""

    key: str
    before: Any
    after: Any
    secret: bool


@dataclass(frozen=True, kw_only=True)
class DiffResult:
    """A dry-run config diff."""

    target_type: str
    target_id: str
    changes: list[ValueChange]


@dataclass(frozen=True, kw_only=True)
class SetValueResult:
    """Result of config_set_*_value."""

    target_type: str
    target_id: str
    key: str
    applied: bool
    requires_reload: bool
    audit_log_id: str
    diff: DiffResult | None


@dataclass(frozen=True, kw_only=True)
class SaveResult:
    """Result of config_save_* (bulk)."""

    target_type: str
    target_id: str
    applied: bool
    changes: list[ValueChange]
    requires_reload: bool
    audit_log_id: str
    diff: DiffResult | None


@dataclass(frozen=True, kw_only=True)
class ActionResult:
    """Result of config_trigger_provider_action."""

    instance_id: str
    action_key: str
    new_entries: list[ConfigEntryDump]
    extra_data: dict[str, Any]
    audit_log_id: str
