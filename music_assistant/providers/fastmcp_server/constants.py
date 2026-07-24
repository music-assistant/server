"""Configuration keys, defaults, and constants for the MCP Server provider."""

from __future__ import annotations

# ── Server settings ────────────────────────────────────────────────────────────
CONF_REQUIRE_AUTH = "require_auth"
CONF_MOUNT_PATH = "mount_path"
CONF_EXTRA_ALLOWED_ORIGINS = "extra_allowed_origins"
CONF_ENFORCE_AUDIENCE = "enforce_audience"
CONF_REQUIRE_CONFIRMATION = "require_confirmation"
CONF_CONNECT_EXTERNAL_URL = "connect_external_url"
# Read at sub-server build time (affects tool registration), so it is
# deliberately NOT in PERMISSION_KEYS/RESOURCE_KEYS below — toggling it falls
# through to a full runtime restart rather than a tag-filter hot-swap.
CONF_LEAN_ADMIN_SCHEMA = "lean_admin_schema"
CONF_TRUST_FORWARDED_PROTO = "trust_forwarded_proto"
CONF_META_TOOL_DISCOVERY = "meta_tool_discovery"

DEFAULT_MOUNT_PATH = "/mcp/v1"

# ── Query permissions ─────────────────────────────────────────────────────────
CONF_QUERY_LIBRARY = "query_library"
CONF_QUERY_QUEUE = "query_queue"
CONF_QUERY_PLAYERS = "query_players"
CONF_QUERY_METADATA = "query_metadata"

# ── Control permissions ───────────────────────────────────────────────────────
CONF_CONTROL_PLAYBACK = "control_playback"
CONF_CONTROL_VOLUME = "control_volume"
CONF_CONTROL_PLAYERS = "control_players"
CONF_CONTROL_MEDIA = "control_media"

# ── Edit permissions ──────────────────────────────────────────────────────────
CONF_EDIT_LIBRARY = "edit_library"
CONF_EDIT_QUEUE = "edit_queue"
CONF_EDIT_PLAYLISTS = "edit_playlists"
CONF_EDIT_FAVORITES = "edit_favorites"

# ── Delete permissions ────────────────────────────────────────────────────────
CONF_DELETE_LIBRARY = "delete_library"
CONF_DELETE_QUEUE = "delete_queue"
CONF_DELETE_PLAYLISTS = "delete_playlists"
CONF_DELETE_FAVORITES = "delete_favorites"

# ── MCP Resources / Prompts toggles ───────────────────────────────────────────
CONF_RES_LIBRARY = "res_library"
CONF_RES_PLAYER = "res_player"
CONF_RES_PROMPTS = "res_prompts"

# ── Debug namespace permission flags (all off-by-default) ──────────────────
CONF_DEBUG_INSPECT = "debug_inspect"
CONF_DEBUG_LOGS = "debug_logs"
CONF_DEBUG_EVENTS = "debug_events"
CONF_DEBUG_PROVIDERS = "debug_providers"
CONF_DEBUG_RELOAD = "debug_reload"
CONF_DEBUG_EVENT_BUFFER_CAPACITY = "debug_event_buffer_capacity"

# ── Config namespace permission flags (all off-by-default) ────────────────────
CONF_CONFIG_READ = "config_read"
CONF_CONFIG_WRITE_PROVIDER = "config_write_provider"
CONF_CONFIG_WRITE_CORE = "config_write_core"
CONF_CONFIG_WRITE_PLAYER = "config_write_player"
CONF_CONFIG_WRITE_SECRET = "config_write_secret"

PERMISSION_KEYS: frozenset[str] = frozenset(
    {
        CONF_QUERY_LIBRARY,
        CONF_QUERY_QUEUE,
        CONF_QUERY_PLAYERS,
        CONF_QUERY_METADATA,
        CONF_CONTROL_PLAYBACK,
        CONF_CONTROL_VOLUME,
        CONF_CONTROL_PLAYERS,
        CONF_CONTROL_MEDIA,
        CONF_EDIT_LIBRARY,
        CONF_EDIT_QUEUE,
        CONF_EDIT_PLAYLISTS,
        CONF_EDIT_FAVORITES,
        CONF_DELETE_LIBRARY,
        CONF_DELETE_QUEUE,
        CONF_DELETE_PLAYLISTS,
        CONF_DELETE_FAVORITES,
        CONF_DEBUG_INSPECT,
        CONF_DEBUG_LOGS,
        CONF_DEBUG_EVENTS,
        CONF_DEBUG_PROVIDERS,
        CONF_DEBUG_RELOAD,
        CONF_CONFIG_READ,
        CONF_CONFIG_WRITE_PROVIDER,
        CONF_CONFIG_WRITE_CORE,
        CONF_CONFIG_WRITE_PLAYER,
        CONF_CONFIG_WRITE_SECRET,
    }
)

RESOURCE_KEYS: frozenset[str] = frozenset(
    {
        CONF_RES_LIBRARY,
        CONF_RES_PLAYER,
        CONF_RES_PROMPTS,
    }
)

# Permission-only changes can be hot-swapped without remount; everything else triggers
# a full restart of the runtime. The meta-discovery toggle qualifies because the
# transform reads it through a closure on every request.
HOT_SWAPPABLE_KEYS: frozenset[str] = PERMISSION_KEYS | RESOURCE_KEYS | {CONF_META_TOOL_DISCOVERY}
