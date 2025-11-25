"""Open Subsonic music provider support for MusicAssistant."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry, ConfigValueType, ProviderConfig
from music_assistant_models.enums import ConfigEntryType, ProviderFeature

from music_assistant.constants import CONF_PASSWORD, CONF_PATH, CONF_PORT, CONF_USERNAME

from .sonic_provider import (
    CONF_BASE_URL,
    CONF_ENABLE_LEGACY_AUTH,
    CONF_ENABLE_PODCASTS,
    CONF_NEW_ALBUMS,
    CONF_OVERRIDE_OFFSET,
    CONF_PAGE_SIZE,
    CONF_PLAYED_ALBUMS,
    CONF_RECO_FAVES,
    CONF_RECO_SIZE,
    OpenSonicProvider,
)

if TYPE_CHECKING:
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES = {
    ProviderFeature.LIBRARY_ARTISTS,
    ProviderFeature.LIBRARY_ALBUMS,
    ProviderFeature.LIBRARY_TRACKS,
    ProviderFeature.LIBRARY_PLAYLISTS,
    ProviderFeature.LIBRARY_PLAYLISTS_EDIT,
    ProviderFeature.BROWSE,
    ProviderFeature.SEARCH,
    ProviderFeature.RECOMMENDATIONS,
    ProviderFeature.ARTIST_ALBUMS,
    ProviderFeature.ARTIST_TOPTRACKS,
    ProviderFeature.SIMILAR_TRACKS,
    ProviderFeature.PLAYLIST_TRACKS_EDIT,
    ProviderFeature.PLAYLIST_CREATE,
    ProviderFeature.LIBRARY_PODCASTS,
    ProviderFeature.LIBRARY_PODCASTS_EDIT,
    ProviderFeature.FAVORITE_ALBUMS_EDIT,
    ProviderFeature.FAVORITE_ARTISTS_EDIT,
    ProviderFeature.FAVORITE_TRACKS_EDIT,
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return OpenSonicProvider(mass, manifest, config, SUPPORTED_FEATURES)


async def get_config_entries(
    mass: MusicAssistant,  # noqa: ARG001
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,  # noqa: ARG001
    values: dict[str, ConfigValueType] | None = None,  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider."""
    return (
        ConfigEntry(
            key=CONF_USERNAME,
            type=ConfigEntryType.STRING,
            label="Username",
            required=True,
            description="Your username for this Open Subsonic server",
        ),
        ConfigEntry(
            key=CONF_PASSWORD,
            type=ConfigEntryType.SECURE_STRING,
            label="Password",
            required=True,
            description="The password associated with the username",
        ),
        ConfigEntry(
            key=CONF_BASE_URL,
            type=ConfigEntryType.STRING,
            label="Base URL",
            required=True,
            description="Base URL for the server, e.g. https://subsonic.mydomain.tld",
        ),
        ConfigEntry(
            key=CONF_PORT,
            type=ConfigEntryType.INTEGER,
            label="Port",
            required=False,
            description="Port Number for the server",
        ),
        ConfigEntry(
            key=CONF_PATH,
            type=ConfigEntryType.STRING,
            label="Server Path",
            required=False,
            description="Path to append to the base URL for the Subsonic server, this is likely "
            "empty unless you are path routing on a proxy",
        ),
        ConfigEntry(
            key=CONF_ENABLE_PODCASTS,
            type=ConfigEntryType.BOOLEAN,
            label="Enable Podcasts",
            required=True,
            description="Should the provider query for podcasts as well as music?",
            default_value=True,
        ),
        ConfigEntry(
            key=CONF_ENABLE_LEGACY_AUTH,
            type=ConfigEntryType.BOOLEAN,
            label="Enable Legacy Auth",
            required=True,
            description='Enable OpenSubsonic "legacy" auth support',
            default_value=False,
        ),
        ConfigEntry(
            key=CONF_OVERRIDE_OFFSET,
            type=ConfigEntryType.BOOLEAN,
            label="Force Player Provider Seek",
            required=True,
            description="Some Subsonic implementations advertise that they support seeking when "
            "they do not always. If seeking does not work for you, enable this.",
            default_value=False,
        ),
        ConfigEntry(
            key=CONF_RECO_FAVES,
            type=ConfigEntryType.BOOLEAN,
            label="Recommend Favorites",
            required=True,
            description="Should favorited (starred) items be included as recommendations.",
            default_value=True,
        ),
        ConfigEntry(
            key=CONF_NEW_ALBUMS,
            type=ConfigEntryType.BOOLEAN,
            label="Recommend New Albums",
            required=True,
            description="Should new albums be included as recommendations.",
            default_value=True,
        ),
        ConfigEntry(
            key=CONF_PLAYED_ALBUMS,
            type=ConfigEntryType.BOOLEAN,
            label="Recommend Most Played",
            required=True,
            description="Should most played albums be included as recommendations.",
            default_value=True,
        ),
        ConfigEntry(
            key=CONF_RECO_SIZE,
            type=ConfigEntryType.INTEGER,
            label="Recommendation Limit",
            required=True,
            description="How many recommendations from each enabled type should be included.",
            default_value=10,
        ),
        ConfigEntry(
            key=CONF_PAGE_SIZE,
            type=ConfigEntryType.INTEGER,
            label="Number of items included per server request.",
            required=True,
            description="When enumerating items from the server, how many should be in each "
            "request. Smaller will require more requests but is better for low bandwidth "
            "connections. The Open Subsonic spec says the max value for this is 500 items.",
            default_value=200,
            category="advanced",
        ),
    )
