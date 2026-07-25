"""
Hue Lights Sync Plugin for Music Assistant.

Syncs Philips Hue lights in Entertainment Areas to music using the
Sendspin visualization pipeline and the Hue Entertainment API (DTLS streaming).

Each Entertainment Area on a paired Hue bridge appears as a virtual player
in Music Assistant. Playing music to the player activates entertainment mode
and makes the lights react to the music in real time.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType, ProviderFeature

from .constants import (
    COLOR_MODES,
    CONF_BRIGHTNESS,
    CONF_COLOR_MODE,
    CONF_HUE_LATENCY_MS,
    DEFAULT_COLOR_MODE,
    DEFAULT_HUE_LATENCY_MS,
)

LOGGER = logging.getLogger(__name__)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES: set[ProviderFeature] = set()


async def setup(
    mass: MusicAssistant,
    manifest: ProviderManifest,
    config: ProviderConfig,
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    from .provider import HueEntertainmentProvider  # noqa: PLC0415

    # bridge pairing (and thus the credentials) is handled by the setup flow; an
    # instance only exists once pairing succeeded. A provider that somehow lacks
    # credentials degrades to unavailable in loaded_in_mass rather than failing here.
    return HueEntertainmentProvider(mass, manifest, config, SUPPORTED_FEATURES)


async def get_config_entries(
    mass: MusicAssistant,  # noqa: ARG001
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,  # noqa: ARG001
    values: dict[str, ConfigValueType] | None = None,  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """
    Return the (options) config entries for the Hue Entertainment provider.

    Bridge pairing runs in the interactive setup flow (see ``setup_flow.py``); only the
    playback/visualization settings are configured here.
    """
    return (
        ConfigEntry(
            key=CONF_BRIGHTNESS,
            type=ConfigEntryType.INTEGER,
            default_value=100,
            range=(0, 100),
            category="settings",
        ),
        ConfigEntry(
            key=CONF_COLOR_MODE,
            type=ConfigEntryType.STRING,
            default_value=DEFAULT_COLOR_MODE,
            options=[ConfigValueOption(mode, title=mode.capitalize()) for mode in COLOR_MODES],
            category="settings",
        ),
        ConfigEntry(
            key=CONF_HUE_LATENCY_MS,
            type=ConfigEntryType.INTEGER,
            default_value=DEFAULT_HUE_LATENCY_MS,
            range=(0, 3000),
            immediate_apply=True,
            category="settings",
        ),
    )
