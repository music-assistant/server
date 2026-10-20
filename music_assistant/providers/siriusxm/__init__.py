"""SiriusXM music provider support for MusicAssistant."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .constants import CONF_SXM_PASSWORD, CONF_SXM_USERNAME
from .provider import SUPPORTED_FEATURES, SiriusXMProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant import MusicAssistant
    from music_assistant.models import ProviderInstanceType

__all__ = [
    "CONF_SXM_PASSWORD",
    "CONF_SXM_USERNAME",
    "SiriusXMProvider",
]


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return SiriusXMProvider(mass, manifest, config, SUPPORTED_FEATURES)
