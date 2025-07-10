"""
Plugin provider for Music Assistant to report Plex playback timeline events.

This provider integrates with Plex to monitor and report playback timeline updates
back to Music Assistant, enabling enhanced synchronization and playback tracking.
It does not provide audio sources, but acts as a plugin to relay timeline events.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry, ConfigValueType, ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

from music_assistant.models.plugin import PluginProvider

from .timeline import PlexTimelineReporter


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return PlexConnect(mass, manifest, config)


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
    # ruff: noqa: ARG001
    # Config Entries are used to configure the Provider if needed.
    # See the models of ConfigEntry and ConfigValueType for more information what is supported.
    # The ConfigEntry is a dataclass that represents a single configuration entry.
    # The ConfigValueType is an Enum that represents the type of value that
    # can be stored in a ConfigEntry.
    # If your provider does not need any configuration, you can return an empty tuple.
    return ()


class PlexConnect(PluginProvider):
    """Plugin provider to report Plex playback timeline."""

    def __init__(
        self, mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
    ) -> None:
        """Initialize PlexConnect plugin provider."""
        super().__init__(mass, manifest, config)
        self._reporter: PlexTimelineReporter | None = None

    @property
    def supported_features(self) -> set:
        """No audio source; plugin only reports timeline."""
        return set()

    async def loaded_in_mass(self) -> None:
        """Set up the Plex timeline reporter after provider is loaded."""
        self._reporter = PlexTimelineReporter(self.mass)
        await self._reporter.setup()

    async def unload(self, is_removed: bool = False) -> None:
        """Clean up reporter on unload."""
        if self._reporter:
            await self._reporter.close()
        await super().unload(is_removed)
