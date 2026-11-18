"""LIFX entertainment bridge: one in-process Sendspin visualizer client per light area."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import TYPE_CHECKING, cast

from aiosendspin.models.core import ClientHelloPayload
from aiosendspin.models.core import DeviceInfo as SendspinDeviceInfo
from aiosendspin.models.types import UndefinedField
from aiosendspin.models.visualizer import (
    ClientHelloVisualizerSpectrum,
    ClientHelloVisualizerSupport,
    VisualizerStatePayload,
)
from lifx_entertainment import EntertainmentSession, LightColorCommand, area_client_id
from music_assistant_models.enums import PlayerType

from music_assistant.providers.light_sync.analyzer import AudioAnalyzer
from music_assistant.providers.sendspin.bridge_role import (
    COLOR_BRIDGE_ROLE_ID,
    VISUALIZER_BRIDGE_ROLE_ID,
    BridgeColorRole,
    BridgeVisualizerRole,
)

from .constants import (
    SPECTRUM_BINS,
    SPECTRUM_F_MAX,
    SPECTRUM_F_MIN,
    SPECTRUM_SCALE,
)
from .settings import (
    get_brightness,
    get_color_mode,
    get_light_latency_ms,
    get_power_on,
    get_transition_ms,
)

if TYPE_CHECKING:
    from aiosendspin.models.core import ServerStatePayload
    from aiosendspin.models.visualizer import BeatTiming
    from aiosendspin.server import (
        ExternalStreamStartRequest,
        SendspinClient,
        SendspinServer,
    )
    from aiosendspin.server.roles.visualizer.features import ExtractedFrame
    from lifx_entertainment import EntertainmentArea

    from music_assistant.providers.sendspin.provider import SendspinProvider

    from .provider import LifxEntertainmentProvider

LOGGER = logging.getLogger(__name__)

# LIFX recommends at most 20 messages per second per device.
_RENDER_RATE_HZ = 20
_RENDER_PERIOD_S = 1.0 / _RENDER_RATE_HZ
# The analyzer's filters (channel rise/decay, bass baseline) are tuned for ~20 Hz input.
_VISUALIZER_RATE_HZ = 20

_SESSION_START_ATTEMPTS = 3
_SESSION_START_BACKOFF_S = 1.0
# Wait this long after a stream ends before restoring the lights: a new stream
# usually starts right away on a track change.
_STOP_DEBOUNCE_S = 2.0


class LifxEntertainmentBridge:
    """Music sync for a single LIFX light area."""

    def __init__(
        self,
        provider: LifxEntertainmentProvider,
        area: EntertainmentArea,
        sendspin_server: SendspinServer,
    ) -> None:
        """Initialize the bridge."""
        self.provider = provider
        self.mass = provider.mass
        self.area = area
        self.sendspin_server = sendspin_server
        self.logger = LOGGER.getChild(f"bridge.{area.name}")

        self._session: EntertainmentSession | None = None
        self._analyzer: AudioAnalyzer | None = None
        self._sendspin_client: SendspinClient | None = None
        self._is_streaming = False
        self._stop_debounce_task: asyncio.Task[None] | None = None
        self._start_task: asyncio.Task[None] | None = None
        self._render_handle: asyncio.TimerHandle | None = None
        self._session_starting: bool = False
        self._latency_us: int = get_light_latency_ms(self.provider.config) * 1000
        self._transition_ms: int = get_transition_ms(self.provider.config)
        self._power_on: bool = get_power_on(self.provider.config)

    async def start(self) -> None:
        """Start the bridge — register as an in-process Sendspin visualizer client."""
        self._analyzer = AudioAnalyzer(
            channels=self.area.channels,
            color_mode=get_color_mode(self.provider.config),
            brightness=get_brightness(self.provider.config),
        )

        client_id = f"lifx-{area_client_id(self.area.id, self.provider.instance_id)}"

        sendspin_prov: SendspinProvider | None = self.mass.get_provider("sendspin")  # type: ignore[assignment]
        if sendspin_prov:
            sendspin_prov.register_bridge_player_type(client_id, PlayerType.LIGHT)

        support = ClientHelloVisualizerSupport(buffer_capacity=2048)
        request = VisualizerStatePayload(
            rate_max=_VISUALIZER_RATE_HZ,
            # Peaks drive the lights until the beat schedule has been computed.
            types=["beat", "peak", "spectrum"],
            spectrum=ClientHelloVisualizerSpectrum(
                n_disp_bins=SPECTRUM_BINS,
                scale=SPECTRUM_SCALE,
                f_min=SPECTRUM_F_MIN,
                f_max=SPECTRUM_F_MAX,
            ),
        )
        hello = ClientHelloPayload(
            client_id=client_id,
            name=f"LIFX: {self.area.name}",
            version=1,
            supported_roles=[VISUALIZER_BRIDGE_ROLE_ID, COLOR_BRIDGE_ROLE_ID],
            device_info=SendspinDeviceInfo(
                manufacturer="LIFX",
                product_name="LIFX Light Group",
            ),
            visualizer_support=support,
        )
        self._sendspin_client = self.sendspin_server.register_external_player(
            hello, on_stream_start=self._on_external_stream_start
        )

        if viz_roles := self._sendspin_client.roles_by_family("visualizer"):
            viz_role = cast("BridgeVisualizerRole", viz_roles[0])
            viz_role.set_callbacks(
                on_frame=self._on_visualizer_frame,
                on_beats=self._on_beats,
                on_beats_clear=self._on_beats_clear,
                on_stream_start=self._on_stream_start,
                on_stream_clear=self._on_stream_clear,
                on_stream_end=self._on_stream_end,
            )
            viz_role.setup_visualizer(request)
        if color_roles := self._sendspin_client.roles_by_family("color"):
            color_role = cast("BridgeColorRole", color_roles[0])
            color_role.set_callbacks(on_color=self._on_color)
        self._sendspin_client.attach_preinitialized_roles()

        self.logger.info(
            "LIFX bridge started for area '%s' (%d lights, %d channels)",
            self.area.name,
            len(self.area.devices),
            len(self.area.channels),
        )
        self.logger.debug(
            "LIFX bridge channels for area '%s': %s",
            self.area.name,
            [
                f"id={c.channel_id} serial={c.service_id} name={c.name} "
                f"zones={c.zone_start}-{c.zone_end} pos={c.position}"
                for c in self.area.channels
            ],
        )

    async def stop(self) -> None:
        """Stop the bridge."""
        self._cancel_render_loop()
        if self._stop_debounce_task and not self._stop_debounce_task.done():
            self._stop_debounce_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._stop_debounce_task
        self._stop_debounce_task = None

        if self._sendspin_client:
            await self.sendspin_server.remove_client(self._sendspin_client.client_id)
            self._sendspin_client = None

        # Cancel an in-flight start so it can't adopt a session after we stop.
        if self._start_task and not self._start_task.done():
            self._start_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._start_task
        self._start_task = None

        await self._stop_session()
        self.logger.debug("LIFX bridge stopped for area '%s'", self.area.name)

    def update_settings(
        self,
        color_mode: str | None = None,
        brightness: int | None = None,
        light_latency_ms: int | None = None,
        transition_ms: int | None = None,
        power_on: bool | None = None,
    ) -> None:
        """Update analyzer/bridge settings without restarting the bridge."""
        if self._analyzer:
            self._analyzer.update_settings(
                color_mode=color_mode,
                brightness=brightness,
            )
        if light_latency_ms is not None:
            self._latency_us = light_latency_ms * 1000
        if transition_ms is not None:
            self._transition_ms = transition_ms
            if self._session is not None:
                self._session.transition_ms = transition_ms
        if power_on is not None:
            # Applied on the next session start.
            self._power_on = power_on

    async def _start_session(self) -> None:
        """Snapshot + power on the lights and begin streaming, with retry."""
        api = self.provider.lifx_api
        if api is None:
            self._session_starting = False
            return

        session = EntertainmentSession(
            api,
            self.area,
            transition_ms=self._transition_ms,
            power_on=self._power_on,
        )
        # Until adopted, the finally closes the session so a failed or cancelled start
        # never leaves the lights half switched on.
        adopted = False
        try:
            for attempt in range(_SESSION_START_ATTEMPTS):
                try:
                    await session.start()
                    self._session = session
                    adopted = True
                    self._is_streaming = True
                    self._start_render_loop()
                    self.logger.info("Music sync active for LIFX area '%s'", self.area.name)
                    return
                except Exception as err:
                    self.logger.warning(
                        "LIFX session start attempt %d failed for '%s': %s",
                        attempt + 1,
                        self.area.name,
                        err,
                    )
                    if attempt + 1 < _SESSION_START_ATTEMPTS:
                        await asyncio.sleep(_SESSION_START_BACKOFF_S)

            self.logger.error(
                "Failed to start music sync for LIFX area '%s' after %d attempts",
                self.area.name,
                _SESSION_START_ATTEMPTS,
            )
        finally:
            self._session_starting = False
            if not adopted:
                await session.aclose()

    async def _stop_session(self) -> None:
        """Stop streaming and restore the lights' previous state."""
        self._is_streaming = False
        self._session_starting = False
        self._cancel_render_loop()
        if self._analyzer is not None:
            self._analyzer.clear_beats()
        if self._session is not None:
            with suppress(Exception):
                await self._session.aclose()
            self._session = None

    def _on_external_stream_start(self, request: ExternalStreamStartRequest) -> None:
        """Handle playback dialing this client."""
        self.logger.debug(
            "Sendspin stream start request for area '%s' (reason=%s)",
            self.area.name,
            request.connection_reason,
        )

    def _on_stream_start(self) -> None:
        """Prepare the lights and cancel a pending stop from the previous track."""
        if self._stop_debounce_task and not self._stop_debounce_task.done():
            self._stop_debounce_task.cancel()
            self._stop_debounce_task = None
        if not self._is_streaming and not self._session_starting:
            self._session_starting = True
            self.logger.info("Stream starting for area '%s', preparing lights...", self.area.name)
            self._start_task = self.mass.create_task(self._start_session())

    def _on_stream_end(self) -> None:
        """Schedule a debounced stop, so track changes don't restore the lights."""
        # Also while a start is in flight: it would otherwise adopt a session that
        # streams forever.
        starting = self._start_task is not None and not self._start_task.done()
        if self._is_streaming or starting:
            if self._stop_debounce_task and not self._stop_debounce_task.done():
                self._stop_debounce_task.cancel()
            self._stop_debounce_task = self.mass.create_task(self._debounced_stop())

    def _on_stream_clear(self) -> None:
        """Drop analyzer state queued before a seek."""
        if self._analyzer is not None:
            self._analyzer.clear_beats()

    async def _debounced_stop(self) -> None:
        """Stop after a short delay, unless a new stream started meanwhile."""
        await asyncio.sleep(_STOP_DEBOUNCE_S)
        if self._start_task is not None and not self._start_task.done():
            self._start_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._start_task
        if self._is_streaming:
            self.logger.info("Visualizer stream ended for area '%s'", self.area.name)
            await self._stop_session()

    def _on_visualizer_frame(self, frame: ExtractedFrame) -> None:
        """Queue spectrum and onset-peak features for the renderer."""
        # Frames arrive ahead of the playhead; the analyzer queues them by timestamp.
        if self._analyzer is None or not (self._is_streaming or self._session_starting):
            return
        if frame.spectrum is not None:
            self._analyzer.apply_spectrum(frame.spectrum.tolist(), frame.timestamp_us)
        if frame.peak is not None:
            self._analyzer.apply_peak(frame.peak, frame.timestamp_us)

    def _on_beats(self, beats: list[BeatTiming]) -> None:
        """Append a beat schedule segment."""
        # Not gated on streaming: the schedule arrives once per track, possibly before
        # the lights are ready.
        if self._analyzer is not None:
            self._analyzer.push_beats(beats)

    def _on_beats_clear(self) -> None:
        """Drop the beat schedule (a track change re-pushes a fresh one)."""
        if self._analyzer is not None:
            self._analyzer.clear_beat_schedule()

    def _on_color(self, payload: ServerStatePayload) -> None:
        """Forward color palette updates from the Sendspin server to the analyzer."""
        if self._analyzer is None or payload.color is None:
            return
        update: dict[str, tuple[int, int, int] | None] = {}
        for name in (
            "background_dark",
            "background_light",
            "primary",
            "accent",
            "on_dark",
            "on_light",
        ):
            value = getattr(payload.color, name)
            if isinstance(value, UndefinedField):
                continue
            update[name] = value
        if update:
            self._analyzer.apply_color_palette(update)

    # -- Render loop --

    def _start_render_loop(self) -> None:
        """Begin the fixed-rate render+send loop."""
        if self._render_handle is not None:
            return
        self._render_handle = self.mass.loop.call_later(_RENDER_PERIOD_S, self._render_tick)

    def _cancel_render_loop(self) -> None:
        """Cancel the fixed-rate render+send loop."""
        if self._render_handle is not None:
            self._render_handle.cancel()
            self._render_handle = None

    def _render_tick(self) -> None:
        """One render+send iteration, then reschedule while streaming."""
        self._render_handle = None
        if not self._is_streaming:
            return
        try:
            if (
                self._analyzer is not None
                and self._session is not None
                and self._session.is_streaming
            ):
                # Render ahead of the playhead to compensate for Wi-Fi + bulb lag.
                server_now = self.sendspin_server.clock.now_us() + self._latency_us
                colors = self._analyzer.render(server_now)
                if colors:
                    self._session.send(
                        [LightColorCommand(c.channel_id, c.red, c.green, c.blue) for c in colors]
                    )
        except Exception:
            # One bad tick must not kill the loop: log and reschedule below.
            self.logger.exception("LIFX render tick failed for area '%s'", self.area.name)
        finally:
            if self._is_streaming:
                self._render_handle = self.mass.loop.call_later(_RENDER_PERIOD_S, self._render_tick)


