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
    CONF_AUTO_TRIGGER,
    CONF_FRIENDLY_NAME,
    CONF_ICON_PRESET,
    CONF_INCLUDE_MONITORS,
    CONF_INPUT_DEVICE,
    CONF_TARGET_PLAYER_ID,
    CONF_THUMBNAIL_IMAGE,
    CONF_TRIGGER_THRESHOLD_DBFS,
    DEFAULT_TRIGGER_THRESHOLD_DBFS,
    ICON_PRESET_CUSTOM,
    ICON_PRESETS,
    PLAYER_ID_AUTO,
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
    mass: MusicAssistant,
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,  # noqa: ARG001
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to setup this provider.

    instance_id: id of an existing provider instance (None if new instance setup).
    action: [optional] action key called from config entries UI.
    values: the (intermediate) raw values for config entries sent with the action.
    """
    include_monitors = bool(values.get(CONF_INCLUDE_MONITORS, False)) if values else False
    device_options = await get_available_input_devices(include_monitors=include_monitors)

    player_options = [
        ConfigValueOption(x.player_id, title=x.display_name)
        for x in sorted(
            mass.players.all_players(False, False), key=lambda p: p.display_name.lower()
        )
    ]

    return (
        CONF_ENTRY_WARN_PREVIEW,
        ConfigEntry(
            key=CONF_FRIENDLY_NAME,
            type=ConfigEntryType.STRING,
            default_value="Local Audio Source",
            required=True,
        ),
        ConfigEntry(
            key=CONF_ICON_PRESET,
            type=ConfigEntryType.STRING,
            options=[
                ConfigValueOption(ICON_PRESET_CUSTOM),
                *(ConfigValueOption(key) for key in ICON_PRESETS),
            ],
            default_value=ICON_PRESET_CUSTOM,
            required=True,
        ),
        ConfigEntry(
            key=CONF_THUMBNAIL_IMAGE,
            type=ConfigEntryType.STRING,
            default_value="",
            required=False,
            depends_on=CONF_ICON_PRESET,
            depends_on_value=ICON_PRESET_CUSTOM,
        ),
        ConfigEntry(
            key=CONF_INCLUDE_MONITORS,
            type=ConfigEntryType.BOOLEAN,
            default_value=False,
            required=False,
            immediate_apply=True,
        ),
        ConfigEntry(
            key=CONF_INPUT_DEVICE,
            type=ConfigEntryType.STRING,
            options=device_options,
            default_value=device_options[0].value if device_options else None,
            required=True,
        ),
        ConfigEntry(
            key=CONF_AUTO_TRIGGER,
            type=ConfigEntryType.BOOLEAN,
            default_value=False,
            required=False,
        ),
        ConfigEntry(
            key=CONF_TARGET_PLAYER_ID,
            type=ConfigEntryType.STRING,
            options=[
                ConfigValueOption(PLAYER_ID_AUTO),
                *player_options,
            ],
            default_value=PLAYER_ID_AUTO,
            required=True,
            depends_on=CONF_AUTO_TRIGGER,
            depends_on_value=True,
        ),
        ConfigEntry(
            key=CONF_TRIGGER_THRESHOLD_DBFS,
            type=ConfigEntryType.FLOAT,
            default_value=DEFAULT_TRIGGER_THRESHOLD_DBFS,
            required=False,
            depends_on=CONF_AUTO_TRIGGER,
            depends_on_value=True,
        ),
    )
