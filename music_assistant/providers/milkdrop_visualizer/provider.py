"""Provider implementation for the MilkDrop Visualizer plugin."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant.models.plugin import PluginProvider

from .relay import MilkdropRelay

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.enums import ProviderFeature
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant


class MilkdropVisualizerProvider(PluginProvider):
    """Streams waveform frames from playing Sendspin groups to the web frontend."""

    def __init__(
        self,
        mass: MusicAssistant,
        manifest: ProviderManifest,
        config: ProviderConfig,
        supported_features: set[ProviderFeature],
    ) -> None:
        """Initialize the provider."""
        super().__init__(mass, manifest, config, supported_features)
        self._relay = MilkdropRelay(self)

    async def loaded_in_mass(self) -> None:
        """Register the relay route once fully loaded."""
        await super().loaded_in_mass()
        self._relay.setup()

    async def unload(self, is_removed: bool = False) -> None:
        """
        Handle unload/close of the provider.

        :param is_removed: True when the provider is removed from the configuration.
        """
        await self._relay.close()
