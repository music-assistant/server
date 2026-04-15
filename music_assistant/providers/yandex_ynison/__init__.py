"""Yandex Music Connect (Ynison) plugin for Music Assistant."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType, ProviderFeature
from music_assistant_models.errors import LoginFailed

from .config_helpers import find_sibling_token
from .constants import (
    CONF_ACTION_AUTH_QR,
    CONF_ACTION_CLEAR_AUTH,
    CONF_ALLOW_PLAYER_SWITCH,
    CONF_DEVICE_ID,
    CONF_MASS_PLAYER_ID,
    CONF_OUTPUT_BIT_DEPTH,
    CONF_OUTPUT_SAMPLE_RATE,
    CONF_PUBLISH_NAME,
    CONF_REMEMBER_SESSION,
    CONF_TOKEN,
    CONF_X_TOKEN,
    DEFAULT_DISPLAY_NAME,
    OUTPUT_AUTO,
    PLAYER_ID_AUTO,
)
from .provider import YandexYnisonProvider
from .yandex_auth import perform_qr_auth

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
    instance_id: str | None = None,
    action: str | None = None,
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

    # Pre-fill token from sibling instance if this instance has no token yet
    sibling_token, sibling_x_token = find_sibling_token(mass, instance_id)
    if not values.get(CONF_TOKEN) and sibling_token:
        values[CONF_TOKEN] = sibling_token
        if sibling_x_token:
            values[CONF_X_TOKEN] = sibling_x_token

    # Handle QR auth action
    if action == CONF_ACTION_AUTH_QR:
        session_id = values.get("session_id")
        if not session_id:
            raise LoginFailed("Missing session_id for QR authentication")
        x_token, music_token = await perform_qr_auth(mass, str(session_id))
        values[CONF_TOKEN] = music_token
        if values.get(CONF_REMEMBER_SESSION, True):
            values[CONF_X_TOKEN] = x_token
        else:
            values[CONF_X_TOKEN] = None

    # Handle clear auth action
    if action == CONF_ACTION_CLEAR_AUTH:
        values[CONF_TOKEN] = None
        values[CONF_X_TOKEN] = None

    # Check if user is authenticated
    is_authenticated = bool(values.get(CONF_TOKEN))
    token_from_sibling = is_authenticated and sibling_token == values.get(CONF_TOKEN)

    # Dynamic label text
    if not is_authenticated:
        label_text = (
            "Scan a QR code with the Yandex app on your phone to authenticate.\n\n"
            "Alternatively, you can enter a music token manually in the advanced settings."
        )
    elif action == CONF_ACTION_AUTH_QR:
        label_text = "Authenticated to Yandex Music. Don't forget to save to complete setup."
    elif token_from_sibling:
        label_text = (
            "Authenticated to Yandex Music (token reused from existing instance).\n"
            "Re-authenticate if you need a different account."
        )
    else:
        label_text = "Authenticated to Yandex Music."

    return (
        # Status label
        ConfigEntry(
            key="label_text",
            type=ConfigEntryType.LABEL,
            label=label_text,
        ),
        # QR authentication (primary)
        ConfigEntry(
            key=CONF_ACTION_AUTH_QR,
            type=ConfigEntryType.ACTION,
            label="Login with QR code",
            description="Opens a QR code page — scan it with the Yandex app on your phone.",
            action=CONF_ACTION_AUTH_QR,
            action_label="Login with QR code",
            hidden=is_authenticated,
        ),
        # Remember session toggle
        ConfigEntry(
            key=CONF_REMEMBER_SESSION,
            type=ConfigEntryType.BOOLEAN,
            label="Remember session (auto-refresh token)",
            description="When enabled, stores a long-lived session token to automatically "
            "refresh your music token when it expires. When disabled, you must "
            "re-authenticate manually when the token expires.",
            default_value=True,
            hidden=is_authenticated,
        ),
        # Clear auth
        ConfigEntry(
            key=CONF_ACTION_CLEAR_AUTH,
            type=ConfigEntryType.ACTION,
            label="Reset authentication",
            description="Clear the current authentication details.",
            action=CONF_ACTION_CLEAR_AUTH,
            action_label="Reset authentication",
            hidden=not is_authenticated,
        ),
        # Token (populated by QR action or manual entry)
        ConfigEntry(
            key=CONF_TOKEN,
            type=ConfigEntryType.SECURE_STRING,
            label="Yandex Music Token",
            description="Music token — populated automatically by QR login, "
            "or enter manually. See the documentation for how to obtain it.",
            required=True,
            hidden=is_authenticated,
            value=cast("str", values.get(CONF_TOKEN)) if values else None,
        ),
        # x_token (internal, always hidden)
        ConfigEntry(
            key=CONF_X_TOKEN,
            type=ConfigEntryType.SECURE_STRING,
            label="Session token",
            hidden=True,
            required=False,
            value=cast("str", values.get(CONF_X_TOKEN)) if values else None,
        ),
        # Target MA player
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
        # Allow manual player switching
        ConfigEntry(
            key=CONF_ALLOW_PLAYER_SWITCH,
            type=ConfigEntryType.BOOLEAN,
            label="Allow manual player switching",
            description="When enabled, you can select this plugin as a source on any player "
            "to switch playback to that player. When disabled, playback is fixed to the "
            "configured default player.",
            default_value=True,
        ),
        # Output sample rate
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
        # Output bit depth
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
        # Device name in Yandex Music app
        ConfigEntry(
            key=CONF_PUBLISH_NAME,
            type=ConfigEntryType.STRING,
            label="Device name in Yandex Music",
            description="How this device appears in the Yandex Music app.",
            default_value=DEFAULT_DISPLAY_NAME,
            advanced=True,
        ),
        # Device ID (internal, hidden)
        ConfigEntry(
            key=CONF_DEVICE_ID,
            type=ConfigEntryType.STRING,
            label="Device ID",
            hidden=True,
            required=False,
        ),
    )
