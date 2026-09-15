"""
WLED Audio Sync Plugin for Music Assistant.

Drives WLED's built-in "Audio Sync" UDP protocol using the Sendspin
visualization pipeline, so WLED's own sound-reactive effects react to
whatever Music Assistant is playing instead of a physical microphone.

Each configured instance is a sync zone (a UDP port) that appears as a
virtual player in Music Assistant. Grouping that virtual player with a real
speaker player makes the zone's WLED devices react to that speaker's audio.
"""

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
    from .provider import WledProvider  # noqa: PLC0415

    return WledProvider(mass, manifest, config, SUPPORTED_FEATURES)
