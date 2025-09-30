"""Podcast Index provider for Music Assistant."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry, ConfigValueType
from music_assistant_models.enums import ConfigEntryType, ProviderFeature

from .constants import CONF_API_KEY, CONF_API_SECRET, CONF_STORED_PODCASTS
from .provider import PodcastIndexProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES = {
    ProviderFeature.SEARCH,
    ProviderFeature.BROWSE,
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return PodcastIndexProvider(mass, manifest, config, SUPPORTED_FEATURES)


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to setup this provider.

    instance_id: id of an existing provider instance (None if new instance setup).
    action: [optional] action key called from config entries UI.
    values: the (intermediate) raw values for config entries sent with the action.
    """
    # ruff: noqa: ARG001
    return (
        ConfigEntry(
            key=CONF_API_KEY,
            type=ConfigEntryType.STRING,
            label="API Key",
            required=True,
            description="Your Podcast Index API key. Get your free API credentials at https://api.podcastindex.org/",
        ),
        ConfigEntry(
            key=CONF_API_SECRET,
            type=ConfigEntryType.SECURE_STRING,
            label="API Secret",
            required=True,
            description="Your Podcast Index API secret",
        ),
        ConfigEntry(
            key=CONF_STORED_PODCASTS,
            type=ConfigEntryType.STRING,
            multi_value=True,
            label="Subscribed Podcasts",
            default_value=[],
            required=False,
            hidden=True,
        ),
    )
