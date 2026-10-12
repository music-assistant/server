"""Provider implementation for the MilkDrop Visualizer plugin."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

from music_assistant.models.plugin import PluginProvider

from .relay import MilkdropRelay
from .tap import CONF_COLOR_TINT, DEFAULT_COLOR_TINT

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.enums import ProviderFeature
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant


class MilkdropVisualizerProvider(PluginProvider):
    """Streams waveform frames from what a player is playing to the web frontend."""

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

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return the (options) config entries for the MilkDrop Visualizer provider."""
        return (
            ConfigEntry(
                key=CONF_COLOR_TINT,
                type=ConfigEntryType.BOOLEAN,
                default_value=DEFAULT_COLOR_TINT,
                required=False,
                advanced=True,
            ),
        )

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
