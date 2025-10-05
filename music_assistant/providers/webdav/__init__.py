"""WebDAV filesystem provider for Music Assistant."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType, ProviderFeature

from music_assistant.constants import CONF_PASSWORD, CONF_USERNAME

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
            label="Content type",
            options=[
                ConfigValueOption("Music", "music"),
                ConfigValueOption("Audiobooks", "audiobooks"),
                ConfigValueOption("Podcasts", "podcasts"),
            ],
            default_value="music",
            description="The type of content stored on this WebDAV server",
            hidden=instance_id is not None,
        ),
        ConfigEntry(
            key=CONF_URL,
            type=ConfigEntryType.STRING,
            label="WebDAV URL",
            required=True,
            description="The base URL of your WebDAV server (e.g., https://example.com/webdav)",
        ),
        ConfigEntry(
            key=CONF_USERNAME,
            type=ConfigEntryType.STRING,
            label="Username",
            required=False,
            description="Username for WebDAV authentication (leave empty for no authentication)",
        ),
        ConfigEntry(
            key=CONF_PASSWORD,
            type=ConfigEntryType.SECURE_STRING,
            label="Password",
            required=False,
            description="Password for WebDAV authentication",
        ),
        ConfigEntry(
            key=CONF_VERIFY_SSL,
            type=ConfigEntryType.BOOLEAN,
            label="Verify SSL certificate",
            default_value=False,
            description="Verify SSL certificates when connecting to HTTPS WebDAV servers",
        ),
        ConfigEntry(
            key="missing_album_artist_action",
            type=ConfigEntryType.STRING,
            label="Action when album artist tag is missing",
            options=[
                ConfigValueOption("Use track artist(s)", "track_artist"),
                ConfigValueOption("Use folder name", "folder_name"),
                ConfigValueOption("Use 'Various Artists'", "various_artists"),
            ],
            default_value="various_artists",
            description="What to do when a track is missing the album artist tag",
        ),
        ConfigEntry(
            key="ignore_album_playlists",
            type=ConfigEntryType.BOOLEAN,
            label="Ignore playlists in album folders",
            default_value=True,
            description="Ignore playlist files found in album subdirectories",
        ),
    )
