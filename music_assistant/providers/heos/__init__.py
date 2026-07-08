"""HEOS Player Provider for HEOS system to work with Music Assistant."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType, ProviderFeature

from music_assistant.constants import CONF_IP_ADDRESS

from .constants import CONF_TIMEOUT, DEFAULT_TIMEOUT
from .provider import HeosPlayerProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES = {
    ProviderFeature.SYNC_PLAYERS,
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize HEOS instance with given configuration."""
    return HeosPlayerProvider(mass, manifest, config, SUPPORTED_FEATURES)


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
            key=CONF_IP_ADDRESS,
            type=ConfigEntryType.STRING,
            required=False,
            advanced=True,
            requires_reload=True,
        ),
        ConfigEntry(
            key=CONF_TIMEOUT,
            type=ConfigEntryType.INTEGER,
            default_value=DEFAULT_TIMEOUT,
            required=False,
            range=(10, 60),
            requires_reload=True,
            advanced=True,
        ),
    )
