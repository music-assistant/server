"""Provider implementation for the MilkDrop Visualizer plugin."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

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
# a display reporting more often than this is logged at debug, so it cannot flood the log
REPORT_COOLDOWN = 30.0
# bucket every display we cannot place under one name, so it shares a single cooldown
UNKNOWN_DISPLAY = "unknown"
# errors cool down apart from render reports, so a chatty renderer cannot bury one
REPORT_KIND_ERROR = "error"
REPORT_KIND_RENDER = "render"


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
        self._last_report: dict[tuple[str, str], float] = {}

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
        # loaded_in_mass runs as a background task, so a reload can still run this for an
        # instance that is already on its way out; it would take the live one's route and
        # commands and then tear them down with itself, as both unregister by name.
        if self.unloading:
            return
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
        :param render: Measured render performance, for displays whose quality adapts.
        :param error: Failure the display hit, logged as a warning instead of a report.
        :param dashboard_id: The reporting display's dashboard id, so several displays
            stay apart in the log.
        """
        display = await self._resolve_display(dashboard_id)
        user_agent = _trim(user_agent)
        if error is not None:
            due = self._report_is_due(display, REPORT_KIND_ERROR)
            log = self.logger.warning if due else self.logger.debug
            log("Viewer error on %s: %s (user_agent=%s)", display, _trim(error), user_agent)
            return
        due = self._report_is_due(display, REPORT_KIND_RENDER)
        log = self.logger.info if due else self.logger.debug
        if render is None:
            log(
                "Viewer capability on %s: webgl2=%s renderer=%s gpu=%s user_agent=%s",
                display,
                webgl2,
                _trim(renderer),
                _trim(gpu),
                user_agent,
            )
            return
        # best-effort observability: a malformed field must not fail the report
        late_ratio = render.get("late_ratio")
        late_pct = round(late_ratio * 100) if isinstance(late_ratio, (int, float)) else 0
        blocked_ratio = render.get("blocked_ratio")
        blocked_pct = round(blocked_ratio * 100) if isinstance(blocked_ratio, (int, float)) else 0
        gpu_part = ""
        if render.get("gpu_warp") is not None:
            gpu_part = (
                f" gpu={_trim(render.get('gpu_warp'))}/{_trim(render.get('gpu_blur')) or '-'}"
                f"/{_trim(render.get('gpu_comp')) or '-'}ms"
            )
        preset = _trim(render.get("preset") or "")
        preset_part = f' preset="{preset}"' if preset else ""
        log(
            "Viewer render %s on %s: level=%s pixels=%s fps=%s/%s"
            " late=%s%% blocked=%s%% render=%sms%s%s",
            _trim(render.get("note")),
            display,
            _trim(render.get("level")),
            _trim(render.get("pixels")),
            _trim(render.get("fps")),
            _trim(render.get("target_fps")),
            late_pct,
            blocked_pct,
            _trim(render.get("render_ms")),
            gpu_part,
            preset_part,
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

    async def _resolve_display(self, dashboard_id: str | None) -> str:
        """
        Name the reporting display, keeping the log key out of a viewer's hands.

        Only an id with a live dashboard session is taken at face value; everything else
        shares one bucket, so a client cannot mint fresh ids to dodge the cooldown.

        :param dashboard_id: The dashboard id the viewer claims to be.
        """
        if dashboard_id is None:
            return UNKNOWN_DISPLAY
        sessions = await self.mass.dashboard.get_dashboard_sessions()
        if any(session.dashboard_id == dashboard_id for session in sessions):
            return dashboard_id
        return UNKNOWN_DISPLAY

    def _report_is_due(self, display: str, kind: str) -> bool:
        """
        Whether this display's report is worth a log line, or is coming in too fast.

        :param display: The reporting display, as resolved by `_resolve_display`.
        :param kind: Which cooldown the report draws on, one of the REPORT_KIND_* values.
        """
        now = time.monotonic()
        last = self._last_report.get((display, kind))
        if last is not None and now - last < REPORT_COOLDOWN:
            return False
        self._last_report[display, kind] = now
        return True


def _trim(value: object) -> str | None:
    """Cap a viewer-reported value and flatten newlines, so it cannot forge log lines."""
    if value is None:
        return None
    return str(value).replace("\n", " ").replace("\r", " ")[:MAX_REPORT_FIELD_LEN]
