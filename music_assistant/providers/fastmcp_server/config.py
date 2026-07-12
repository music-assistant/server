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
    CONF_LEAN_ADMIN_SCHEMA,
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
    CONF_TRUST_FORWARDED_PROTO,
    DEFAULT_MOUNT_PATH,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType

    from music_assistant.mass import MusicAssistant


def _bool(key: str, default: bool, category: str) -> ConfigEntry:
    # Label/description intentionally unset: strings.json owns all entry text.
    return ConfigEntry(
        key=key,
        type=ConfigEntryType.BOOLEAN,
        default_value=default,
        category=category,
        required=False,
    )


def build_config_entries(
    mass: MusicAssistant,
    values: dict[str, ConfigValueType],
) -> tuple[ConfigEntry, ...]:
    """
    Return the full ConfigEntry schema for this provider.

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
            category="server",
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
            category="server",
            required=False,
        ),
        ConfigEntry(
            key=CONF_MOUNT_PATH,
            type=ConfigEntryType.STRING,
            default_value=DEFAULT_MOUNT_PATH,
            category="server",
            advanced=True,
            required=False,
        ),
        ConfigEntry(
            key=CONF_REQUIRE_CONFIRMATION,
            type=ConfigEntryType.BOOLEAN,
            default_value=True,
            category="server",
            required=False,
        ),
        ConfigEntry(
            key=CONF_ENFORCE_AUDIENCE,
            type=ConfigEntryType.BOOLEAN,
            default_value=False,
            category="server",
            advanced=True,
            required=False,
        ),
        ConfigEntry(
            key=CONF_EXTRA_ALLOWED_ORIGINS,
            type=ConfigEntryType.STRING,
            default_value="",
            category="server",
            advanced=True,
            required=False,
        ),
        ConfigEntry(
            key=CONF_CONNECT_EXTERNAL_URL,
            type=ConfigEntryType.STRING,
            default_value="",
            category="server",
            advanced=True,
            required=False,
        ),
        ConfigEntry(
            key=CONF_LEAN_ADMIN_SCHEMA,
            type=ConfigEntryType.BOOLEAN,
            default_value=False,
            category="server",
            advanced=True,
            required=False,
        ),
        ConfigEntry(
            key=CONF_TRUST_FORWARDED_PROTO,
            type=ConfigEntryType.BOOLEAN,
            default_value=False,
            category="server",
            advanced=True,
            required=False,
        ),
        # Query permissions
        _bool(CONF_QUERY_LIBRARY, True, "query_permissions"),
        _bool(CONF_QUERY_QUEUE, True, "query_permissions"),
        _bool(CONF_QUERY_PLAYERS, True, "query_permissions"),
        _bool(CONF_QUERY_METADATA, True, "query_permissions"),
        # Control permissions
        _bool(CONF_CONTROL_PLAYBACK, False, "control_permissions"),
        _bool(CONF_CONTROL_VOLUME, False, "control_permissions"),
        _bool(CONF_CONTROL_PLAYERS, False, "control_permissions"),
        _bool(CONF_CONTROL_MEDIA, False, "control_permissions"),
        # Edit permissions
        _bool(CONF_EDIT_LIBRARY, False, "edit_permissions"),
        _bool(CONF_EDIT_QUEUE, False, "edit_permissions"),
        _bool(CONF_EDIT_PLAYLISTS, False, "edit_permissions"),
        _bool(CONF_EDIT_FAVORITES, False, "edit_permissions"),
        # Delete permissions
        _bool(CONF_DELETE_LIBRARY, False, "delete_permissions"),
        _bool(CONF_DELETE_QUEUE, False, "delete_permissions"),
        _bool(CONF_DELETE_PLAYLISTS, False, "delete_permissions"),
        _bool(CONF_DELETE_FAVORITES, False, "delete_permissions"),
        # Resources / prompts
        _bool(CONF_RES_LIBRARY, True, "mcp_resources"),
        _bool(CONF_RES_PLAYER, True, "mcp_resources"),
        _bool(CONF_RES_PROMPTS, True, "mcp_resources"),
        # Debug namespace — all off-by-default. See specs/inprogress/0005-debug-namespace.md.
        _bool(CONF_DEBUG_INSPECT, False, "debug"),
        _bool(CONF_DEBUG_LOGS, False, "debug"),
        _bool(CONF_DEBUG_EVENTS, False, "debug"),
        _bool(CONF_DEBUG_PROVIDERS, False, "debug"),
        _bool(CONF_DEBUG_RELOAD, False, "debug"),
        ConfigEntry(
            key=CONF_DEBUG_EVENT_BUFFER_CAPACITY,
            type=ConfigEntryType.INTEGER,
            default_value=500,
            range=(50, 5000),
            category="debug",
            required=False,
        ),
        # Config namespace — all off-by-default. See specs/inprogress/0006-config-read-write.md.
        _bool(CONF_CONFIG_READ, False, "mcp_config"),
        _bool(CONF_CONFIG_WRITE_PROVIDER, False, "mcp_config"),
        _bool(CONF_CONFIG_WRITE_CORE, False, "mcp_config"),
        _bool(CONF_CONFIG_WRITE_PLAYER, False, "mcp_config"),
        _bool(CONF_CONFIG_WRITE_SECRET, False, "mcp_config"),
    )
