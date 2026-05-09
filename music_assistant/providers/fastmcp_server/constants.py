"""Configuration keys, defaults, and constants for the MCP Server provider."""

from __future__ import annotations

# ── Server settings ────────────────────────────────────────────────────────────
CONF_REQUIRE_AUTH = "require_auth"
CONF_MOUNT_PATH = "mount_path"
CONF_EXTRA_ALLOWED_ORIGINS = "extra_allowed_origins"
CONF_ENFORCE_AUDIENCE = "enforce_audience"
CONF_REQUIRE_CONFIRMATION = "require_confirmation"

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
# a full restart of the runtime.
HOT_SWAPPABLE_KEYS: frozenset[str] = PERMISSION_KEYS | RESOURCE_KEYS
