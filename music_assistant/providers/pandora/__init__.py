"""Pandora music provider support for Music Assistant."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption, ConfigValueType
from music_assistant_models.enums import ConfigEntryType, ProviderFeature
from music_assistant_models.errors import SetupFailedError

from music_assistant.constants import CONF_PASSWORD, CONF_SOCKS_URL, CONF_USERNAME

from .constants import CONF_QUALITY, CONF_TAKEOVER_ACTION, QUALITY_HIGH, QUALITY_STANDARD
from .provider import PandoraProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant import MusicAssistant
    from music_assistant.models import ProviderInstanceType

# Supported Features - Pandora is primarily a radio service
SUPPORTED_FEATURES = {
    ProviderFeature.BROWSE,
    ProviderFeature.LIBRARY_RADIOS,
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider instance with given configuration."""
    username = config.get_value(CONF_USERNAME)
    password = config.get_value(CONF_PASSWORD)

    # Type-safe validation
    if (
        not username
        or not password
        or not isinstance(username, str)
        or not isinstance(password, str)
        or not username.strip()
        or not password.strip()
    ):
        raise SetupFailedError("Username and password are required")

    return PandoraProvider(mass, manifest, config, SUPPORTED_FEATURES)


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """Return configuration entries for this provider."""
    # ruff: noqa: ARG001
    if action == CONF_TAKEOVER_ACTION and instance_id:
        provider = cast("PandoraProvider|None", mass.get_provider(instance_id))
        if provider is not None:
            await provider.takeover_stream()

    return (
        ConfigEntry(
            key=CONF_USERNAME,
            type=ConfigEntryType.STRING,
            label="Username",
            description="Your Pandora username or email address",
            required=True,
        ),
        ConfigEntry(
            key=CONF_PASSWORD,
            type=ConfigEntryType.SECURE_STRING,
            label="Password",
            description="Your Pandora password",
            required=True,
        ),
        ConfigEntry(
            key=CONF_QUALITY,
            type=ConfigEntryType.STRING,
            label="Audio quality",
            description=(
                "Audio quality to request from Pandora. High quality is only available with an "
                "active Pandora subscription. If your account is not eligible for high-quality "
                "streaming, standard quality will be used regardless of this setting."
            ),
            required=True,
            default_value=QUALITY_STANDARD,
            options=[
                ConfigValueOption("Standard (64 kbps AAC+)", QUALITY_STANDARD),
                ConfigValueOption("High (192 kbps MP3)", QUALITY_HIGH),
            ],
        ),
        ConfigEntry(
            key=CONF_SOCKS_URL,
            type=ConfigEntryType.STRING,
            label="Socks proxy server",
            description="This socks proxy is only used to route Pandora network traffic through."
            "\n\nThe server address should be written as:\n"
            "<code>ip_address:port</code> (or <code>socks5://ip_address:port</code>)",
            required=False,
            default_value="",
            advanced=True,
        ),
        ConfigEntry(
            key=CONF_TAKEOVER_ACTION,
            type=ConfigEntryType.ACTION,
            label="Take over stream",
            description=(
                "Pandora only allows one active stream at a time per account. You can request that "
                "Pandora terminate any existing stream on other devices and allow streaming here. "
                "You must manually restart playback after performing this action."
            ),
            action=CONF_TAKEOVER_ACTION,
            required=False,
        ),
    )
