"""Pandora music provider support for Music Assistant."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import ProviderFeature

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
    return PandoraProvider(mass, manifest, config, SUPPORTED_FEATURES)
