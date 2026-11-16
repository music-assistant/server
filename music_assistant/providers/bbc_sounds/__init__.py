"""BBC Sounds music provider support for Music Assistant."""

from typing import TYPE_CHECKING

from music_assistant.providers.bbc_sounds.browse import BrowseManager
from music_assistant.providers.bbc_sounds.now_playing import NowPlayingManager
from music_assistant.providers.bbc_sounds.provider import BBCSoundsProvider
from music_assistant.providers.bbc_sounds.streaming import StreamManager

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant import MusicAssistant
    from music_assistant.models import ProviderInstanceType

__all__ = ["BBCSoundsProvider", "BrowseManager", "NowPlayingManager", "StreamManager"]


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Create new BBCSoundsProvider instance."""
    return BBCSoundsProvider(mass, manifest, config)
