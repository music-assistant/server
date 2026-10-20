"""Deezer music provider support for MusicAssistant."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .provider import SUPPORTED_FEATURES, DeezerProvider

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
