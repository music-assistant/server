"""
Sonos Player S1 provider for Music Assistant.

Based on the SoCo library for Sonos which uses the legacy/V1 UPnP API.

Note that large parts of this code are copied over from the Home Assistant
integration for Sonos.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import ProviderFeature

from .provider import SonosPlayerProvider

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
    return SonosPlayerProvider(mass, manifest, config, SUPPORTED_FEATURES)
