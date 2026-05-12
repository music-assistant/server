"""ConfigEntry schema for the MCP Server provider."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

from .constants import (
    CONF_CONNECT_EXTERNAL_URL,
    CONF_CONTROL_MEDIA,
    CONF_CONTROL_PLAYBACK,
    CONF_CONTROL_PLAYERS,
    CONF_CONTROL_VOLUME,
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
    base_url = getattr(mass.webserver, "base_url", "").rstrip("/")
    raw_mount = str(values.get(CONF_MOUNT_PATH) or DEFAULT_MOUNT_PATH)
    # Mirror ``MCPServerRuntime.__init__``'s normalisation so the info label
    # always renders a valid URL even if the user dropped the leading slash.
    mount_path = "/" + raw_mount.strip("/")
    info_label = (
        f"MCP endpoint: {base_url}{mount_path}\nCreate tokens in Settings → Security → Tokens."
    )

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
            label="Open Connect Wizard",
            description=(
                "One-click setup for Claude Desktop, Claude Code, Cursor, "
                "Windsurf, VSCode, ChatGPT and other MCP clients. Mints a "
                "per-client token labelled `MCP — <Client>` (revocable in "
                "Settings → Security → Tokens) and copies the ready-to-paste "
                "snippet for you."
            ),
            action="open_connect",
            required=False,
        ),
        ConfigEntry(
            key=CONF_REQUIRE_AUTH,
            type=ConfigEntryType.BOOLEAN,
            label="Require authentication",
            default_value=True,
            category="Server",
            description=(
                "Reject unauthenticated MCP clients. Strongly recommended — "
                "with auth disabled, every MCP client on the network can drive playback."
            ),
            required=False,
        ),
        ConfigEntry(
            key=CONF_MOUNT_PATH,
            type=ConfigEntryType.STRING,
            label="Mount path",
            default_value=DEFAULT_MOUNT_PATH,
            category="Server",
            advanced=True,
            description=(
                "HTTP path prefix where the MCP server is mounted on MA's webserver. "
                "Change only if it conflicts with another route."
            ),
            required=False,
        ),
        ConfigEntry(
            key=CONF_REQUIRE_CONFIRMATION,
            type=ConfigEntryType.BOOLEAN,
            label="Confirm destructive operations",
            default_value=True,
            category="Server",
            description=(
                "Ask the MCP client to confirm before running destructive tools "
                "(clear_queue, remove_tracks, remove_from_library, "
                "remove_from_favorites). If the client doesn't support "
                "elicitation, the call falls through to the permission flag."
            ),
            required=False,
        ),
        ConfigEntry(
            key=CONF_ENFORCE_AUDIENCE,
            type=ConfigEntryType.BOOLEAN,
            label="Enforce token audience (RFC 8707)",
            default_value=False,
            category="Server",
            advanced=True,
            description=(
                "Reject Bearer tokens whose `aud` claim does not match this MCP "
                "server's canonical URI. Mitigates the OAuth confused-deputy "
                "attack where a token issued for one MA endpoint is replayed "
                "against another. Requires upstream Music Assistant support for "
                "writing `aud` into JWTs (in progress) — until then enabling "
                "this rejects all existing tokens. Leave off unless your MA "
                "build issues audience-bound tokens."
            ),
            required=False,
        ),
        ConfigEntry(
            key=CONF_EXTRA_ALLOWED_ORIGINS,
            type=ConfigEntryType.STRING,
            label="Additional allowed Origins (CSV)",
            default_value="",
            category="Server",
            advanced=True,
            description=(
                "Comma-separated list of additional `Origin` headers to accept "
                "(e.g. `https://ha.example.com` for Home Assistant ingress, or a "
                "reverse-proxy hostname). By default the server only accepts "
                "`localhost`, `127.0.0.1`, the MA `base_url` host, and `publish_ip`. "
                "Mismatching Origins are rejected with 403 to mitigate DNS rebinding."
            ),
            required=False,
        ),
        ConfigEntry(
            key=CONF_CONNECT_EXTERNAL_URL,
            type=ConfigEntryType.STRING,
            label="Connect Wizard external URL (fallback)",
            default_value="",
            category="Server",
            advanced=True,
            description=(
                "Optional explicit base URL the Connect Wizard should open at "
                "(e.g. `https://ha.example.com/<addon-slug>` for Home Assistant "
                "add-on ingress). Used only when the wizard cannot auto-detect "
                "the external URL from the active client connection — set it "
                "if your reverse proxy strips the `X-Forwarded-Host` / "
                "`X-Ingress-Path` headers."
            ),
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
    )
