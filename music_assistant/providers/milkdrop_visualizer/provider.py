"""Provider implementation for the MilkDrop Visualizer plugin."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.auth import Scope
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

from music_assistant.models.plugin import PluginProvider

from .relay import MilkdropRelay

if TYPE_CHECKING:
    from collections.abc import Callable

    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.enums import ProviderFeature
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant

CONF_SHOW_ON_DASHBOARDS = "show_on_dashboards"
CONF_COMMAND = "milkdrop_visualizer/config"


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
        self._unregister_handles: list[Callable[[], None]] = []

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return the configuration entries for this provider."""
        return (
            *await super().get_config_entries(),
            ConfigEntry(
                key=CONF_SHOW_ON_DASHBOARDS,
                type=ConfigEntryType.BOOLEAN,
                default_value=False,
            ),
        )

    async def loaded_in_mass(self) -> None:
        """Register the relay route once fully loaded."""
        await super().loaded_in_mass()
        self._relay.setup()
        # PROVIDERS_READ (held by guests) so a cast dashboard, which runs as the
        # dashboard viewer and has no preferences of its own, can read this.
        # A provider with depends_on loads twice, and registering a name twice
        # raises: without dropping the previous handler the command stays bound
        # to the earlier instance and keeps answering from its stale config.
        self.mass.command_handlers.pop(CONF_COMMAND, None)
        self._unregister_handles.append(
            self.mass.register_api_command(
                CONF_COMMAND,
                self.get_visualizer_config,
                required_scope=Scope.PROVIDERS_READ,
            )
        )

    async def get_visualizer_config(self) -> dict[str, bool]:
        """Return the visualizer settings that apply to every viewer."""
        # Read through the config controller rather than this instance's snapshot,
        # so the answer is current even if an older instance still owns the command.
        value = await self.mass.config.get_provider_config_value(
            self.instance_id, CONF_SHOW_ON_DASHBOARDS, default=False
        )
        return {CONF_SHOW_ON_DASHBOARDS: bool(value)}

    async def unload(self, is_removed: bool = False) -> None:
        """
        Handle unload/close of the provider.

        :param is_removed: True when the provider is removed from the configuration.
        """
        for unregister in self._unregister_handles:
            unregister()
        self._unregister_handles.clear()
        await self._relay.close()
