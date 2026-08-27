"""Open Home Player Provider for Music Assistant."""

# The Linn/OpenHome Media provider allows you to stream music to an OpenHome Media compliant renderer as a Music Assistant player
# This allows use and control of devices such as a Linn Products Ltd streamer
# It will allow you to control transport and volume and see the details for the currently playing item.

from __future__ import annotations

from typing import TYPE_CHECKING

from .provider import OpenHomePlayerProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES = None


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return OpenHomePlayerProvider(mass, manifest, config)
