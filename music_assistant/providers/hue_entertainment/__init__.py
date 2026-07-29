"""
Hue Lights Sync Plugin for Music Assistant.

Syncs Philips Hue lights in Entertainment Areas to music using the
Sendspin visualization pipeline and the Hue Entertainment API (DTLS streaming).

Each Entertainment Area on a paired Hue bridge appears as a virtual player
in Music Assistant. Playing music to the player activates entertainment mode
and makes the lights react to the music in real time.
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
    from .provider import HueEntertainmentProvider  # noqa: PLC0415

    # bridge pairing (and thus the credentials) is handled by the setup flow; an
    # instance only exists once pairing succeeded. A provider that somehow lacks
    # credentials degrades to unavailable in loaded_in_mass rather than failing here.
    return HueEntertainmentProvider(mass, manifest, config, SUPPORTED_FEATURES)
