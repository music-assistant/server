"""iHeartRadio music provider support for Music Assistant."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .constants import CONF_COUNTRY
from .provider import SUPPORTED_FEATURES, IHeartRadioProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant import MusicAssistant
    from music_assistant.models import ProviderInstanceType

__all__ = [
    "CONF_COUNTRY",
    "IHeartRadioProvider",
]


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return IHeartRadioProvider(mass, manifest, config, SUPPORTED_FEATURES)
