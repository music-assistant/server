"""
Sonos Player S1 provider for Music Assistant.

Based on the SoCo library for Sonos which uses the legacy/V1 UPnP API.

Note that large parts of this code are copied over from the Home Assistant
integration for Sonos.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry, ConfigValueType
from music_assistant_models.enums import ConfigEntryType

from music_assistant.constants import CONF_ENTRY_MANUAL_DISCOVERY_IPS

from .provider import SonosPlayerProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return SonosPlayerProvider(mass, manifest, config)


async def get_config_entries(
    mass: MusicAssistant,  # noqa: ARG001
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,  # noqa: ARG001
    values: dict[str, ConfigValueType] | None = None,  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to setup this provider.

    instance_id: id of an existing provider instance (None if new instance setup).
    action: [optional] action key called from config entries UI.
    values: the (intermediate) raw values for config entries sent with the action.
    """
    return (
        CONF_ENTRY_MANUAL_DISCOVERY_IPS,
        ConfigEntry(
            key="discovery_timeout",
            type=ConfigEntryType.INTEGER,
            label="Discovery timeout (seconds)",
            description="Timeout for discovering Sonos players on the network",
            default_value=30,
            range=(10, 120),
        ),
    )
