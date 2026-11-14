"""
Hue Entertainment bridge — in-process Sendspin visualizer client.

Each light target registers as an external Sendspin client whose
bridge roles run the server's visualizer feature extraction on the group's
audio and receive color palette updates directly — no WebSocket involved.
The analyzer queues features by playback timestamp, converts them to light
colors at render time, and streams them to the target's lights.
"""

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
from music_assistant_models.enums import PlayerType

from music_assistant.providers.sendspin.bridge_role import (
    COLOR_BRIDGE_ROLE_ID,
    VISUALIZER_BRIDGE_ROLE_ID,
    BridgeColorRole,
    BridgeVisualizerRole,
)

from .analyzer import HueAudioAnalyzer
from .constants import (
    SPECTRUM_BINS,
    SPECTRUM_F_MAX,
    SPECTRUM_F_MIN,
    SPECTRUM_SCALE,
)
from .settings import get_brightness, get_color_mode, get_hue_latency_ms
from .targets import TargetUnavailableError

if TYPE_CHECKING:
    from aiosendspin.models.core import ServerStatePayload
    from aiosendspin.models.visualizer import BeatTiming
    from aiosendspin.server import (
        ExternalStreamStartRequest,
        SendspinClient,
        SendspinServer,
    )
    from aiosendspin.server.roles.visualizer.features import ExtractedFrame

    from music_assistant.providers.sendspin.provider import SendspinProvider

    from .provider import HueEntertainmentProvider
    from .targets import LightTarget

LOGGER = logging.getLogger(__name__)

# Hue Entertainment streams comfortably accept ~30 Hz updates over DTLS.
_RENDER_RATE_HZ = 30
_RENDER_PERIOD_S = 1.0 / _RENDER_RATE_HZ
# Visualizer feature rate requested from the extraction pipeline. Bridge
# filters (channel rise/decay, bass baseline) are tuned for ~20 Hz spectrum
# input; the DTLS render loop runs faster and interpolates.
_VISUALIZER_RATE_HZ = 20

# Session start retries when the bridge is slow to complete the DTLS handshake.
_ENTERTAINMENT_START_ATTEMPTS = 6
_ENTERTAINMENT_START_BACKOFF_S = 1.5


