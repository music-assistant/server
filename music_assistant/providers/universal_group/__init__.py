"""
Universal Group Player provider.

Create universal groups to group speakers of different
protocols/ecosystems to play the same audio (but not in sync).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import ProviderFeature

from .player import UniversalGroupPlayer
from .provider import UniversalGroupProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES = {ProviderFeature.CREATE_GROUP_PLAYER, ProviderFeature.REMOVE_GROUP_PLAYER}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return UniversalGroupProvider(mass, manifest, config, SUPPORTED_FEATURES)


__all__ = (
    "UniversalGroupPlayer",
    "UniversalGroupProvider",
    "setup",
)
