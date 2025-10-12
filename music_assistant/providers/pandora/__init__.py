"""Pandora music provider support for Music Assistant."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry, ConfigValueType
from music_assistant_models.enums import ConfigEntryType, ProviderFeature
from music_assistant_models.errors import SetupFailedError

from music_assistant.constants import CONF_PASSWORD, CONF_USERNAME

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
    )
