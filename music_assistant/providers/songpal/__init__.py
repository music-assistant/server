"""Sony SongPal player provider for Music Assistant."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

# No provider-level features. Player sync and wireless groups are out of scope.
SUPPORTED_FEATURES: set[Any] = set()


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize the SongPal provider."""
    from .provider import SongpalPlayerProvider

    return SongpalPlayerProvider(mass, manifest, config, SUPPORTED_FEATURES)
