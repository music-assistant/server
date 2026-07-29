"""OneDrive filesystem provider for Music Assistant."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .provider import OneDriveFileSystemProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    # mass calls handle_async_init after setup returns; calling it here too
    # would register the stream route twice
    return OneDriveFileSystemProvider(mass, manifest, config)
