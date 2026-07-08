"""WebDAV filesystem provider for Music Assistant."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType, ProviderFeature

from music_assistant.constants import CONF_PASSWORD, CONF_USERNAME
from music_assistant.providers.filesystem_local.constants import (
    CONF_ENTRY_IGNORE_ALBUM_PLAYLISTS,
    CONF_ENTRY_LIBRARY_SYNC_AUDIOBOOKS,
    CONF_ENTRY_LIBRARY_SYNC_PLAYLISTS,
    CONF_ENTRY_LIBRARY_SYNC_PODCASTS,
    CONF_ENTRY_LIBRARY_SYNC_TRACKS,
    CONF_ENTRY_MISSING_ALBUM_ARTIST,
    CONF_ENTRY_PROPAGATE_GENRES,
)

from .constants import CONF_CONTENT_TYPE, CONF_URL, CONF_VERIFY_SSL
from .provider import WebDAVFileSystemProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


# Supported features
SUPPORTED_FEATURES = {
    ProviderFeature.BROWSE,
    ProviderFeature.SEARCH,
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    prov = WebDAVFileSystemProvider(mass, manifest, config)
    await prov.handle_async_init()
    return prov


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider."""
    # ruff: noqa: ARG001
    return (
        ConfigEntry(
            key=CONF_CONTENT_TYPE,
            type=ConfigEntryType.STRING,
            options=[
                ConfigValueOption("music"),
                ConfigValueOption("audiobooks"),
                ConfigValueOption("podcasts"),
            ],
            default_value="music",
            hidden=instance_id is not None,
        ),
        ConfigEntry(
            key=CONF_URL,
            type=ConfigEntryType.STRING,
            required=True,
        ),
        ConfigEntry(
            key=CONF_USERNAME,
            type=ConfigEntryType.STRING,
            required=False,
        ),
        ConfigEntry(
            key=CONF_PASSWORD,
            type=ConfigEntryType.SECURE_STRING,
            required=False,
        ),
        ConfigEntry(
            key=CONF_VERIFY_SSL,
            type=ConfigEntryType.BOOLEAN,
            default_value=False,
        ),
        CONF_ENTRY_MISSING_ALBUM_ARTIST,
        CONF_ENTRY_IGNORE_ALBUM_PLAYLISTS,
        CONF_ENTRY_LIBRARY_SYNC_TRACKS,
        CONF_ENTRY_LIBRARY_SYNC_PLAYLISTS,
        CONF_ENTRY_LIBRARY_SYNC_PODCASTS,
        CONF_ENTRY_LIBRARY_SYNC_AUDIOBOOKS,
        CONF_ENTRY_PROPAGATE_GENRES,
    )
