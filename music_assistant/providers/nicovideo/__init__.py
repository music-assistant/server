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
    mass: MusicAssistant,  # noqa: ARG001
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,  # noqa: ARG001
    values: dict[str, ConfigValueType] | None = None,  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider."""
    return await get_config_entries_impl()
