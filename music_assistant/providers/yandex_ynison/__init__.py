"""Yandex Music Connect (Ynison) plugin for Music Assistant."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType, ProviderFeature

from .constants import (
    CONF_ALLOW_PLAYER_SWITCH,
    CONF_DEVICE_ID,
    CONF_MASS_PLAYER_ID,
    CONF_OUTPUT_BIT_DEPTH,
    CONF_OUTPUT_SAMPLE_RATE,
    CONF_PUBLISH_NAME,
    DEFAULT_DISPLAY_NAME,
    OUTPUT_AUTO,
    PLAYER_ID_AUTO,
)
from .provider import YandexYnisonProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES = {ProviderFeature.AUDIO_SOURCE}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return YandexYnisonProvider(mass, manifest, config, SUPPORTED_FEATURES)


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,  # noqa: ARG001 — required by MA callback signature
    action: str | None = None,  # noqa: ARG001 — auth now runs in the setup flow
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to configure this provider.

    Authentication and the Yandex account source are collected by the interactive
    setup flow (see setup_flow.py); only the genuine playback options live here.
    """
    if values is None:
        values = {}

    # Migrate legacy config keys (renamed in v1.5.0)
    if "player" in values and CONF_MASS_PLAYER_ID not in values:
        values[CONF_MASS_PLAYER_ID] = values.pop("player")
    if "display_name" in values and CONF_PUBLISH_NAME not in values:
        values[CONF_PUBLISH_NAME] = values.pop("display_name")

    return (
        ConfigEntry(
            key=CONF_MASS_PLAYER_ID,
            type=ConfigEntryType.STRING,
            default_value=PLAYER_ID_AUTO,
            options=[
                ConfigValueOption(PLAYER_ID_AUTO),
                *(
                    ConfigValueOption(x.player_id, title=x.display_name)
                    for x in sorted(
                        mass.players.all_players(False, False),
                        key=lambda p: p.display_name.lower(),
                    )
                ),
            ],
            required=True,
        ),
        ConfigEntry(
            key=CONF_ALLOW_PLAYER_SWITCH,
            type=ConfigEntryType.BOOLEAN,
            default_value=True,
        ),
        ConfigEntry(
            key=CONF_OUTPUT_SAMPLE_RATE,
            type=ConfigEntryType.STRING,
            default_value=OUTPUT_AUTO,
            options=[
                ConfigValueOption(OUTPUT_AUTO),
                ConfigValueOption("44100"),
                ConfigValueOption("48000"),
                ConfigValueOption("96000"),
            ],
            advanced=True,
        ),
        ConfigEntry(
            key=CONF_OUTPUT_BIT_DEPTH,
            type=ConfigEntryType.STRING,
            default_value=OUTPUT_AUTO,
            options=[
                ConfigValueOption(OUTPUT_AUTO),
                ConfigValueOption("16"),
                ConfigValueOption("24"),
            ],
            advanced=True,
        ),
        ConfigEntry(
            key=CONF_PUBLISH_NAME,
            type=ConfigEntryType.STRING,
            default_value=DEFAULT_DISPLAY_NAME,
        ),
        ConfigEntry(
            key=CONF_DEVICE_ID,
            type=ConfigEntryType.STRING,
            hidden=True,
            required=False,
        ),
    )
