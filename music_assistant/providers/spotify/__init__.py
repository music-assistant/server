"""Spotify music provider support for Music Assistant."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from music_assistant_models.config_entries import ConfigEntry, ConfigValueType
from music_assistant_models.enums import ConfigEntryType, ProviderFeature
from music_assistant_models.errors import InvalidDataError, LoginFailed

from music_assistant.helpers.app_vars import app_var  # type: ignore[attr-defined]

from .constants import (
    CALLBACK_REDIRECT_URL,
    CONF_ACTION_AUTH,
    CONF_ACTION_AUTH_DEV,
    CONF_ACTION_CLEAR_AUTH,
    CONF_ACTION_CLEAR_AUTH_DEV,
    CONF_CLIENT_ID,
    CONF_REFRESH_TOKEN_DEPRECATED,
    CONF_REFRESH_TOKEN_DEV,
    CONF_REFRESH_TOKEN_GLOBAL,
    CONF_SYNC_AUDIOBOOK_PROGRESS,
    CONF_SYNC_PODCAST_PROGRESS,
)
from .helpers import pkce_auth_flow
from .provider import SpotifyProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES = {
    ProviderFeature.LIBRARY_ARTISTS,
    ProviderFeature.LIBRARY_ALBUMS,
    ProviderFeature.LIBRARY_TRACKS,
    ProviderFeature.LIBRARY_PLAYLISTS,
    ProviderFeature.LIBRARY_ARTISTS_EDIT,
    ProviderFeature.LIBRARY_ALBUMS_EDIT,
    ProviderFeature.LIBRARY_PLAYLISTS_EDIT,
    ProviderFeature.LIBRARY_TRACKS_EDIT,
    ProviderFeature.PLAYLIST_TRACKS_EDIT,
    ProviderFeature.BROWSE,
    ProviderFeature.SEARCH,
    ProviderFeature.ARTIST_ALBUMS,
    ProviderFeature.ARTIST_TOPTRACKS,
    ProviderFeature.SIMILAR_TRACKS,
    ProviderFeature.LIBRARY_PODCASTS,
    ProviderFeature.LIBRARY_PODCASTS_EDIT,
}


async def _handle_auth_actions(
    mass: MusicAssistant,
    action: str | None,
    values: dict[str, ConfigValueType] | None,
) -> None:
    """Handle authentication-related actions for config entries."""
    if values is None:
        return

    if action == CONF_ACTION_AUTH:
        refresh_token = await pkce_auth_flow(mass, cast("str", values["session_id"]), app_var(2))
        values[CONF_REFRESH_TOKEN_GLOBAL] = refresh_token
        values[CONF_REFRESH_TOKEN_DEV] = None  # Clear dev token on new global auth
        values[CONF_CLIENT_ID] = None  # Clear client ID on new global auth

    elif action == CONF_ACTION_AUTH_DEV:
        custom_client_id = values.get(CONF_CLIENT_ID)
        if not custom_client_id:
            raise InvalidDataError("Client ID is required for developer authentication")
        refresh_token = await pkce_auth_flow(
            mass, cast("str", values["session_id"]), cast("str", custom_client_id)
        )
        values[CONF_REFRESH_TOKEN_DEV] = refresh_token

    elif action == CONF_ACTION_CLEAR_AUTH:
        values[CONF_REFRESH_TOKEN_GLOBAL] = None

    elif action == CONF_ACTION_CLEAR_AUTH_DEV:
        values[CONF_REFRESH_TOKEN_DEV] = None
        values[CONF_CLIENT_ID] = None


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider."""
    # Check if audiobooks are supported by existing provider instance
    audiobooks_supported = (
        instance_id
        and (prov_instance := mass.get_provider(instance_id))
        and getattr(prov_instance, "audiobooks_supported", False)
    )

    # Handle any authentication actions
    await _handle_auth_actions(mass, action, values)

    # Determine authentication states from current values
    # Note: encrypted values are sent as placeholder text, which indicates value IS set
    global_token = (values or {}).get(CONF_REFRESH_TOKEN_GLOBAL)
    dev_token = (values or {}).get(CONF_REFRESH_TOKEN_DEV)
    global_authenticated = global_token not in (None, "")
    dev_authenticated = dev_token not in (None, "")

    # Build label text based on state - these are dynamic based on current values
    if not global_authenticated:
        label_text = (
            "You need to authenticate to Spotify. Click the authenticate button below "
            "to start the authentication process which will open in a new (popup) window, "
            "so make sure to disable any popup blockers.\n\n"
            "Also make sure to perform this action from your local network."
        )
    elif action == CONF_ACTION_AUTH:
        label_text = "Authenticated to Spotify. Don't forget to save to complete setup."
    else:
        label_text = "Authenticated to Spotify. No further action required."

    # Build dev label text
    if action == CONF_ACTION_AUTH_DEV:
        dev_label_text = "Developer session authenticated. Don't forget to save to complete setup."
    elif dev_authenticated:
        dev_label_text = (
            "Developer API session authenticated. "
            "This session will be used for most API requests to avoid rate limits."
        )
    else:
        dev_label_text = (
            "Optionally, enter your own Spotify Developer Client ID to speed up performance."
        )

    return (
        # Global authentication section
        ConfigEntry(
            key="label_text",
            type=ConfigEntryType.LABEL,
            label=label_text,
        ),
        ConfigEntry(
            key=CONF_REFRESH_TOKEN_GLOBAL,
            type=ConfigEntryType.SECURE_STRING,
            label=CONF_REFRESH_TOKEN_GLOBAL,
            hidden=True,
            required=True,
            default_value="",
            value=values.get(CONF_REFRESH_TOKEN_GLOBAL, "") if values else "",
        ),
        ConfigEntry(
            key=CONF_ACTION_AUTH,
            type=ConfigEntryType.ACTION,
            label="Authenticate with Spotify",
            description="This button will redirect you to Spotify to authenticate.",
            action=CONF_ACTION_AUTH,
            # Show only when not authenticated
            hidden=global_authenticated,
        ),
        ConfigEntry(
            key=CONF_ACTION_CLEAR_AUTH,
            type=ConfigEntryType.ACTION,
            label="Clear authentication",
            description="Clear the current authentication details.",
            action=CONF_ACTION_CLEAR_AUTH,
            action_label="Clear authentication",
            required=False,
            # Show only when authenticated
            hidden=not global_authenticated,
        ),
        # Developer API section
        ConfigEntry(
            key="dev_label_text",
            type=ConfigEntryType.LABEL,
            label=dev_label_text,
            category="Developer Token",
            # Show only when global auth is complete
            hidden=not global_authenticated,
        ),
        ConfigEntry(
            key=CONF_CLIENT_ID,
            type=ConfigEntryType.SECURE_STRING,
            label="Client ID (optional)",
            description="Enter your own Spotify Developer Client ID to speed up performance "
            "by avoiding global rate limits. Some features like recommendations and similar "
            "tracks will continue to use the global session due to Spotify API restrictions.\n\n"
            f"Use {CALLBACK_REDIRECT_URL} as callback URL in your Spotify Developer app.",
            required=False,
            default_value="",
            value=values.get(CONF_CLIENT_ID, "") if values else "",
            category="Developer Token",
            # Show only when global auth is complete
            hidden=not global_authenticated or dev_authenticated,
        ),
        ConfigEntry(
            key=CONF_REFRESH_TOKEN_DEV,
            type=ConfigEntryType.SECURE_STRING,
            label=CONF_REFRESH_TOKEN_DEV,
            hidden=True,
            required=False,
            default_value="",
            value=values.get(CONF_REFRESH_TOKEN_DEV, "") if values else "",
        ),
        ConfigEntry(
            key=CONF_ACTION_AUTH_DEV,
            type=ConfigEntryType.ACTION,
            label="Authenticate Developer Session",
            description="Authenticate with your custom Client ID.",
            action=CONF_ACTION_AUTH_DEV,
            category="Developer Token",
            # Show only when global is authenticated and dev is NOT authenticated
            # The client_id dependency is checked at action time, not visibility
            hidden=not global_authenticated or dev_authenticated,
        ),
        ConfigEntry(
            key=CONF_ACTION_CLEAR_AUTH_DEV,
            type=ConfigEntryType.ACTION,
            label="Clear Developer Authentication",
            description="Clear the developer session authentication and client ID.",
            action=CONF_ACTION_CLEAR_AUTH_DEV,
            action_label="Clear developer authentication",
            required=False,
            category="Developer Token",
            # Show when dev token is set
            hidden=not global_authenticated or not dev_authenticated,
        ),
        # Sync options
        ConfigEntry(
            key=CONF_SYNC_PODCAST_PROGRESS,
            type=ConfigEntryType.BOOLEAN,
            label="Sync Podcast Progress from Spotify",
            description="Automatically sync episode played status from Spotify to Music Assistant. "
            "Episodes marked as played in Spotify will be marked as played in MA."
            "Only enable this if you use both the Spotify app and Music Assistant "
            "for podcast playback.",
            default_value=False,
            value=values.get(CONF_SYNC_PODCAST_PROGRESS, True) if values else True,
            category="sync_options",
            hidden=not global_authenticated,
        ),
        ConfigEntry(
            key=CONF_SYNC_AUDIOBOOK_PROGRESS,
            type=ConfigEntryType.BOOLEAN,
            label="Sync Audiobook Progress from Spotify",
            description="Automatically sync audiobook progress from Spotify to Music Assistant. "
            "Progress from Spotify app will sync to MA when audiobooks are accessed. "
            "Only enable this if you use both the Spotify app and Music Assistant "
            "for audiobook playback.",
            default_value=False,
            value=values.get(CONF_SYNC_AUDIOBOOK_PROGRESS, False) if values else False,
            category="sync_options",
            hidden=not global_authenticated or not audiobooks_supported,
        ),
    )


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    # Migration: handle legacy refresh_token
    legacy_token = config.get_value(CONF_REFRESH_TOKEN_DEPRECATED)
    global_token = config.get_value(CONF_REFRESH_TOKEN_GLOBAL)

    if legacy_token and not global_token:
        # Migrate legacy token to appropriate new key
        if config.get_value(CONF_CLIENT_ID):
            # Had custom client ID, migrate to dev token
            mass.config.set_raw_provider_config_value(
                config.instance_id, CONF_REFRESH_TOKEN_DEV, legacy_token, encrypted=True
            )
        else:
            # No custom client ID, migrate to global token
            mass.config.set_raw_provider_config_value(
                config.instance_id, CONF_REFRESH_TOKEN_GLOBAL, legacy_token, encrypted=True
            )
        # Remove the deprecated legacy token from config
        mass.config.set_raw_provider_config_value(
            config.instance_id, CONF_REFRESH_TOKEN_DEPRECATED, None
        )
        # Re-fetch the updated config value
        global_token = config.get_value(CONF_REFRESH_TOKEN_GLOBAL)

    if global_token in (None, ""):
        msg = "Re-Authentication required"
        raise LoginFailed(msg)
    return SpotifyProvider(mass, manifest, config, SUPPORTED_FEATURES)
