"""Yandex Music provider support for Music Assistant."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption, ConfigValueType
from music_assistant_models.enums import ConfigEntryType

from .constants import (
    CONF_ACTION_CLEAR_AUTH,
    CONF_QUALITY,
    CONF_TOKEN,
    QUALITY_HIGH,
    QUALITY_LOSSLESS,
)
from .provider import YandexMusicProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration.

    :param mass: The Music Assistant instance.
    :param manifest: The provider manifest.
    :param config: The provider configuration.
    :return: The initialized provider instance.
    """
    return YandexMusicProvider(mass, manifest, config)


async def get_config_entries(
    mass: MusicAssistant,  # noqa: ARG001
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """Return configuration entries required to set up the Yandex Music provider.

    :param mass: The Music Assistant instance.
    :param instance_id: Optional instance identifier for the provider.
    :param action: Optional action to perform (e.g., authenticate or clear auth).
    :param values: Dictionary of current configuration values.
    :return: Tuple of ConfigEntry objects representing the configuration steps.
    """
    if values is None:
        values = {}

    # Handle clear auth action
    if action == CONF_ACTION_CLEAR_AUTH:
        values[CONF_TOKEN] = None

    # Check if user is authenticated
    is_authenticated = bool(values.get(CONF_TOKEN))

    if is_authenticated:
        # User is authenticated - show status and clear option
        return (
            ConfigEntry(
                key="label_ok",
                type=ConfigEntryType.LABEL,
                label="You are authenticated with Yandex Music",
            ),
            ConfigEntry(
                key=CONF_ACTION_CLEAR_AUTH,
                type=ConfigEntryType.ACTION,
                label="Reset authentication",
                description="Reset the authentication for Yandex Music",
                action=CONF_ACTION_CLEAR_AUTH,
                value=None,
            ),
            ConfigEntry(
                key=CONF_QUALITY,
                type=ConfigEntryType.STRING,
                label="Audio quality",
                description="Select preferred audio quality.\n\n"
                "High: MP3 320 kbps\n\n"
                "Lossless: FLAC (requires Yandex Music Plus subscription)",
                options=[
                    ConfigValueOption("High (320 kbps)", QUALITY_HIGH),
                    ConfigValueOption("Lossless (FLAC)", QUALITY_LOSSLESS),
                ],
                default_value=QUALITY_HIGH,
            ),
            # Hidden field to preserve the token
            ConfigEntry(
                key=CONF_TOKEN,
                type=ConfigEntryType.SECURE_STRING,
                label="Yandex Music Token",
                hidden=True,
                value=cast("str", values.get(CONF_TOKEN)) if values else None,
            ),
        )

    # User is not authenticated - show token input
    return (
        ConfigEntry(
            key="label_instructions",
            type=ConfigEntryType.LABEL,
            label="To use Yandex Music, you need to provide your OAuth token.\n\n"
            "You can obtain your token from browser developer tools:\n\n"
            "1. Open https://music.yandex.ru in your browser\n"
            "2. Log in to your Yandex account\n"
            "3. Open browser Developer Tools (F12)\n"
            "4. Go to Application/Storage > Cookies\n"
            "5. Find the 'Session_id' cookie and copy its value\n\n"
            "Alternatively, use yandex-music-token tool:\n"
            "https://github.com/MarshalX/yandex-music-token",
        ),
        ConfigEntry(
            key=CONF_TOKEN,
            type=ConfigEntryType.SECURE_STRING,
            label="Yandex Music Token",
            description="Enter your Yandex Music OAuth token",
            required=True,
        ),
        ConfigEntry(
            key=CONF_QUALITY,
            type=ConfigEntryType.STRING,
            label="Audio quality",
            description="Select preferred audio quality.\n\n"
            "High: MP3 320 kbps\n\n"
            "Lossless: FLAC (requires Yandex Music Plus subscription)",
            options=[
                ConfigValueOption("High (320 kbps)", QUALITY_HIGH),
                ConfigValueOption("Lossless (FLAC)", QUALITY_LOSSLESS),
            ],
            default_value=QUALITY_HIGH,
        ),
    )
