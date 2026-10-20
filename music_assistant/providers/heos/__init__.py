"""HEOS Player Provider for HEOS system to work with Music Assistant."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import ProviderFeature

from .provider import HeosPlayerProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES = {
    ProviderFeature.SYNC_PLAYERS,
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize HEOS instance with given configuration."""
    return HeosPlayerProvider(mass, manifest, config, SUPPORTED_FEATURES)
