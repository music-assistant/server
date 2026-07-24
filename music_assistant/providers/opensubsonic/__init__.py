"""Open Subsonic music provider support for MusicAssistant."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry, ConfigValueType, ProviderConfig
from music_assistant_models.enums import ConfigEntryType, ProviderFeature

from .sonic_provider import (
    CONF_ENABLE_LEGACY_AUTH,
    CONF_ENABLE_PODCASTS,
    CONF_NEW_ALBUMS,
    CONF_PAGE_SIZE,
    CONF_PLAYED_ALBUMS,
    CONF_RAW_FILE,
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
    ProviderFeature.LYRICS,
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
            key=CONF_ENABLE_PODCASTS,
            type=ConfigEntryType.BOOLEAN,
            required=True,
            default_value=True,
        ),
        ConfigEntry(
            key=CONF_ENABLE_LEGACY_AUTH,
            type=ConfigEntryType.BOOLEAN,
            required=True,
            default_value=False,
        ),
        ConfigEntry(
            key=CONF_RECO_FAVES,
            type=ConfigEntryType.BOOLEAN,
            required=True,
            default_value=True,
        ),
        ConfigEntry(
            key=CONF_NEW_ALBUMS,
            type=ConfigEntryType.BOOLEAN,
            required=True,
            default_value=True,
        ),
        ConfigEntry(
            key=CONF_PLAYED_ALBUMS,
            type=ConfigEntryType.BOOLEAN,
            required=True,
            default_value=True,
        ),
        ConfigEntry(
            key=CONF_RECO_SIZE,
            type=ConfigEntryType.INTEGER,
            required=True,
            default_value=10,
        ),
        ConfigEntry(
            key=CONF_RAW_FILE,
            type=ConfigEntryType.BOOLEAN,
            required=False,
            default_value=True,
        ),
        ConfigEntry(
            key=CONF_PAGE_SIZE,
            type=ConfigEntryType.INTEGER,
            required=True,
            default_value=200,
            advanced=True,
        ),
    )
