"""Smart DJ plugin provider."""
from __future__ import annotations

from typing import TYPE_CHECKING

from .provider import SmartDJProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant

SUPPORTED_FEATURES: set[str] = set()

async def setup(
    mass: MusicAssistant,
    manifest: ProviderManifest,
    config: ProviderConfig,
) -> SmartDJProvider:
    """Set up Smart DJ."""
    return SmartDJProvider(mass, manifest, config, SUPPORTED_FEATURES)
