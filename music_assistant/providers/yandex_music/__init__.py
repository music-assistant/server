"""Yandex Music provider support for Music Assistant."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption, ConfigValueType
from music_assistant_models.enums import ConfigEntryType, ProviderFeature
from music_assistant_models.errors import InvalidDataError

from .auth import perform_device_auth, perform_qr_auth
from .constants import (
    CONF_ACTION_AUTH_DEVICE,
    CONF_ACTION_AUTH_QR,
    CONF_ACTION_CLEAR_AUTH,
    CONF_BASE_URL,
    CONF_LIKED_TRACKS_MAX_TRACKS,
    CONF_MY_WAVE_MAX_TRACKS,
    CONF_QUALITY,
    CONF_REFRESH_TOKEN,
    CONF_REMEMBER_SESSION,
    CONF_TOKEN,
    CONF_X_TOKEN,
    DEFAULT_BASE_URL,
    QUALITY_BALANCED,
    QUALITY_EFFICIENT,
    QUALITY_HIGH,
    QUALITY_SUPERB,
)
from .provider import YandexMusicProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES = {
    ProviderFeature.LIBRARY_ARTISTS,
    ProviderFeature.LIBRARY_ALBUMS,
    ProviderFeature.LIBRARY_TRACKS,
    ProviderFeature.LIBRARY_PLAYLISTS,
    ProviderFeature.LIBRARY_PODCASTS,
    ProviderFeature.LIBRARY_AUDIOBOOKS,
    ProviderFeature.ARTIST_ALBUMS,
    ProviderFeature.ARTIST_TOPTRACKS,
    ProviderFeature.SEARCH,
    ProviderFeature.LIBRARY_ARTISTS_EDIT,
    ProviderFeature.LIBRARY_ALBUMS_EDIT,
    ProviderFeature.LIBRARY_TRACKS_EDIT,
    ProviderFeature.LIBRARY_PODCASTS_EDIT,
    ProviderFeature.LIBRARY_AUDIOBOOKS_EDIT,
    ProviderFeature.BROWSE,
    ProviderFeature.SIMILAR_TRACKS,
    ProviderFeature.SIMILAR_ARTISTS,
    ProviderFeature.RECOMMENDATIONS,
    ProviderFeature.LYRICS,
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return YandexMusicProvider(mass, manifest, config, SUPPORTED_FEATURES)


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider."""
    if values is None:
        values = {}

    # Handle QR auth action
    if action == CONF_ACTION_AUTH_QR:
        session_id = values.get("session_id")
        if not session_id:
            raise InvalidDataError("Missing session_id for QR authentication")
        x_token, music_token = await perform_qr_auth(mass, str(session_id))
        values[CONF_TOKEN] = music_token
        if values.get(CONF_REMEMBER_SESSION, True):
            values[CONF_X_TOKEN] = x_token
        else:
            values[CONF_X_TOKEN] = None

    # Handle Device Flow auth action (yields x_token + refresh_token,
    # so we get silent auto-refresh on music-token AND x_token expiry)
    if action == CONF_ACTION_AUTH_DEVICE:
        session_id = values.get("session_id")
        if not session_id:
            raise InvalidDataError("Missing session_id for device authentication")
        x_token, music_token, refresh_token = await perform_device_auth(mass, str(session_id))
        values[CONF_TOKEN] = music_token
        if values.get(CONF_REMEMBER_SESSION, True):
            values[CONF_X_TOKEN] = x_token
            values[CONF_REFRESH_TOKEN] = refresh_token
        else:
            values[CONF_X_TOKEN] = None
            values[CONF_REFRESH_TOKEN] = None

    # Handle clear auth action
    if action == CONF_ACTION_CLEAR_AUTH:
        values[CONF_TOKEN] = None
        values[CONF_X_TOKEN] = None
        values[CONF_REFRESH_TOKEN] = None

    # Check if user is authenticated
    is_authenticated = bool(values.get(CONF_TOKEN))

    # Dynamic label text
    if not is_authenticated:
        label_text = (
            "Open a verification URL on any device and enter the short code, "
            "or scan a QR code with the Yandex app on your phone.\n\n"
            "Alternatively, you can enter a music token manually in the advanced settings."
        )
    elif action in (CONF_ACTION_AUTH_QR, CONF_ACTION_AUTH_DEVICE):
        label_text = "Authenticated to Yandex Music. Don't forget to save to complete setup."
    else:
        label_text = "Authenticated to Yandex Music."

    return (
        # Status label
        ConfigEntry(
            key="label_text",
            type=ConfigEntryType.LABEL,
            label=label_text,
        ),
        # Device Flow authentication (primary)
        ConfigEntry(
            key=CONF_ACTION_AUTH_DEVICE,
            type=ConfigEntryType.ACTION,
            label="Login with device code",
            description=("Open a verification URL on any device and enter the short code."),
            action=CONF_ACTION_AUTH_DEVICE,
            action_label="Login with device code",
            hidden=is_authenticated,
        ),
        # QR authentication (alternative)
        ConfigEntry(
            key=CONF_ACTION_AUTH_QR,
            type=ConfigEntryType.ACTION,
            label="Login with QR code",
            description="Opens a QR code page — scan it with the Yandex app on your phone.",
            action=CONF_ACTION_AUTH_QR,
            action_label="Login with QR code",
            hidden=is_authenticated,
        ),
        # Remember session toggle
        ConfigEntry(
            key=CONF_REMEMBER_SESSION,
            type=ConfigEntryType.BOOLEAN,
            label="Remember session (auto-refresh token)",
            description="When enabled, stores a long-lived session token to automatically "
            "refresh your music token when it expires. When disabled, you must "
            "re-authenticate manually when the token expires.",
            default_value=True,
            hidden=is_authenticated,
        ),
        # Clear auth
        ConfigEntry(
            key=CONF_ACTION_CLEAR_AUTH,
            type=ConfigEntryType.ACTION,
            label="Reset authentication",
            description="Clear the current authentication details.",
            action=CONF_ACTION_CLEAR_AUTH,
            action_label="Reset authentication",
            hidden=not is_authenticated,
        ),
        # Token storage (populated by QR action or manual entry)
        ConfigEntry(
            key=CONF_TOKEN,
            type=ConfigEntryType.SECURE_STRING,
            label="Yandex Music Token (manual)",
            description="Advanced: manually enter a music token. "
            "See the documentation for how to obtain it.",
            required=True,
            hidden=is_authenticated,
            advanced=True,
            value=cast("str", values.get(CONF_TOKEN)) if values else None,
        ),
        # x_token (internal storage, always hidden)
        ConfigEntry(
            key=CONF_X_TOKEN,
            type=ConfigEntryType.SECURE_STRING,
            label="Session token",
            hidden=True,
            required=False,
            value=cast("str", values.get(CONF_X_TOKEN)) if values else None,
        ),
        # refresh_token (internal storage, always hidden — device flow only)
        ConfigEntry(
            key=CONF_REFRESH_TOKEN,
            type=ConfigEntryType.SECURE_STRING,
            label="Refresh token",
            hidden=True,
            required=False,
            value=cast("str", values.get(CONF_REFRESH_TOKEN)) if values else None,
        ),
        # Quality
        ConfigEntry(
            key=CONF_QUALITY,
            type=ConfigEntryType.STRING,
            label="Audio quality",
            description="Select preferred audio quality.",
            options=[
                ConfigValueOption("Efficient (AAC ~64kbps)", QUALITY_EFFICIENT),
                ConfigValueOption("Balanced (AAC ~192kbps)", QUALITY_BALANCED),
                ConfigValueOption("High (MP3 ~320kbps)", QUALITY_HIGH),
                ConfigValueOption("Superb (FLAC Lossless)", QUALITY_SUPERB),
            ],
            default_value=QUALITY_BALANCED,
        ),
        # My Wave maximum tracks (advanced)
        ConfigEntry(
            key=CONF_MY_WAVE_MAX_TRACKS,
            type=ConfigEntryType.INTEGER,
            label="My Wave maximum tracks",
            description="Maximum number of tracks to fetch for My Wave playlist. "
            "Lower values load faster but provide fewer tracks. Default: 150.",
            range=(10, 1000),
            default_value=150,
            required=False,
            advanced=True,
        ),
        # Liked Tracks maximum tracks (advanced)
        ConfigEntry(
            key=CONF_LIKED_TRACKS_MAX_TRACKS,
            type=ConfigEntryType.INTEGER,
            label="Liked Tracks maximum tracks",
            description="Maximum number of tracks to show in Liked Tracks virtual playlist. "
            "Higher values may significantly increase load time. "
            "Lower values load faster. Default: 500.",
            range=(50, 2000),
            default_value=500,
            required=False,
            advanced=True,
        ),
        # API Base URL (advanced)
        ConfigEntry(
            key=CONF_BASE_URL,
            type=ConfigEntryType.STRING,
            label="API Base URL",
            description="API endpoint base URL. "
            "Only change if Yandex Music changes their API endpoint. "
            f"Default: {DEFAULT_BASE_URL}",
            default_value=DEFAULT_BASE_URL,
            required=False,
            advanced=True,
        ),
    )
