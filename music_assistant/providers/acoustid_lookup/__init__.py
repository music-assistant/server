"""AcoustID Lookup audio analysis provider."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .provider import AcoustidLookupProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.enums import ProviderFeature
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant

SUPPORTED_FEATURES: set[ProviderFeature] = set()


async def setup(
    mass: MusicAssistant,
    manifest: ProviderManifest,
    config: ProviderConfig,
) -> AcoustidLookupProvider:
    """Set up the AcoustID Lookup provider."""
    return AcoustidLookupProvider(mass, manifest, config, SUPPORTED_FEATURES)
