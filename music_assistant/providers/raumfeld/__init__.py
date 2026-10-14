"""
Teufel Raumfeld Player provider for Music Assistant.

Talks to the Raumfeld host via the ``hassfeld`` library and exposes each Raumfeld
room as a Music Assistant player, mapping MA sync-groups onto Raumfeld zones.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .constants import DEFAULT_PORT, HOST_ERRORS
from .provider import RaumfeldPlayerProvider

__all__ = [
    "DEFAULT_PORT",
    "HOST_ERRORS",
    "RaumfeldPlayerProvider",
    "setup",
]

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.enums import ProviderFeature
    from music_assistant_models.provider import ProviderManifest

    from music_assistant import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES: set[ProviderFeature] = set()


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return RaumfeldPlayerProvider(mass, manifest, config, SUPPORTED_FEATURES)
