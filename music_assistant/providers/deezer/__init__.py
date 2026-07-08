"""Deezer music provider support for MusicAssistant."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry, ConfigValueType
from music_assistant_models.enums import ConfigEntryType

from music_assistant.constants import CONF_ENTRY_UNOFFICIAL_PROVIDER

from .provider import CONF_ARL_TOKEN, SUPPORTED_FEATURES, DeezerProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant import MusicAssistant
    from music_assistant.models import ProviderInstanceType

__all__ = ["DeezerProvider"]


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return DeezerProvider(mass, manifest, config, SUPPORTED_FEATURES)


async def get_config_entries(
    mass: MusicAssistant,  # noqa: ARG001
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,  # noqa: ARG001
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider."""
    return (
        CONF_ENTRY_UNOFFICIAL_PROVIDER,
        ConfigEntry(
            key=CONF_ARL_TOKEN,
            type=ConfigEntryType.SECURE_STRING,
            required=True,
            value=values.get(CONF_ARL_TOKEN) if values else None,
        ),
    )
