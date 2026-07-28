"""
Local Audio Source plugin for Music Assistant.

Captures raw PCM from a user-selected PulseAudio/PipeWire source and
exposes it to Music Assistant as an AudioSource, streamed to any player
through an ultra-low-latency CUSTOM stream.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .provider import LocalAudioSourceProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return LocalAudioSourceProvider(mass, manifest, config)
