"""Local Audio Out player provider for Music Assistant."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType, ProviderFeature

from music_assistant.mass import MusicAssistant

from .constants import (
    CONF_VOLUME_CONTROL,
    VOLUME_CONTROL_DISABLED,
    VOLUME_CONTROL_HARDWARE,
    VOLUME_CONTROL_SOFTWARE,
)
from .provider import LocalAudioProvider

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
    """Return Config entries to setup this provider."""
    # ruff: noqa: ARG001
    return (
        ConfigEntry(
            key=CONF_VOLUME_CONTROL,
            type=ConfigEntryType.STRING,
            label="Volume control mode",
            options=[
                ConfigValueOption(title="Hardware (preferred)", value=VOLUME_CONTROL_HARDWARE),
                ConfigValueOption(title="Software", value=VOLUME_CONTROL_SOFTWARE),
                ConfigValueOption(title="Disabled", value=VOLUME_CONTROL_DISABLED),
            ],
            default_value=VOLUME_CONTROL_HARDWARE,
            description="Hardware uses OS/ALSA-level volume control. "
            "Software applies volume scaling to PCM audio data. "
            "Disabled passes audio at full volume.",
        ),
    )


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return LocalAudioProvider(mass, manifest, config, SUPPORTED_FEATURES)
