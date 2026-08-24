"""
Player Provider for the Sendspin Audio Protocol.

https://github.com/Sendspin-Protocol/spec
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant.providers.sendspin.provider import SendspinProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return SendspinProvider(mass, manifest, config)
