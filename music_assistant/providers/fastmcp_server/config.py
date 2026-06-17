"""ConfigEntry schema for the MCP Server provider."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

from .constants import (
    CONF_CONFIG_READ,
    CONF_CONFIG_WRITE_CORE,
    CONF_CONFIG_WRITE_PLAYER,
    CONF_CONFIG_WRITE_PROVIDER,
    CONF_CONFIG_WRITE_SECRET,
    CONF_CONNECT_EXTERNAL_URL,
    CONF_CONTROL_MEDIA,
    CONF_CONTROL_PLAYBACK,
    CONF_CONTROL_PLAYERS,
    CONF_CONTROL_VOLUME,
    CONF_DEBUG_EVENT_BUFFER_CAPACITY,
    CONF_DEBUG_EVENTS,
    CONF_DEBUG_INSPECT,
    CONF_DEBUG_LOGS,
    CONF_DEBUG_PROVIDERS,
    CONF_DEBUG_RELOAD,
    CONF_DELETE_FAVORITES,
    CONF_DELETE_LIBRARY,
    CONF_DELETE_PLAYLISTS,
    CONF_DELETE_QUEUE,
    CONF_EDIT_FAVORITES,
    CONF_EDIT_LIBRARY,
    CONF_EDIT_PLAYLISTS,
    CONF_EDIT_QUEUE,
    CONF_ENFORCE_AUDIENCE,
    CONF_EXTRA_ALLOWED_ORIGINS,
    CONF_MOUNT_PATH,
    CONF_QUERY_LIBRARY,
    CONF_QUERY_METADATA,
    CONF_QUERY_PLAYERS,
    CONF_QUERY_QUEUE,
    CONF_REQUIRE_AUTH,
    CONF_REQUIRE_CONFIRMATION,
    CONF_RES_LIBRARY,
    CONF_RES_PLAYER,
    CONF_RES_PROMPTS,
    DEFAULT_MOUNT_PATH,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType

    from music_assistant.mass import MusicAssistant


def _bool(key: str, label: str, default: bool, category: str, description: str = "") -> ConfigEntry:
    return ConfigEntry(
        key=key,
        type=ConfigEntryType.BOOLEAN,
        label=label,
        default_value=default,
        category=category,
        description=description or label,
        required=False,
    )


def build_config_entries(
    mass: MusicAssistant,
    values: dict[str, ConfigValueType],
) -> tuple[ConfigEntry, ...]:
    """Return the full ConfigEntry schema for this provider.

    :param mass: MusicAssistant instance, used to compose the info label.
    :param values: Current config values (may be empty on first setup).
    """
    base_url = mass.webserver.base_url.rstrip("/")
    raw_mount = str(values.get(CONF_MOUNT_PATH) or DEFAULT_MOUNT_PATH)
    # Mirror ``MCPServerRuntime.__init__``'s normalisation so the info label
    # always renders a valid URL even if the user dropped the leading slash.
    mount_path = "/" + raw_mount.strip("/")
    info_label = f"MCP endpoint: {base_url}{mount_path}\nCreate tokens in Profile → Long-lived access tokens."

    return (
        ConfigEntry(
            key="info_label",
            type=ConfigEntryType.LABEL,
            label=info_label,
            category="Server",
            required=False,
        ),
        ConfigEntry(
            key="open_connect",
            type=ConfigEntryType.ACTION,
            action="open_connect",
            required=False,
        ),
        ConfigEntry(
            key=CONF_REQUIRE_AUTH,
            type=ConfigEntryType.BOOLEAN,
            default_value=True,
            category="Server",
            required=False,
        ),
        ConfigEntry(
            key=CONF_MOUNT_PATH,
            type=ConfigEntryType.STRING,
            default_value=DEFAULT_MOUNT_PATH,
            category="Server",
            advanced=True,
            required=False,
        ),
        ConfigEntry(
            key=CONF_REQUIRE_CONFIRMATION,
            type=ConfigEntryType.BOOLEAN,
            default_value=True,
            category="Server",
            required=False,
        ),
        ConfigEntry(
            key=CONF_ENFORCE_AUDIENCE,
            type=ConfigEntryType.BOOLEAN,
            default_value=False,
            category="Server",
            advanced=True,
            required=False,
        ),
        ConfigEntry(
            key=CONF_EXTRA_ALLOWED_ORIGINS,
            type=ConfigEntryType.STRING,
            default_value="",
            category="Server",
            advanced=True,
            required=False,
        ),
        ConfigEntry(
            key=CONF_CONNECT_EXTERNAL_URL,
            type=ConfigEntryType.STRING,
            default_value="",
            category="Server",
            advanced=True,
            required=False,
        ),
        # Query permissions
        _bool(
            CONF_QUERY_LIBRARY,
            "Query library",
            True,
            "Query Permissions",
            "Search music, browse library, get artists/albums/tracks/playlists.",
        ),
        _bool(
            CONF_QUERY_QUEUE,
            "Query queue",
            True,
            "Query Permissions",
            "Read the current queue state for any player.",
        ),
        _bool(
            CONF_QUERY_PLAYERS,
            "Query players",
            True,
            "Query Permissions",
            "List players and read their state and capabilities.",
        ),
        _bool(
            CONF_QUERY_METADATA,
            "Query metadata",
            True,
            "Query Permissions",
            "Get lyrics, recommendations, and similar tracks.",
        ),
        # Control permissions
        _bool(
            CONF_CONTROL_PLAYBACK,
            "Control playback",
            False,
            "Control Permissions",
            "Play, pause, stop, seek, next/previous, play media.",
        ),
        _bool(
            CONF_CONTROL_VOLUME,
            "Control volume",
            False,
            "Control Permissions",
            "Set volume, volume up/down, mute, group volume.",
        ),
        _bool(
            CONF_CONTROL_PLAYERS,
            "Control players",
            False,
            "Control Permissions",
            "Power players on/off, select source.",
        ),
        _bool(
            CONF_CONTROL_MEDIA,
            "Play announcements / mark played",
            False,
            "Control Permissions",
            "Send TTS announcements, mark items as played.",
        ),
        # Edit permissions
        _bool(
            CONF_EDIT_LIBRARY,
            "Add items to library",
            False,
            "Edit Permissions",
            "Add tracks, albums, artists, or playlists to the library.",
        ),
        _bool(
            CONF_EDIT_QUEUE,
            "Edit queue",
            False,
            "Edit Permissions",
            "Move queue items, save queue as playlist.",
        ),
        _bool(
            CONF_EDIT_PLAYLISTS,
            "Create / modify playlists",
            False,
            "Edit Permissions",
            "Create playlists, add tracks, reorder.",
        ),
        _bool(
            CONF_EDIT_FAVORITES,
            "Add to favorites",
            False,
            "Edit Permissions",
            "Mark items as favorites.",
        ),
        # Delete permissions
        _bool(
            CONF_DELETE_LIBRARY,
            "Remove items from library",
            False,
            "Delete Permissions",
            "Remove tracks, albums, artists, or playlists from the library.",
        ),
        _bool(
            CONF_DELETE_QUEUE,
            "Clear queue / remove items",
            False,
            "Delete Permissions",
            "Remove queue items or clear the queue.",
        ),
        _bool(
            CONF_DELETE_PLAYLISTS,
            "Delete playlists / remove tracks",
            False,
            "Delete Permissions",
            "Delete playlists, remove tracks from playlists.",
        ),
        _bool(
            CONF_DELETE_FAVORITES,
            "Remove from favorites",
            False,
            "Delete Permissions",
            "Remove items from favorites.",
        ),
        # Resources / prompts
        _bool(
            CONF_RES_LIBRARY,
            "Expose library:// resources",
            True,
            "MCP Resources",
            "URI-addressable read-only views of artists, albums, tracks, playlists.",
        ),
        _bool(
            CONF_RES_PLAYER,
            "Expose player:// and queue:// resources",
            True,
            "MCP Resources",
            "URI-addressable views of players and queues.",
        ),
        _bool(
            CONF_RES_PROMPTS,
            "Expose canned prompts",
            True,
            "MCP Resources",
            "Pre-defined prompts: find_and_play, party_playlist, now_playing_summary.",
        ),
        # Debug namespace — all off-by-default. See specs/inprogress/0005-debug-namespace.md.
        _bool(
            CONF_DEBUG_INSPECT,
            "Debug: inspect raw player/queue/provider state",
            False,
            "Debug",
            "Exposes raw runtime state of players, queues, and providers via MCP. "
            "Intended for development and troubleshooting. Disable in production.",
        ),
        _bool(
            CONF_DEBUG_LOGS,
            "Debug: tail musicassistant.log",
            False,
            "Debug",
            "Allows MCP clients to read the tail of MA's log file with filters. "
            "Common token patterns are redacted. Intended for troubleshooting. "
            "Disable in production.",
        ),
        _bool(
            CONF_DEBUG_EVENTS,
            "Debug: read recent MA events",
            False,
            "Debug",
            "Subscribes to MA's event bus at provider startup and exposes a "
            "ring buffer over MCP. Memory cost is bounded by the buffer "
            "capacity. Intended for troubleshooting. Disable in production.",
        ),
        _bool(
            CONF_DEBUG_PROVIDERS,
            "Debug: inspect configured providers",
            False,
            "Debug",
            "Exposes provider state, masked configuration, registered "
            "webserver routes, installed package versions, and a health "
            "summary roll-up. Intended for troubleshooting. Disable in "
            "production.",
        ),
        _bool(
            CONF_DEBUG_RELOAD,
            "Debug: reload a provider instance",
            False,
            "Debug",
            "Allows MCP clients to unload and reload provider instances, "
            "INTERRUPTING ANY ACTIVE STREAMS on the affected provider. "
            "Each call requires elicitation confirmation. Intended for "
            "provider-development iteration. Disable in production.",
        ),
        ConfigEntry(
            key=CONF_DEBUG_EVENT_BUFFER_CAPACITY,
            type=ConfigEntryType.INTEGER,
            default_value=500,
            range=(50, 5000),
            category="Debug",
            required=False,
        ),
        # Config namespace — all off-by-default. See specs/inprogress/0006-config-read-write.md.
        _bool(
            CONF_CONFIG_READ,
            "Config: read core/provider/player settings",
            False,
            "Config",
            "Exposes read access to MA core, provider, and player configuration "
            "over MCP (secrets stay masked). Disable in production unless needed.",
        ),
        _bool(
            CONF_CONFIG_WRITE_PROVIDER,
            "Config: edit provider settings",
            False,
            "Config",
            "Allows MCP clients to change provider configuration and trigger "
            "provider config actions. Changes may reload the provider and "
            "interrupt its streams. Disable in production.",
        ),
        _bool(
            CONF_CONFIG_WRITE_CORE,
            "Config: edit core settings",
            False,
            "Config",
            "Allows MCP clients to change MA core controller configuration "
            "(webserver, streams, cache, ...). Core changes may RESTART "
            "subsystems and interrupt ALL playback. Disable in production.",
        ),
        _bool(
            CONF_CONFIG_WRITE_PLAYER,
            "Config: edit player settings",
            False,
            "Config",
            "Allows MCP clients to change per-player configuration and DSP. Disable in production.",
        ),
        _bool(
            CONF_CONFIG_WRITE_SECRET,
            "Config: allow writing secret values",
            False,
            "Config",
            "Required IN ADDITION to a category write flag before any "
            "SECURE_STRING (password/token) value can be written. With this "
            "off, secret writes are rejected while non-secret edits still "
            "work. Keep off unless deliberately rotating credentials.",
        ),
    )
