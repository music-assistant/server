"""Yandex Music Connect (Ynison) plugin for Music Assistant."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType, ProviderFeature

from .config_helpers import list_yandex_music_instances
from .constants import (
    CONF_ALLOW_PLAYER_SWITCH,
    CONF_DEVICE_ID,
    CONF_MASS_PLAYER_ID,
    CONF_OUTPUT_BIT_DEPTH,
    CONF_OUTPUT_SAMPLE_RATE,
    CONF_PUBLISH_NAME,
    CONF_TOKEN,
    CONF_YM_INSTANCE,
    DEFAULT_DISPLAY_NAME,
    OUTPUT_AUTO,
    PLAYER_ID_AUTO,
    YM_INSTANCE_OWN,
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
    action: str | None = None,  # noqa: ARG001 — required by MA callback signature
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider."""
    if values is None:
        values = {}

    # Migrate legacy config keys (renamed in v1.5.0)
    if "player" in values and CONF_MASS_PLAYER_ID not in values:
        values[CONF_MASS_PLAYER_ID] = values.pop("player")
    if "display_name" in values and CONF_PUBLISH_NAME not in values:
        values[CONF_PUBLISH_NAME] = values.pop("display_name")

    # Discover available yandex_music instances for borrow-mode dropdown
    ym_instances = list_yandex_music_instances(mass)
    ym_instance_ids = {inst_id for inst_id, _ in ym_instances}

    # Determine the currently selected source (borrow vs own)
    selected = cast("str | None", values.get(CONF_YM_INSTANCE))
    if selected is None:
        # Preserve existing own-token configs on upgrade (CONF_TOKEN already set
        # but CONF_YM_INSTANCE absent). Only auto-select borrowing for truly
        # fresh installs with no stored token and exactly one YM instance.
        has_manual_token = bool(values.get(CONF_TOKEN))
        if has_manual_token:
            selected = YM_INSTANCE_OWN
        else:
            selected = ym_instances[0][0] if len(ym_instances) == 1 else YM_INSTANCE_OWN
    borrowing = selected != YM_INSTANCE_OWN and selected in ym_instance_ids

    # Dynamic label
    if borrowing:
        ym_name = next((name for inst_id, name in ym_instances if inst_id == selected), selected)
        label_text = f"Borrowing credentials from Yandex Music instance '{ym_name}'."
    elif selected != YM_INSTANCE_OWN:
        # Referenced YM instance is not currently configured
        label_text = (
            "Selected Yandex Music instance is not available. "
            "Re-select below or fall back to manual token."
        )
    else:
        label_text = (
            "Using a manually entered Yandex Music token. Token refresh is not "
            "automatic in this mode — prefer borrowing from a Yandex Music "
            "instance if possible."
        )

    # Build dropdown options: one per YM instance + "Use own token" sentinel
    source_options = [
        ConfigValueOption(f"Yandex Music: {name}", inst_id) for inst_id, name in ym_instances
    ]
    source_options.append(ConfigValueOption("Use own token (manual entry)", YM_INSTANCE_OWN))

    # Guard against a stale selection pointing at a removed YM instance — the
    # UI would otherwise render with a default that isn't in `options`.
    dropdown_default = selected if borrowing or selected == YM_INSTANCE_OWN else YM_INSTANCE_OWN

    return (
        ConfigEntry(
            key="label_text",
            type=ConfigEntryType.LABEL,
            label=label_text,
        ),
        ConfigEntry(
            key=CONF_YM_INSTANCE,
            type=ConfigEntryType.STRING,
            label="Yandex Music source",
            description="Borrow OAuth credentials from a linked Yandex Music provider "
            "instance. Requires configuring Yandex Music first. Select 'Use own token' "
            "to enter a music token manually.",
            options=source_options,
            default_value=dropdown_default,
            required=True,
        ),
        ConfigEntry(
            key=CONF_TOKEN,
            type=ConfigEntryType.SECURE_STRING,
            label="Yandex Music Token",
            description="Manually pasted Yandex Music OAuth token. Only needed when "
            "not borrowing from a Yandex Music instance.",
            required=not borrowing,
            hidden=borrowing,
            value=cast("str", values.get(CONF_TOKEN)) if values else None,
        ),
        ConfigEntry(
            key=CONF_MASS_PLAYER_ID,
            type=ConfigEntryType.STRING,
            label="Connected Music Assistant Player",
            description="The Music Assistant player connected to this Ynison plugin. "
            "When playback is directed to this device in the Yandex Music app, "
            "the audio will play on the selected player. "
            "Set to 'Auto' to automatically select a currently playing player.",
            default_value=PLAYER_ID_AUTO,
            options=[
                ConfigValueOption("Auto (prefer playing player)", PLAYER_ID_AUTO),
                *(
                    ConfigValueOption(x.display_name, x.player_id)
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
            label="Allow manual player switching",
            description="When enabled, you can select this plugin as a source on any player "
            "to switch playback to that player. When disabled, playback is fixed to the "
            "configured default player.",
            default_value=True,
        ),
        ConfigEntry(
            key=CONF_OUTPUT_SAMPLE_RATE,
            type=ConfigEntryType.STRING,
            label="Output sample rate",
            description="Sample rate for PCM output to the player. "
            "'Auto' selects 44.1 kHz for lossy or 48 kHz for lossless sources.",
            default_value=OUTPUT_AUTO,
            options=[
                ConfigValueOption("Auto (from source quality)", OUTPUT_AUTO),
                ConfigValueOption("44100 Hz (CD)", "44100"),
                ConfigValueOption("48000 Hz", "48000"),
                ConfigValueOption("96000 Hz (Hi-Res)", "96000"),
            ],
            advanced=True,
        ),
        ConfigEntry(
            key=CONF_OUTPUT_BIT_DEPTH,
            type=ConfigEntryType.STRING,
            label="Output bit depth",
            description="Bit depth for PCM output to the player. "
            "'Auto' selects 16-bit for lossy or 24-bit for lossless sources.",
            default_value=OUTPUT_AUTO,
            options=[
                ConfigValueOption("Auto (from source quality)", OUTPUT_AUTO),
                ConfigValueOption("16-bit", "16"),
                ConfigValueOption("24-bit", "24"),
            ],
            advanced=True,
        ),
        ConfigEntry(
            key=CONF_PUBLISH_NAME,
            type=ConfigEntryType.STRING,
            label="Device name in Yandex Music",
            description="How this device appears in the Yandex Music app.",
            default_value=DEFAULT_DISPLAY_NAME,
            advanced=True,
        ),
        ConfigEntry(
            key=CONF_DEVICE_ID,
            type=ConfigEntryType.STRING,
            label="Device ID",
            hidden=True,
            required=False,
        ),
    )
