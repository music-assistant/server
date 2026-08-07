"""Filesystem musicprovider support for MusicAssistant."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from .base import (
    FileSystemProvider,
    exists,
    isdir,
    isfile,
    ismount,
    makedirs,
    scandir,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

__all__ = [
    "FileSystemProvider",
    "LocalFileSystemProvider",
    "exists",
    "isdir",
    "isfile",
    "ismount",
    "makedirs",
    "scandir",
]


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return LocalFileSystemProvider(mass, manifest, config)


class LocalFileSystemProvider(FileSystemProvider):
    """
    Implementation of a musicprovider for (local) files.

    Reads ID3 tags from file and falls back to parsing filename.
    Optionally reads metadata from nfo files and images in folder structure <artist>/<album>.
    Supports m3u files for playlists.
    """

    # mounted/local disks are cheap to scan, so proactively analyze by default
    _background_analysis_default_enabled: ClassVar[bool] = True