class LifxEntertainmentBridgeManager:
    """Manages LIFX entertainment bridges for all light areas."""

    def __init__(self, provider: LifxEntertainmentProvider) -> None:
        """Initialize the bridge manager."""
        self.provider = provider
        self.mass = provider.mass
        self.logger = LOGGER.getChild("bridge_manager")
        self._bridges: dict[str, LifxEntertainmentBridge] = {}

    @property
    def sendspin_server(self) -> SendspinServer | None:
        """Get the Sendspin server if available."""
        provider = cast("SendspinProvider | None", self.mass.get_provider("sendspin"))
        if provider is not None:
            return provider.server_api
        return None

    async def setup_bridges(self, areas: list[EntertainmentArea]) -> None:
        """Set up bridges for all light areas."""
        sendspin_server = self.sendspin_server
        if sendspin_server is None:
            self.logger.warning("Sendspin provider not available, cannot set up LIFX bridges")
            return

        current_ids = {area.id for area in areas}
        for area_id in list(self._bridges.keys()):
            if area_id not in current_ids:
                bridge = self._bridges.pop(area_id)
                with suppress(Exception):
                    await bridge.stop()

        for area in areas:
            if area.id in self._bridges:
                continue
            if not area.channels:
                continue

            bridge = LifxEntertainmentBridge(self.provider, area, sendspin_server)
            try:
                await bridge.start()
            except Exception:
                self.logger.warning("Failed to start bridge for LIFX area '%s'", area.name)
                with suppress(Exception):
                    await bridge.stop()
                continue

            self._bridges[area.id] = bridge
            self.logger.info("Bridge created for LIFX area '%s'", area.name)

    def update_settings(
        self,
        color_mode: str | None = None,
        brightness: int | None = None,
        light_latency_ms: int | None = None,
        transition_ms: int | None = None,
        power_on: bool | None = None,
    ) -> None:
        """Update settings on all bridges."""
        for bridge in self._bridges.values():
            bridge.update_settings(
                color_mode=color_mode,
                brightness=brightness,
                light_latency_ms=light_latency_ms,
                transition_ms=transition_ms,
                power_on=power_on,
            )

    async def stop_all(self) -> None:
        """Stop all bridges."""
        for bridge in list(self._bridges.values()):
            with suppress(Exception):
                await bridge.stop()
        self._bridges.clear()
