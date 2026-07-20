"""
Player Provider for the Sendspin Audio Protocol.

https://github.com/Sendspin-Protocol/spec
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

from music_assistant.constants import CONF_ENTRY_MANUAL_DISCOVERY_IPS
from music_assistant.providers.sendspin.constants import (
    CONF_ALLOW_UNENCRYPTED,
    CONF_MIN_PIN_LENGTH,
    DEFAULT_MIN_PIN_LENGTH,
)
from music_assistant.providers.sendspin.provider import SendspinProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return SendspinProvider(mass, manifest, config)


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
            key=CONF_ALLOW_UNENCRYPTED,
            type=ConfigEntryType.BOOLEAN,
            default_value=True,
        ),
        ConfigEntry(
            key=CONF_MIN_PIN_LENGTH,
            type=ConfigEntryType.INTEGER,
            range=(4, 12),
            default_value=DEFAULT_MIN_PIN_LENGTH,
        ),
    )
