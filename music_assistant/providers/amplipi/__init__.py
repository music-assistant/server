"""
AmpliPi Player Provider for Music Assistant.

Exposes each (enabled) zone of an AmpliPi multi-zone audio controller as a
Music Assistant player, with native AmpliPi zone grouping and source management.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import ProviderFeature

from .provider import AmpliPiPlayerProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES = {
    ProviderFeature.SYNC_PLAYERS,
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return AmpliPiPlayerProvider(mass, manifest, config, SUPPORTED_FEATURES)
