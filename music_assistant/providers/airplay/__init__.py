"""AirPlay Player provider for Music Assistant."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry, ProviderConfig
from music_assistant_models.enums import ConfigEntryType, ProviderFeature
from music_assistant_models.provider import ProviderManifest

from music_assistant.mass import MusicAssistant
from music_assistant.providers.airplay.constants import (
    CONF_ENABLE_LATE_JOIN,
    ENABLE_LATE_JOIN_DEFAULT,
)

from .provider import AirPlayProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
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
            default_value=ENABLE_LATE_JOIN_DEFAULT,
            label="Enable late joining",
            description=(
                "Allow the player to join an existing AirPlay stream instead of "
                "restarting the whole stream. \n NOTE: may not work in all conditions. "
                "If you experience issues or players are not fully in sync, disable this option. \n"
                "Also note that a late joining player may take a few seconds to catch up."
            ),
            category="airplay",
        ),
    )


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return AirPlayProvider(mass, manifest, config, SUPPORTED_FEATURES)
