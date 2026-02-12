"""KION Music provider support for Music Assistant."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption, ConfigValueType
from music_assistant_models.enums import ConfigEntryType, ProviderFeature

from .constants import (
    CONF_ACTION_CLEAR_AUTH,
    CONF_BROWSE_INITIAL_TRACKS,
    CONF_DISCOVERY_INITIAL_TRACKS,
    CONF_ENABLE_MY_MIX_BROWSE,
    CONF_ENABLE_MY_MIX_PLAYLIST,
    CONF_ENABLE_MY_MIX_RADIO,
    CONF_ENABLE_RECOMMENDATIONS,
    CONF_MY_MIX_BATCH_SIZE,
    CONF_MY_MIX_MAX_TRACKS,
    CONF_QUALITY,
    CONF_TOKEN,
    CONF_TRACK_BATCH_SIZE,
    QUALITY_HIGH,
    QUALITY_LOSSLESS,
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
    return KionMusicProvider(mass, manifest, config, features)


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
        ConfigEntry(
            key=CONF_MY_MIX_MAX_TRACKS,
            type=ConfigEntryType.INTEGER,
            label="My Mix maximum tracks",
            description="Maximum number of tracks to fetch for My Mix playlist. "
            "Lower values load faster but provide fewer tracks. Default: 150.",
            default_value=150,
            required=False,
        ),
        ConfigEntry(
            key=CONF_MY_MIX_BATCH_SIZE,
            type=ConfigEntryType.INTEGER,
            label="My Mix batch count",
            description="Number of API calls to make when loading My Mix in Browse and Discover. "
            "Each call returns ~20-30 tracks from Yandex. "
            "The 'Load more' button always makes 1 call. Default: 3.",
            default_value=3,
            required=False,
        ),
        ConfigEntry(
            key=CONF_TRACK_BATCH_SIZE,
            type=ConfigEntryType.INTEGER,
            label="Track details batch size",
            description="Number of tracks to fetch in one API request when loading track details. "
            "Higher values are faster but may timeout. "
            "Lower values are more reliable. Default: 50.",
            default_value=50,
            required=False,
        ),
        ConfigEntry(
            key=CONF_DISCOVERY_INITIAL_TRACKS,
            type=ConfigEntryType.INTEGER,
            label="Discovery initial tracks",
            description="Number of tracks to show initially in Discover section. "
            "Affects only the first display, all fetched tracks remain available. Default: 5.",
            default_value=5,
            required=False,
        ),
        ConfigEntry(
            key=CONF_BROWSE_INITIAL_TRACKS,
            type=ConfigEntryType.INTEGER,
            label="Browse initial tracks",
            description="Number of tracks to show initially when browsing My Mix. "
            "Additional tracks can be loaded with 'Load more' button. Default: 15.",
            default_value=15,
            required=False,
        ),
        ConfigEntry(
            key=CONF_ENABLE_RECOMMENDATIONS,
            type=ConfigEntryType.BOOLEAN,
            label="Enable Discover (Recommendations)",
            description="Show My Mix recommendations on the home page. "
            "When enabled, recommendations refresh each time you reload the page "
            "for fresh discoveries.",
            default_value=False,  # Experimental feature - disabled by default
            required=False,
        ),
        ConfigEntry(
            key=CONF_ENABLE_MY_MIX_BROWSE,
            type=ConfigEntryType.BOOLEAN,
            label="Enable My Mix in Browse",
            description="Show My Mix folder in the Browse section. "
            "When disabled, My Mix will not appear in Browse.",
            default_value=False,  # Experimental feature - disabled by default
            required=False,
        ),
        ConfigEntry(
            key=CONF_ENABLE_MY_MIX_PLAYLIST,
            type=ConfigEntryType.BOOLEAN,
            label="Enable My Mix Playlist",
            description="Show My Mix as a virtual playlist in your library. "
            "When disabled, My Mix will not appear in your playlists.",
            default_value=False,  # Experimental feature - disabled by default
            required=False,
        ),
        ConfigEntry(
            key=CONF_ENABLE_MY_MIX_RADIO,
            type=ConfigEntryType.BOOLEAN,
            label="Enable My Mix Radio Mode",
            description="Enable radio feedback for My Mix (like/dislike tracks). "
            "When disabled, radio feedback will not be sent to Yandex.",
            default_value=False,  # Experimental feature - disabled by default
            required=False,
        ),
    )
