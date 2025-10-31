"""AirPlay Player provider for Music Assistant."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ProviderConfig
from music_assistant_models.enums import ConfigEntryType, ProviderFeature
from music_assistant_models.provider import ProviderManifest

from music_assistant.mass import MusicAssistant
from music_assistant.providers.airplay.constants import CONF_ENABLE_LATE_JOIN

from .provider import AirPlayProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry, ConfigValueType, ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES = {
    ProviderFeature.SYNC_PLAYERS,
}


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
            key=CONF_ENABLE_LATE_JOIN,
            type=ConfigEntryType.BOOLEAN,
            default_value=False,
            label="Enable late joining",
            description=(
                "Allow the player to join an existing AirPlay stream instead of "
                "starting a new one. \n NOTE: may not work in all conditions. "
                "If you experience issues or players are not fully in sync, disable this option."
            ),
            category="airplay",
        ),
    )


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return AirPlayProvider(mass, manifest, config, SUPPORTED_FEATURES)
