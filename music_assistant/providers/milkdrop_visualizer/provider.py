"""Provider implementation for the MilkDrop Visualizer plugin."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from music_assistant_models.auth import Scope
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

from music_assistant.models.plugin import PluginProvider

from .relay import MilkdropRelay
from .tap import CONF_COLOR_TINT, DEFAULT_COLOR_TINT

if TYPE_CHECKING:
    from collections.abc import Callable

    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.enums import ProviderFeature
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant

CONF_SHOW_ON_DASHBOARDS = "show_on_dashboards"
CONF_COMMAND = "milkdrop_visualizer/config"
CAPABILITY_COMMAND = "milkdrop_visualizer/report_capability"


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
                key=CONF_COLOR_TINT,
                type=ConfigEntryType.BOOLEAN,
                default_value=DEFAULT_COLOR_TINT,
                required=False,
                advanced=True,
            ),
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
        # PROVIDERS_READ (held by guests) so the dashboard viewer user can use these.
        # loaded_in_mass runs as a background task, so a fast reload can leave a stale
        # instance's registration in place; registering a taken name raises, drop it first.
        for command, handler in (
            (CONF_COMMAND, self.get_visualizer_config),
            (CAPABILITY_COMMAND, self.report_capability),
        ):
            self.mass.command_handlers.pop(command, None)
            self._unregister_handles.append(
                self.mass.register_api_command(
                    command,
                    handler,
                    required_scope=Scope.PROVIDERS_READ,
                )
            )

    async def get_visualizer_config(self) -> dict[str, bool]:
        """Return the visualizer settings that apply to every viewer."""
        # read live config: a stale instance may still own the command
        value = await self.mass.config.get_provider_config_value(
            self.instance_id, CONF_SHOW_ON_DASHBOARDS, default=False
        )
        return {CONF_SHOW_ON_DASHBOARDS: bool(value)}

    async def report_capability(
        self,
        webgl2: bool | None = None,
        renderer: str | None = None,
        user_agent: str | None = None,
        gpu: str | None = None,
        render: dict[str, Any] | None = None,
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
        :param render: Measured render performance, for displays whose quality adapts.
        """
        if render is None:
            self.logger.info(
                "Viewer capability: webgl2=%s renderer=%s gpu=%s user_agent=%s",
                webgl2,
                renderer,
                gpu,
                user_agent,
            )
            return
        # best-effort observability: a malformed field must not fail the report
        late_ratio = render.get("late_ratio")
        late_pct = round(late_ratio * 100) if isinstance(late_ratio, (int, float)) else 0
        self.logger.info(
            "Viewer render %s: level=%s pixels=%s fps=%s/%s late=%s%% render=%sms",
            render.get("note"),
            render.get("level"),
            render.get("pixels"),
            render.get("fps"),
            render.get("target_fps"),
            late_pct,
            render.get("render_ms"),
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
