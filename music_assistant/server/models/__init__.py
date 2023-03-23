"""Server specific/only models."""
from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from .metadata_provider import MetadataProvider
from .music_provider import MusicProvider
from .player_provider import PlayerProvider
from .plugin import PluginProvider

if TYPE_CHECKING:
    from music_assistant.common.models.config_entries import ConfigEntry, ProviderConfig
    from music_assistant.common.models.provider import ProviderManifest
    from music_assistant.server import MusicAssistant


ProviderInstanceType = MetadataProvider | MusicProvider | PlayerProvider | PluginProvider


class ProviderModuleType(Protocol):
    """Model for a provider module to support type hints."""

    @staticmethod
    async def setup(
        mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
    ) -> ProviderInstanceType:
        """Initialize provider(instance) with given configuration."""

    @staticmethod
    async def get_config_entries(
        mass: MusicAssistant, manifest: ProviderManifest
    ) -> tuple[ConfigEntry, ...]:
        """Return Config entries to setup this provider."""
