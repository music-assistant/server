"""nicovideo support for Music Assistant."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant.mass import MusicAssistant
from music_assistant.models import ProviderInstanceType
from music_assistant.providers.nicovideo.config import get_config_entries_impl
from music_assistant.providers.nicovideo.provider import NicovideoMusicProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import (
        ConfigEntry,
        ConfigValueType,
        ProviderConfig,
    )
    from music_assistant_models.provider import ProviderManifest


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return NicovideoMusicProvider(mass, manifest, config)


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider."""
    return await get_config_entries_impl(mass, instance_id, action, values)
