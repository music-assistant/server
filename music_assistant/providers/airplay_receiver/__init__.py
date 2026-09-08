"""
AirPlay Receiver plugin for Music Assistant.

This plugin allows Music Assistant to receive AirPlay audio streams
and use them as a source for any player. It uses shairport-sync to
receive the AirPlay streams and outputs them as PCM audio.

The provider runs as a single instance that advertises one AirPlay receiver
(one shairport-sync daemon and one AudioSource) per connected Music
Assistant player.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant.providers.airplay_receiver.provider import (
    AirPlayReceiverProvider,
    airplay_receiver_ports,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

__all__ = [
    "AirPlayReceiverProvider",
    "airplay_receiver_ports",
    "setup",
]


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return AirPlayReceiverProvider(mass, manifest, config)
