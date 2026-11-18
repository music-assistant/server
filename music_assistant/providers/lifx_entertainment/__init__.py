"""LIFX Lights Sync plugin: sync LIFX lights to music over the LAN protocol."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

LOGGER = logging.getLogger(__name__)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.enums import ProviderFeature
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES: set[ProviderFeature] = set()


async def setup(
    mass: MusicAssistant,
    manifest: ProviderManifest,
    config: ProviderConfig,
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    from .provider import LifxEntertainmentProvider  # noqa: PLC0415

    return LifxEntertainmentProvider(mass, manifest, config, SUPPORTED_FEATURES)
