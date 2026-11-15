"""BBC Sounds music provider support for MusicAssistant."""

from typing import TYPE_CHECKING

from .provider import BBCSoundsProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant import MusicAssistant
    from music_assistant.models import ProviderInstanceType

__all__ = ["BBCSoundsProvider"]


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Create new BBCSoundsProvider instance."""
    return BBCSoundsProvider(mass, manifest, config)
