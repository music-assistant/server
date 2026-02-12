"""Yandex Music provider support for Music Assistant."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption, ConfigValueType
from music_assistant_models.enums import ConfigEntryType, ProviderFeature

from .constants import (
    CONF_ACTION_CLEAR_AUTH,
    CONF_BASE_URL,
    CONF_BROWSE_INITIAL_TRACKS,
    CONF_DISCOVERY_INITIAL_TRACKS,
    CONF_ENABLE_LIKED_TRACKS_BROWSE,
    CONF_ENABLE_LIKED_TRACKS_PLAYLIST,
    CONF_ENABLE_MY_WAVE_BROWSE,
    CONF_ENABLE_MY_WAVE_PLAYLIST,
    CONF_ENABLE_MY_WAVE_RADIO,
    CONF_ENABLE_RECOMMENDATIONS,
    CONF_LIKED_TRACKS_MAX_TRACKS,
    CONF_MY_WAVE_BATCH_SIZE,
    CONF_MY_WAVE_MAX_TRACKS,
    CONF_QUALITY,
    CONF_TOKEN,
    CONF_TRACK_BATCH_SIZE,
    DEFAULT_BASE_URL,
    QUALITY_HIGH,
    QUALITY_LOSSLESS,
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
    ProviderFeature.ARTIST_ALBUMS,
    ProviderFeature.ARTIST_TOPTRACKS,
    ProviderFeature.SEARCH,
    ProviderFeature.LIBRARY_ARTISTS_EDIT,
    ProviderFeature.LIBRARY_ALBUMS_EDIT,
    ProviderFeature.LIBRARY_TRACKS_EDIT,
    ProviderFeature.BROWSE,
    ProviderFeature.SIMILAR_TRACKS,
    ProviderFeature.RECOMMENDATIONS,
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    features = SUPPORTED_FEATURES.copy()
    # Conditionally disable recommendations based on config
    if not config.get_value(CONF_ENABLE_RECOMMENDATIONS, True):
        features.discard(ProviderFeature.RECOMMENDATIONS)
    return YandexMusicProvider(mass, manifest, config, features)


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
        # Authentication & Quality (no category, always visible)
        ConfigEntry(
            key=CONF_TOKEN,
            type=ConfigEntryType.SECURE_STRING,
            label="Yandex Music Token",
            description="Enter your Yandex Music OAuth token. "
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
        ConfigEntry(
            key=CONF_QUALITY,
            type=ConfigEntryType.STRING,
            label="Audio quality",
            description="Select preferred audio quality.",
            options=[
                ConfigValueOption("High (320 kbps)", QUALITY_HIGH),
                ConfigValueOption("Lossless (FLAC)", QUALITY_LOSSLESS),
            ],
            default_value=QUALITY_HIGH,
        ),
        # My Wave category (user-facing toggles)
        ConfigEntry(
            key=CONF_ENABLE_RECOMMENDATIONS,
            type=ConfigEntryType.BOOLEAN,
            label="Enable Discover (Recommendations)",
            description="Show My Wave recommendations on the home page. "
            "When enabled, recommendations refresh each time you reload the page "
            "for fresh discoveries.",
            default_value=True,
            required=False,
            category="my_wave",
        ),
        ConfigEntry(
            key=CONF_ENABLE_MY_WAVE_BROWSE,
            type=ConfigEntryType.BOOLEAN,
            label="Enable My Wave in Browse",
            description="Show My Wave folder in the Browse section. "
            "When disabled, My Wave will not appear in Browse.",
            default_value=True,
            required=False,
            category="my_wave",
        ),
        ConfigEntry(
            key=CONF_ENABLE_MY_WAVE_PLAYLIST,
            type=ConfigEntryType.BOOLEAN,
            label="Enable My Wave Playlist",
            description="Show My Wave as a virtual playlist in your library. "
            "When disabled, My Wave will not appear in your playlists.",
            default_value=True,
            required=False,
            category="my_wave",
        ),
        ConfigEntry(
            key=CONF_ENABLE_MY_WAVE_RADIO,
            type=ConfigEntryType.BOOLEAN,
            label="Enable My Wave Radio Mode",
            description="Enable radio feedback for My Wave (like/dislike tracks). "
            "When disabled, radio feedback will not be sent to Yandex.",
            default_value=True,
            required=False,
            category="my_wave",
        ),
        # My Wave category (advanced performance tuning)
        ConfigEntry(
            key=CONF_MY_WAVE_MAX_TRACKS,
            type=ConfigEntryType.INTEGER,
            label="My Wave maximum tracks",
            description="Maximum number of tracks to fetch for My Wave playlist. "
            "Lower values load faster but provide fewer tracks. Default: 150.",
            default_value=150,
            required=False,
            category="my_wave",
            advanced=True,
        ),
        ConfigEntry(
            key=CONF_MY_WAVE_BATCH_SIZE,
            type=ConfigEntryType.INTEGER,
            label="My Wave batch count",
            description="Number of API calls to make when loading My Wave in Browse and Discover. "
            "Each call returns ~20-30 tracks from Yandex. "
            "The 'Load more' button always makes 1 call. Default: 3.",
            default_value=3,
            required=False,
            category="my_wave",
            advanced=True,
        ),
        # Liked Tracks category (user-facing toggles)
        ConfigEntry(
            key=CONF_ENABLE_LIKED_TRACKS_BROWSE,
            type=ConfigEntryType.BOOLEAN,
            label="Enable Liked Tracks in Browse",
            description="Show Liked Tracks folder in the Browse section. "
            "When disabled, Liked Tracks will not appear in Browse.",
            default_value=True,
            required=False,
            category="liked_tracks",
        ),
        ConfigEntry(
            key=CONF_ENABLE_LIKED_TRACKS_PLAYLIST,
            type=ConfigEntryType.BOOLEAN,
            label="Enable Liked Tracks Playlist",
            description="Show Liked Tracks as a virtual playlist in your library. "
            "When disabled, Liked Tracks will not appear in your playlists.",
            default_value=True,
            required=False,
            category="liked_tracks",
        ),
        # Liked Tracks category (advanced performance tuning)
        ConfigEntry(
            key=CONF_LIKED_TRACKS_MAX_TRACKS,
            type=ConfigEntryType.INTEGER,
            label="Liked Tracks maximum tracks",
            description="Maximum number of tracks to show in Liked Tracks virtual playlist. "
            "Lower values load faster. Default: 500.",
            default_value=500,
            required=False,
            category="liked_tracks",
            advanced=True,
        ),
        # Global advanced settings (no category)
        ConfigEntry(
            key=CONF_TRACK_BATCH_SIZE,
            type=ConfigEntryType.INTEGER,
            label="Track details batch size",
            description="Number of tracks to fetch in one API request when loading track details. "
            "Higher values are faster but may timeout. "
            "Lower values are more reliable. Default: 50.",
            default_value=50,
            required=False,
            advanced=True,
        ),
        ConfigEntry(
            key=CONF_DISCOVERY_INITIAL_TRACKS,
            type=ConfigEntryType.INTEGER,
            label="Discovery initial tracks",
            description="Number of tracks to show initially in Discover section. "
            "Affects only the first display, all fetched tracks remain available. Default: 5.",
            default_value=5,
            required=False,
            advanced=True,
        ),
        ConfigEntry(
            key=CONF_BROWSE_INITIAL_TRACKS,
            type=ConfigEntryType.INTEGER,
            label="Browse initial tracks",
            description="Number of tracks to show initially when browsing My Wave. "
            "Additional tracks can be loaded with 'Load more' button. Default: 15.",
            default_value=15,
            required=False,
            advanced=True,
        ),
        ConfigEntry(
            key=CONF_BASE_URL,
            type=ConfigEntryType.STRING,
            label="API Base URL",
            description="API endpoint base URL. "
            "Only change if Yandex Music changes their API endpoint. "
            "Default: https://api.music.yandex.net",
            default_value=DEFAULT_BASE_URL,
            required=False,
            advanced=True,
        ),
    )
