"""Provider implementation for the MilkDrop Visualizer plugin."""

from __future__ import annotations

import logging
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
CONFIG_COMMAND = "milkdrop_visualizer/config"
CAPABILITY_COMMAND = "milkdrop_visualizer/report_capability"
# viewer-reported strings go straight into the server log, cap what a display can write
MAX_REPORT_FIELD_LEN = 500


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
        self._unregister_handles: list[Callable[[], None]] = []

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return the (options) config entries for the MilkDrop Visualizer provider."""
        return (
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
        # PROVIDERS_READ (held by guests) so the dashboard viewer user can use these
        for command, handler in (
            (CONFIG_COMMAND, self.get_visualizer_config),
            (CAPABILITY_COMMAND, self.report_capability),
        ):
            self._unregister_handles.append(
                self.mass.register_api_command(
                    command,
                    handler,
                    required_scope=Scope.PROVIDERS_READ,
                )
            )

    async def get_visualizer_config(self) -> dict[str, bool]:
        """Return the visualizer settings that apply to every viewer."""
        return {CONF_SHOW_ON_DASHBOARDS: bool(self.config.get_value(CONF_SHOW_ON_DASHBOARDS))}

    async def report_capability(
        self,
        webgl2: bool | None = None,
        renderer: str | None = None,
        user_agent: str | None = None,
        gpu: str | None = None,
        error: str | None = None,
        dashboard_id: str | None = None,
    ) -> None:
        """
        Record a display's reported render capabilities in the server log.

        Cast and TV receivers vary wildly in graphics support and have no
        reachable console, so this is the only place their WebGL2 support
        (and whether MilkDrop actually rendered) becomes visible.

        :param webgl2: Whether the display's browser has a working WebGL2 context.
        :param renderer: What the display ended up rendering with.
        :param user_agent: The display browser's user agent string.
        :param gpu: What the display's GL context reports it draws with.
        :param error: Failure the display hit.
        :param dashboard_id: The reporting display's dashboard id, so several displays
            stay apart in the log.
        """
        level = logging.WARNING if error or webgl2 is False else logging.DEBUG
        self.logger.log(
            level,
            "Viewer capability on %s: webgl2=%s renderer=%s gpu=%s user_agent=%s error=%s",
            _trim(dashboard_id),
            webgl2,
            _trim(renderer),
            _trim(gpu),
            _trim(user_agent),
            _trim(error),
        )

    async def unload(self, is_removed: bool = False) -> None:
        """
        Handle unload/close of the provider.

        :param is_removed: True when the provider is removed from the configuration.
        """
        for unregister in self._unregister_handles:
            unregister()
        self._unregister_handles.clear()
        await self._relay.close()


def _trim(value: object) -> str | None:
    """Cap a viewer-reported value and flatten newlines, so it cannot forge log lines."""
    if value is None:
        return None
    return str(value).replace("\n", " ").replace("\r", " ")[:MAX_REPORT_FIELD_LEN]
