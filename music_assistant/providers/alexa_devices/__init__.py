"""Alexa Devices player provider for Music Assistant, built on aioamazondevices."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .provider import AlexaDevicesProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return AlexaDevicesProvider(mass, manifest, config, set())
