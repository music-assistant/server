"""Local Audio Out player provider for Music Assistant."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType, ProviderFeature

from music_assistant.mass import MusicAssistant

from .constants import (
    AUDIO_BACKEND_ALSA,
    AUDIO_BACKEND_AUTO,
    AUDIO_BACKEND_PULSEAUDIO,
    CONF_AUDIO_BACKEND,
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
    mass: MusicAssistant,  # noqa: ARG001
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,  # noqa: ARG001
    values: dict[str, ConfigValueType] | None = None,  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider."""
    # ruff: noqa: ARG001
    entries: list[ConfigEntry] = [
        ConfigEntry(
            key=CONF_VOLUME_CONTROL,
            type=ConfigEntryType.STRING,
            options=[
                ConfigValueOption(VOLUME_CONTROL_HARDWARE),
                ConfigValueOption(VOLUME_CONTROL_SOFTWARE),
                ConfigValueOption(VOLUME_CONTROL_DISABLED),
            ],
            default_value=VOLUME_CONTROL_HARDWARE,
        ),
    ]

    if sys.platform == "linux":
        entries.append(
            ConfigEntry(
                key=CONF_HARDWARE_VOLUME_CEILING,
                type=ConfigEntryType.INTEGER,
                label="Hardware volume ceiling",
                description=(
                    "Sets the PulseAudio sink volume to this level on every startup. "
                    "This attenuates the maximum output level of the hardware. "
                    "Day-to-day volume control uses software scaling within this ceiling. "
                    "Range: 0-100. Default: 50."
                ),
                default_value=DEFAULT_HARDWARE_VOLUME_CEILING,
                required=False,
            ),
        )

    return tuple(entries)


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return LocalAudioProvider(mass, manifest, config, SUPPORTED_FEATURES)
