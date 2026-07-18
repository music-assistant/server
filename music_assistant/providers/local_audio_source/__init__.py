"""
Local Audio Source plugin for Music Assistant.

Captures raw PCM from a user-selected PulseAudio/PipeWire source and
exposes it to Music Assistant as an AudioSource, streamed to any player
through an ultra-low-latency CUSTOM stream.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType

from music_assistant.constants import CONF_ENTRY_WARN_PREVIEW

from .constants import (
    CONF_FRIENDLY_NAME,
    CONF_ICON_PRESET,
    CONF_INPUT_DEVICE,
    CONF_THUMBNAIL_IMAGE,
    ICON_PRESET_CUSTOM,
    ICON_PRESETS,
)
from .helpers import get_available_input_devices
from .provider import LocalAudioSourceProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return LocalAudioSourceProvider(mass, manifest, config)


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
    device_options = await get_available_input_devices()

    return (
        CONF_ENTRY_WARN_PREVIEW,
        ConfigEntry(
            key=CONF_FRIENDLY_NAME,
            type=ConfigEntryType.STRING,
            label="Display Name",
            default_value="Local Audio Source",
            required=True,
        ),
        ConfigEntry(
            key=CONF_ICON_PRESET,
            type=ConfigEntryType.STRING,
            label="Thumbnail",
            description="Pick a bundled icon or use a custom image URL.",
            options=[
                ConfigValueOption(ICON_PRESET_CUSTOM, title="Custom URL"),
                *(ConfigValueOption(key, title=label) for key, label in ICON_PRESETS.items()),
            ],
            default_value=ICON_PRESET_CUSTOM,
            required=True,
        ),
        ConfigEntry(
            key=CONF_THUMBNAIL_IMAGE,
            type=ConfigEntryType.STRING,
            label="Thumbnail image URL",
            description="Direct URL to an SVG/PNG/JPG, e.g. https://example.com/icon.svg",
            default_value="",
            required=False,
            depends_on=CONF_ICON_PRESET,
            depends_on_value=ICON_PRESET_CUSTOM,
        ),
        ConfigEntry(
            key=CONF_INPUT_DEVICE,
            type=ConfigEntryType.STRING,
            label="Audio Input Device",
            description="Select a PulseAudio/PipeWire capture source (pactl list sources).",
            options=device_options,
            default_value=device_options[0].value if device_options else None,
            required=True,
        ),
    )
