"""KION Music provider support for Music Assistant."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption, ConfigValueType
from music_assistant_models.enums import ConfigEntryType, ProviderFeature

from .constants import (
    CONF_ACTION_CLEAR_AUTH,
    CONF_BASE_URL,
    CONF_CODECS,
    CONF_LIKED_TRACKS_MAX_TRACKS,
    CONF_MY_WAVE_MAX_TRACKS,
    CONF_QUALITY,
    CONF_TOKEN,
    CONF_TRANSPORT,
    DEFAULT_BASE_URL,
    QUALITY_BALANCED,
    QUALITY_EFFICIENT,
    QUALITY_HIGH,
    QUALITY_LOSSLESS,
    TRANSPORT_ENCRAW,
    TRANSPORT_RAW,
)
from .provider import KionMusicProvider

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
    ProviderFeature.ARTIST_ALBUMS,
    ProviderFeature.ARTIST_TOPTRACKS,
    ProviderFeature.SEARCH,
    ProviderFeature.LIBRARY_ARTISTS_EDIT,
    ProviderFeature.LIBRARY_ALBUMS_EDIT,
    ProviderFeature.LIBRARY_TRACKS_EDIT,
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
    return KionMusicProvider(mass, manifest, config, SUPPORTED_FEATURES)


async def get_config_entries(
    mass: MusicAssistant,  # noqa: ARG001
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider."""
    if values is None:
        values = {}

    # Handle clear auth action
    if action == CONF_ACTION_CLEAR_AUTH:
        values[CONF_TOKEN] = None

    # Check if user is authenticated
    is_authenticated = bool(values.get(CONF_TOKEN))

    return (
        # Authentication
        ConfigEntry(
            key=CONF_TOKEN,
            type=ConfigEntryType.SECURE_STRING,
            label="KION Music Token",
            description="Enter your KION Music OAuth token. "
            "See the documentation for how to obtain it.",
            required=True,
            hidden=is_authenticated,
            value=cast("str", values.get(CONF_TOKEN)) if values else None,
        ),
        ConfigEntry(
            key=CONF_ACTION_CLEAR_AUTH,
            type=ConfigEntryType.ACTION,
            label="Reset authentication",
            description="Clear the current authentication details.",
            action=CONF_ACTION_CLEAR_AUTH,
            hidden=not is_authenticated,
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
                ConfigValueOption("Superb (FLAC Lossless)", QUALITY_LOSSLESS),
            ],
            default_value=QUALITY_BALANCED,
        ),
        # My Mix maximum tracks (advanced)
        ConfigEntry(
            key=CONF_MY_WAVE_MAX_TRACKS,
            type=ConfigEntryType.INTEGER,
            label="My Mix maximum tracks",
            description="Maximum number of tracks to fetch for My Mix playlist. "
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
        # Transport mode (advanced)
        ConfigEntry(
            key=CONF_TRANSPORT,
            type=ConfigEntryType.STRING,
            label="Streaming transport",
            description="Transport mode for audio streaming. "
            "'raw' — direct unencrypted stream (default). "
            "'encraw' — AES-CTR encrypted stream (fallback).",
            options=[
                ConfigValueOption("Raw (unencrypted)", TRANSPORT_RAW),
                ConfigValueOption("Encrypted (AES-CTR)", TRANSPORT_ENCRAW),
            ],
            default_value=TRANSPORT_RAW,
            required=False,
            advanced=True,
        ),
        # Custom codecs override (advanced)
        ConfigEntry(
            key=CONF_CODECS,
            type=ConfigEntryType.STRING,
            label="Codecs override",
            description="Comma-separated codec list to override quality-based defaults. "
            "Leave empty to use defaults. Example: 'flac-mp4,flac,aac-mp4,aac,mp3'.",
            default_value="",
            required=False,
            advanced=True,
        ),
        # API Base URL (advanced)
        ConfigEntry(
            key=CONF_BASE_URL,
            type=ConfigEntryType.STRING,
            label="API Base URL",
            description="API endpoint base URL. "
            "Only change if KION Music changes their API endpoint. "
            f"Default: {DEFAULT_BASE_URL}",
            default_value=DEFAULT_BASE_URL,
            required=False,
            advanced=True,
        ),
    )
