"""Yandex Disk filesystem provider for Music Assistant."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .provider import YandexDiskFileSystemProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant import MusicAssistant
    from music_assistant.models import ProviderInstanceType


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize a Yandex Disk provider instance."""
    return YandexDiskFileSystemProvider(mass, manifest, config)
