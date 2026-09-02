"""Telmore Musik musicprovider support for MusicAssistant."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant.providers.music247e import SUPPORTED_FEATURES
from music_assistant.providers.telmore.provider import TelmoreMusikProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    # setup is called when the user wants to setup a new provider instance.
    # you are free to do any preflight checks here and but you must return
    #  an instance of the provider.
    return TelmoreMusikProvider(mass, manifest, config, SUPPORTED_FEATURES)
