"""MusicCast for MusicAssistant."""

from typing import TYPE_CHECKING

from music_assistant_models.enums import ProviderFeature

from music_assistant.mass import MusicAssistant
from music_assistant.models import ProviderInstanceType

from .provider import MusicCastProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

SUPPORTED_FEATURES = {
    ProviderFeature.SYNC_PLAYERS,
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return MusicCastProvider(mass, manifest, config, SUPPORTED_FEATURES)
