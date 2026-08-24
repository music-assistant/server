"""NFS filesystem provider for Music Assistant."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .provider import NFSFileSystemProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    # base_path will be the path where we're going to mount the NFS export
    base_path = f"/tmp/{config.instance_id}"  # noqa: S108
    return NFSFileSystemProvider(mass, manifest, config, base_path)
