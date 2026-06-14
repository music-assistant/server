"""Pandora music provider support for Music Assistant."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption, ConfigValueType
from music_assistant_models.enums import ConfigEntryType, ProviderFeature
from music_assistant_models.errors import SetupFailedError

from music_assistant.constants import (
    CONF_ENTRY_UNOFFICIAL_PROVIDER,
    CONF_PASSWORD,
    CONF_SOCKS_URL,
    CONF_USERNAME,
)

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
        CONF_ENTRY_UNOFFICIAL_PROVIDER,
        ConfigEntry(
            key=CONF_USERNAME,
            type=ConfigEntryType.STRING,
            required=True,
        ),
        ConfigEntry(
            key=CONF_PASSWORD,
            type=ConfigEntryType.SECURE_STRING,
            required=True,
        ),
        ConfigEntry(
            key=CONF_QUALITY,
            type=ConfigEntryType.STRING,
            required=True,
            default_value=QUALITY_STANDARD,
            options=[
                ConfigValueOption(QUALITY_STANDARD),
                ConfigValueOption(QUALITY_HIGH),
            ],
        ),
        ConfigEntry(
            key=CONF_SOCKS_URL,
            type=ConfigEntryType.STRING,
            required=False,
            default_value="",
            advanced=True,
        ),
        ConfigEntry(
            key=CONF_TAKEOVER_ACTION,
            type=ConfigEntryType.ACTION,
            action=CONF_TAKEOVER_ACTION,
            required=False,
        ),
    )
