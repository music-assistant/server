"""Media Assistant Player Provider for Music Assistant."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

from music_assistant.constants import CONF_ENTRY_MANUAL_DISCOVERY_IPS

from .constants import CONF_AUTO_DISCOVER, CONF_ROKU_APP_ID
from .provider import MediaAssistantprovider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return MediaAssistantprovider(mass, manifest, config)


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
        CONF_ENTRY_MANUAL_DISCOVERY_IPS,
        ConfigEntry(
            key=CONF_ROKU_APP_ID,
            type=ConfigEntryType.STRING,
            label="App ID of Media Assistant",
            default_value="782875",
            description="By default, Music Assistant will use the Roku Channel Store version "
            "of Media Assistant (ID: 782875). If you sideloaded the App on your Roku "
            "this will need to be set to (ID: dev).",
            required=False,
            category="advanced",
        ),
        ConfigEntry(
            key=CONF_AUTO_DISCOVER,
            type=ConfigEntryType.BOOLEAN,
            label="Allow automatic Roku discovery",
            default_value=True,
            description="Enable automatic discovery of Roku players.",
            category="advanced",
        ),
    )
