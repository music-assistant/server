"""
Provider for Bose SoundTouch speakers.

Following the Bose SoundTouch end of life, this provider keeps the speakers usable
within Music Assistant: it detects them on the network, exposes native control
(power, volume, transport, source and multiroom grouping) and maps the physical
preset buttons to Music Assistant content. Audio playback is delegated to a linked
playback protocol (such as DLNA) via the standard protocol linking mechanism.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import ProviderFeature

from .provider import BoseSoundTouchProvider

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
    return BoseSoundTouchProvider(mass, manifest, config, SUPPORTED_FEATURES)