class HueEntertainmentBridge:
    """
    Manages the Hue Entertainment bridge for a single light target.

    Registers with the local Sendspin server as an external visualizer
    client, receives extracted visualization features keyed to playback
    time, converts them to light colors, and streams them to the target.
    """

    def __init__(
        self,
        provider: HueEntertainmentProvider,
        target: LightTarget,
        sendspin_server: SendspinServer,
    ) -> None:
        """Initialize the bridge."""
        self.provider = provider
        self.mass = provider.mass
        self.target = target
        self.sendspin_server = sendspin_server
        self.logger = LOGGER.getChild(f"bridge.{target.name}")

        self._analyzer: HueAudioAnalyzer | None = None
        self._sendspin_client: SendspinClient | None = None
        self._is_streaming = False
        self._stop_debounce_task: asyncio.Task[None] | None = None
        self._start_task: asyncio.Task[None] | None = None
        self._render_handle: asyncio.TimerHandle | None = None
        self._entertainment_starting: bool = False
        self._hue_latency_us: int = get_hue_latency_ms(self.provider.config) * 1000

    async def start(self) -> None:
        """Start the bridge — register as an in-process Sendspin visualizer client."""
        self._analyzer = HueAudioAnalyzer(
            channels=self.target.channels,
            color_mode=get_color_mode(self.provider.config),
            brightness=get_brightness(self.provider.config),
        )

        client_id = self.target.client_id

        # Register this client as a LIGHT player type with the Sendspin provider
        # so the resulting player shows up correctly in the UI
        sendspin_prov: SendspinProvider | None = self.mass.get_provider("sendspin")  # type: ignore[assignment]
        if sendspin_prov:
            sendspin_prov.register_bridge_player_type(client_id, PlayerType.LIGHT)

        # Beat + small bundle of periodic features. Each periodic frame
        # is ~20-30 bytes; one second's worth fits comfortably under
        # the buffer cap below.
        support = ClientHelloVisualizerSupport(buffer_capacity=2048)
        request = VisualizerStatePayload(
            rate_max=_VISUALIZER_RATE_HZ,
            # Peaks requested as a fallback for when beats aren't computed yet.
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
            name=f"Hue: {self.target.name}",
            version=1,
            supported_roles=[VISUALIZER_BRIDGE_ROLE_ID, COLOR_BRIDGE_ROLE_ID],
            device_info=SendspinDeviceInfo(
                manufacturer="Signify",
                product_name=self.target.product_name,
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
        # Subscribes the roles to the group's visualizer/color group roles,
        # which beat schedules and palette updates flow through.
        self._sendspin_client.attach_preinitialized_roles()

        self.logger.info(
            "Hue bridge started for '%s' (%d channels)",
            self.target.name,
            len(self.target.channels),
        )
        self.logger.debug(
            "Hue bridge channels for '%s': %s",
            self.target.name,
            [
                f"id={c.channel_id} svc={c.service_id} name={c.name} pos={c.position}"
                for c in self.target.channels
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

        await self._stop_entertainment()
        self.logger.debug("Hue bridge stopped for '%s'", self.target.name)

    def update_settings(
        self,
        color_mode: str | None = None,
        brightness: int | None = None,
        hue_latency_ms: int | None = None,
    ) -> None:
        """Update analyzer/bridge settings without restarting the bridge."""
        if self._analyzer:
            self._analyzer.update_settings(
                color_mode=color_mode,
                brightness=brightness,
            )
        if hue_latency_ms is not None:
            self._hue_latency_us = hue_latency_ms * 1000

    async def _start_entertainment(self) -> None:
        """Open the target's stream, with retry."""
        # The target is only treated as streaming once its start succeeded; until
        # then it is closed in the finally so a failed - or cancelled - start never
        # leaks the stream or leaves the lights in entertainment mode.
        adopted = False
        try:
            for attempt in range(_ENTERTAINMENT_START_ATTEMPTS):
                try:
                    await self.target.start()
                    adopted = True
                    self._is_streaming = True
                    self._start_render_loop()
                    self.logger.info("Entertainment streaming active for '%s'", self.target.name)
                    return
                except TargetUnavailableError as err:
                    self.logger.error(
                        "Cannot start entertainment for '%s': %s", self.target.name, err
                    )
                    return
                except Exception as err:
                    self.logger.warning(
                        "Entertainment start attempt %d failed for '%s': %s",
                        attempt + 1,
                        self.target.name,
                        err,
                    )
                    if attempt + 1 < _ENTERTAINMENT_START_ATTEMPTS:
                        await asyncio.sleep(_ENTERTAINMENT_START_BACKOFF_S)

            self.logger.error(
                "Failed to start entertainment for '%s' after %d attempts",
                self.target.name,
                _ENTERTAINMENT_START_ATTEMPTS,
            )
        finally:
            self._entertainment_starting = False
            if not adopted:
                await self.target.aclose()

    async def _stop_entertainment(self) -> None:
        """Stop the target's stream and restore its lights."""
        self._is_streaming = False
        self._entertainment_starting = False
        self._cancel_render_loop()
        if self._analyzer is not None:
            self._analyzer.clear_beats()
        await self.target.aclose()

    def _on_external_stream_start(self, request: ExternalStreamStartRequest) -> None:
        """Handle playback dialing this client."""
        self.logger.debug(
            "Sendspin stream start request for '%s' (reason=%s)",
            self.target.name,
            request.connection_reason,
        )

    def _on_stream_start(self) -> None:
        """Handle stream start — start entertainment mode + DTLS proactively."""
        # Cancel any pending stop from a previous stream end (track transition)
        if self._stop_debounce_task and not self._stop_debounce_task.done():
            self._stop_debounce_task.cancel()
            self._stop_debounce_task = None
        if not self._is_streaming and not self._entertainment_starting:
            self._entertainment_starting = True
            self.logger.info("Stream starting for '%s', connecting...", self.target.name)
            self._start_task = self.mass.create_task(self._start_entertainment())

    def _on_stream_end(self) -> None:
        """Handle stream end — debounce to survive track transitions."""
        # Also act while a start is still in flight: the stream can end before
        # the target's start() completes, and that late start would otherwise leave
        # a stream that runs forever.
        starting = self._start_task is not None and not self._start_task.done()
        if self._is_streaming or starting:
            if self._stop_debounce_task and not self._stop_debounce_task.done():
                self._stop_debounce_task.cancel()
            self._stop_debounce_task = self.mass.create_task(self._debounced_stop())

    def _on_stream_clear(self) -> None:
        """Handle seek — queued analyzer state belongs to pre-seek audio, drop it."""
        if self._analyzer is not None:
            self._analyzer.clear_beats()

    async def _debounced_stop(self) -> None:
        """Wait briefly before stopping — a new stream may start (track change)."""
        await asyncio.sleep(2.0)
        # Cancel a still-running start first (its finally closes the not-yet-adopted
        # target); if the start already completed, tear the live stream down.
        if self._start_task is not None and not self._start_task.done():
            self._start_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._start_task
        if self._is_streaming:
            self.logger.info("Visualizer stream ended for '%s'", self.target.name)
            await self._stop_entertainment()

    def _on_visualizer_frame(self, frame: ExtractedFrame) -> None:
        """Queue spectrum and onset-peak features for the renderer."""
        # Frames arrive ahead of the playhead (in-process delivery follows the audio push);
        # the analyzer queues them by timestamp and drains at render time. Gated so the
        # queues only fill while the lights are up or coming up: a lost stream or a parked
        # Zigbee target skips the render that drains them.
        if self._analyzer is None or not (self.target.is_streaming or self._entertainment_starting):
            return
        if frame.spectrum is not None:
            self._analyzer.apply_spectrum(frame.spectrum.tolist(), frame.timestamp_us)
        if frame.peak is not None:
            self._analyzer.apply_peak(frame.peak, frame.timestamp_us)

    def _on_beats(self, beats: list[BeatTiming]) -> None:
        """Append a beat schedule segment."""
        # Not gated on streaming: the schedule lands once per track and may arrive while
        # DTLS is still connecting; the renderer prunes past beats and track changes
        # replace the schedule.
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
            # Skip this tick when not ready (stream down) but keep the loop
            # alive so it recovers once the connection is back.
            if self._analyzer is not None and self.target.is_streaming:
                # Render slightly ahead of the playhead to compensate for the light lag.
                server_now = self.sendspin_server.clock.now_us() + self._hue_latency_us
                commands = self._analyzer.render(server_now)
                if commands:
                    self.target.send(commands)
        except Exception:
            # One bad tick must not kill the loop: log and reschedule below.
            self.logger.exception("Hue render tick failed for '%s'", self.target.name)
        finally:
            if self._is_streaming:
                self._render_handle = self.mass.loop.call_later(_RENDER_PERIOD_S, self._render_tick)


class HueEntertainmentBridgeManager:
    """Manages Hue Entertainment bridges for all light targets."""

    def __init__(self, provider: HueEntertainmentProvider) -> None:
        """Initialize the bridge manager."""
        self.provider = provider
        self.mass = provider.mass
        self.logger = LOGGER.getChild("bridge_manager")
        self._bridges: dict[str, HueEntertainmentBridge] = {}

    @property
    def sendspin_server(self) -> SendspinServer | None:
        """Get the Sendspin server if available."""
        provider = cast("SendspinProvider | None", self.mass.get_provider("sendspin"))
        if provider is not None:
            return provider.server_api
        return None

    async def setup_bridges(self, targets: list[LightTarget]) -> None:
        """Set up bridges for all light targets."""
        sendspin_server = self.sendspin_server
        if sendspin_server is None:
            self.logger.warning("Sendspin provider not available, cannot set up Hue bridges")
            return

        # Remove bridges for targets that no longer exist
        current_ids = {target.client_id for target in targets}
        for client_id in list(self._bridges.keys()):
            if client_id not in current_ids:
                bridge = self._bridges.pop(client_id)
                with suppress(Exception):
                    await bridge.stop()

        for target in targets:
            if target.client_id in self._bridges:
                continue
            if not target.channels:
                continue

            bridge = HueEntertainmentBridge(self.provider, target, sendspin_server)
            try:
                await bridge.start()
            except Exception:
                self.logger.warning("Failed to start bridge for '%s'", target.name)
                with suppress(Exception):
                    await bridge.stop()
                continue

            self._bridges[target.client_id] = bridge
            self.logger.info("Bridge created for '%s'", target.name)

    def update_settings(
        self,
        color_mode: str | None = None,
        brightness: int | None = None,
        hue_latency_ms: int | None = None,
    ) -> None:
        """Update settings on all bridges."""
        for bridge in self._bridges.values():
            bridge.update_settings(
                color_mode=color_mode,
                brightness=brightness,
                hue_latency_ms=hue_latency_ms,
            )

    async def stop_all(self) -> None:
        """Stop all bridges."""
        for bridge in list(self._bridges.values()):
            with suppress(Exception):
                await bridge.stop()
        self._bridges.clear()
