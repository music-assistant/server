"""BookOrbit provider for Music Assistant."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import ProviderFeature

from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES = {
    ProviderFeature.LIBRARY_AUDIOBOOKS,
    ProviderFeature.LIBRARY_ARTISTS,
    ProviderFeature.BROWSE,
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider instance."""
    return BookOrbitProvider(mass, manifest, config, SUPPORTED_FEATURES)


class BookOrbitProvider(MusicProvider):
    """BookOrbit MusicProvider (audiobooks)."""

    async def handle_async_init(self) -> None:
        """Initialize client after config is available - implemented in later tasks."""
        return
