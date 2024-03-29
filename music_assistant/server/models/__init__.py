"""Server specific/only models."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from .metadata_provider import MetadataProvider
from .music_provider import MusicProvider
from .player_provider import PlayerProvider
from .plugin import PluginProvider

if TYPE_CHECKING:
    from music_assistant.common.models.config_entries import (
        ConfigEntry,
        ConfigValueType,
        ProviderConfig,
    )
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
        mass: MusicAssistant,
        instance_id: str | None = None,
        action: str | None = None,
        values: dict[str, ConfigValueType] | None = None,
    ) -> tuple[ConfigEntry, ...]:
        """
        Return Config entries to setup this provider.

        instance_id: id of an existing provider instance (None if new instance setup).
        action: [optional] action key called from config entries UI.
        values: the (intermediate) raw values for config entries sent with the action.
        """
