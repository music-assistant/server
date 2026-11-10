"""
Universal Player provider.

Auto-creates virtual players that merge multiple protocol players
(AirPlay, Chromecast, DLNA, Squeezelite, SendSpin) for the same device
into a single unified player when no native provider exists.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .player import UniversalPlayer
from .provider import UniversalPlayerProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant import MusicAssistant
    from music_assistant.models import ProviderInstanceType


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return UniversalPlayerProvider(mass, manifest, config)


__all__ = (
    "UniversalPlayer",
    "UniversalPlayerProvider",
    "setup",
)
