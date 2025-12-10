"""Configuration management for the Jellyfin provider.

Handles provider setup, configuration entries, and related constants.
"""

# pylint: disable=cyclic-import
from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.config_entries import (
    ConfigEntry,
    ConfigValueType,
    ProviderConfig,
)
from music_assistant_models.enums import ConfigEntryType, ProviderFeature

from music_assistant.mass import MusicAssistant
from music_assistant.models import ProviderInstanceType
from music_assistant.providers.jellyfin import JellyfinProvider

if TYPE_CHECKING:
    from music_assistant_models.provider import ProviderManifest


# Configuration keys
CONF_URL = "url"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_VERIFY_SSL = "verify_ssl"

# Provider constants
FAKE_ARTIST_PREFIX = "_fake://"

# Supported features
SUPPORTED_FEATURES: set[ProviderFeature] = {
    ProviderFeature.LIBRARY_ARTISTS,
    ProviderFeature.LIBRARY_ALBUMS,
    ProviderFeature.LIBRARY_TRACKS,
    ProviderFeature.LIBRARY_PLAYLISTS,
    ProviderFeature.BROWSE,
    ProviderFeature.SEARCH,
    ProviderFeature.ARTIST_ALBUMS,
    ProviderFeature.SIMILAR_TRACKS,
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration.

    :param mass: MusicAssistant instance.
    :param manifest: Provider manifest.
    :param config: Provider configuration.
    :return: Initialized provider instance.
    """
    return JellyfinProvider(mass, manifest, config, SUPPORTED_FEATURES)


async def get_config_entries(
    _mass: MusicAssistant,
    _instance_id: str | None = None,
    _action: str | None = None,
    _values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider.

    :param mass: MusicAssistant instance (required by framework).
    :param instance_id: ID of an existing provider instance (None if new).
    :param action: Optional action key called from config entries UI.
    :param values: Optional intermediate raw values for config entries.
    :return: Tuple of ConfigEntry objects for provider setup.
    """
    # config flow auth action/step (authenticate button clicked)
    # NOTE: mass, instance_id, action, and values are present for framework compatibility
    # but not currently used by this provider's config entries
    return (
        ConfigEntry(
            key=CONF_URL,
            type=ConfigEntryType.STRING,
            label="Server",
            required=True,
            description="The url of the Jellyfin server to connect to.",
        ),
        ConfigEntry(
            key=CONF_USERNAME,
            type=ConfigEntryType.STRING,
            label="Username",
            required=True,
            description="The username to authenticate to the remote server. For example 'media'.",
        ),
        ConfigEntry(
            key=CONF_PASSWORD,
            type=ConfigEntryType.SECURE_STRING,
            label="Password",
            required=False,
            description="The password to authenticate to the remote server.",
        ),
        ConfigEntry(
            key=CONF_VERIFY_SSL,
            type=ConfigEntryType.BOOLEAN,
            label="Verify SSL",
            required=False,
            description="Whether or not to verify the certificate of SSL/TLS connections.",
            category="advanced",
            default_value=True,
        ),
    )
