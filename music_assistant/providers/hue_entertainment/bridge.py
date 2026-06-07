"""
Hue Entertainment bridge — connects as a Sendspin visualizer client.

Instead of being a server-side role in the PushStream (which delivers
audio 30 seconds ahead of playback), we connect as a Sendspin WebSocket
client with the visualizer role. The server computes visualization data
(FFT, loudness, spectrum) and delivers it at the right playback time
through the connection layer's built-in scheduling.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import suppress
from typing import TYPE_CHECKING

from aiosendspin.client import SendspinClient
from aiosendspin.models import Roles
from aiosendspin.models.core import DeviceInfo as SendspinDeviceInfo
from aiosendspin.models.core import ServerStatePayload
from aiosendspin.models.types import UndefinedField
from aiosendspin.models.visualizer import (
    BeatTiming,
    ClientHelloVisualizerSpectrum,
    ClientHelloVisualizerSupport,
    VisualizerFrame,
)
from music_assistant_models.enums import PlayerType

from music_assistant.providers.hue_entertainment.hue_sendspin_bridge import (
    HueAudioAnalyzer,
    HueDtlsStreamer,
)
from music_assistant.providers.hue_entertainment.hue_sendspin_bridge.constants import (
    SPECTRUM_BINS,
    SPECTRUM_F_MAX,
    SPECTRUM_F_MIN,
    SPECTRUM_SCALE,
)

from .constants import (
    CONF_BRIGHTNESS,
    CONF_COLOR_MODE,
    CONF_HUE_LATENCY_MS,
    DEFAULT_HUE_LATENCY_MS,
)

if TYPE_CHECKING:
    from music_assistant.providers.hue_entertainment.hue_sendspin_bridge import (
        EntertainmentArea,
    )
    from music_assistant.providers.sendspin.provider import SendspinProvider

    from .provider import HueEntertainmentProvider

LOGGER = logging.getLogger(__name__)

SENDSPIN_PORT = 8927

# Hue Entertainment streams comfortably accept ~30 Hz updates over DTLS.
_RENDER_RATE_HZ = 30
_RENDER_PERIOD_S = 1.0 / _RENDER_RATE_HZ
# Visualizer frame rate requested from the Sendspin server. Bridge filters
# (channel rise/decay, bass baseline) are tuned for ~20 Hz spectrum input; the
# DTLS render loop runs faster and interpolates.
_VISUALIZER_RATE_HZ = 20


class HueEntertainmentBridge:
    """
    Manages the Hue Entertainment bridge for a single entertainment area.

    Connects to the local Sendspin server as a visualizer client, receives
    pre-computed visualization data at the correct playback time, converts
    it to light colors, and streams to the Hue bridge over DTLS.
    """

    def __init__(
        self,
        provider: HueEntertainmentProvider,
        area: EntertainmentArea,
    ) -> None:
        """Initialize the bridge."""
        self.provider = provider
        self.mass = provider.mass
        self.area = area
        self.logger = LOGGER.getChild(f"bridge.{area.name}")

        self._dtls_streamer = HueDtlsStreamer()
        self._analyzer: HueAudioAnalyzer | None = None
        self._sendspin_client: SendspinClient | None = None
        self._client_task: asyncio.Task[None] | None = None
        self._is_streaming = False
        self._unsubscribe_viz: Callable[[], None] | None = None
        self._unsubscribe_color: Callable[[], None] | None = None
        self._stop_debounce_task: asyncio.Task[None] | None = None
        self._render_handle: asyncio.TimerHandle | None = None
        self._entertainment_starting: bool = False
        self._hue_latency_us: int = (
            int(
                float(
                    str(
                        self.provider.config.get_value(CONF_HUE_LATENCY_MS)
                        or DEFAULT_HUE_LATENCY_MS
                    )
                )
            )
            * 1000
        )

    async def start(self) -> None:
        """Start the bridge — connect as a Sendspin visualizer client."""
        self._analyzer = HueAudioAnalyzer(
            channels=self.area.channels,
            color_mode=str(self.provider.config.get_value(CONF_COLOR_MODE) or "smooth"),
            brightness=int(float(str(self.provider.config.get_value(CONF_BRIGHTNESS) or 100))),
        )

        # Create Sendspin client with visualizer role
        client_id = f"hue-{self.area.id.replace('-', '')[:16]}"

        # Register this client as a LIGHT player type with the Sendspin provider
        # so the resulting player shows up correctly in the UI
        sendspin_prov: SendspinProvider | None = self.mass.get_provider("sendspin")  # type: ignore[assignment]
        if sendspin_prov:
            sendspin_prov.register_bridge_player_type(client_id, PlayerType.LIGHT)

        self._sendspin_client = SendspinClient(
            client_id=client_id,
            client_name=f"Hue: {self.area.name}",
            roles=[Roles.VISUALIZER, Roles.COLOR],
            device_info=SendspinDeviceInfo(
                manufacturer="Signify",
                product_name="Hue Entertainment Area",
            ),
            visualizer_support=ClientHelloVisualizerSupport(
                # Beat + small bundle of periodic features. Each periodic frame
                # is ~20-30 bytes; one second's worth fits comfortably under
                # the buffer cap below.
                buffer_capacity=2048,
                rate_max=_VISUALIZER_RATE_HZ,
                # Peaks requested as a fallback for when beats aren't computed yet.
                types=["beat", "peak", "spectrum"],
                spectrum=ClientHelloVisualizerSpectrum(
                    n_disp_bins=SPECTRUM_BINS,
                    scale=SPECTRUM_SCALE,
                    f_min=SPECTRUM_F_MIN,
                    f_max=SPECTRUM_F_MAX,
                ),
            ),
        )

        self._unsubscribe_viz = self._sendspin_client.add_visualizer_listener(
            self._on_visualizer_frames
        )
        self._unsubscribe_color = self._sendspin_client.add_color_listener(self._on_color)
        self._sendspin_client.add_stream_start_listener(self._on_stream_start)
        self._sendspin_client.add_stream_end_listener(self._on_stream_end)

        # Connect to local Sendspin server
        self._client_task = self.mass.create_task(self._run_client())

        self.logger.info(
            "Hue bridge started for area '%s' (%d channels)",
            self.area.name,
            len(self.area.channels),
        )
        self.logger.debug(
            "Hue bridge channels for area '%s': %s",
            self.area.name,
            [
                f"id={c.channel_id} svc={c.service_id} name={c.name} pos={c.position}"
                for c in self.area.channels
            ],
        )

    async def stop(self) -> None:
        """Stop the bridge."""
        self._cancel_render_loop()
        if self._unsubscribe_viz:
            self._unsubscribe_viz()
            self._unsubscribe_viz = None
        if self._unsubscribe_color:
            self._unsubscribe_color()
            self._unsubscribe_color = None

        if self._client_task and not self._client_task.done():
            self._client_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._client_task
            self._client_task = None

        if self._sendspin_client and self._sendspin_client.connected:
            await self._sendspin_client.disconnect()
        self._sendspin_client = None

        await self._stop_entertainment()
        self.logger.debug("Hue bridge stopped for area '%s'", self.area.name)

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

    async def _run_client(self) -> None:
        """Connect to the Sendspin server and stay connected."""
        try:
            assert self._sendspin_client is not None
            bind_ip = self.mass.streams.bind_ip
            ws_url = f"ws://{bind_ip}:{SENDSPIN_PORT}/sendspin"
            await self._sendspin_client.connect(ws_url)
            self.logger.info("Connected to Sendspin server as visualizer client")

            # Keep alive until stopped — entertainment mode starts on first viz frame
            while self._sendspin_client and self._sendspin_client.connected:
                await asyncio.sleep(1.0)

        except asyncio.CancelledError:
            pass
        except Exception as err:
            self.logger.error("Sendspin client error: %s", err)
        finally:
            await self._stop_entertainment()

    async def _start_entertainment(self) -> None:
        """Start entertainment mode and DTLS connection with retry."""
        hue_api = self.provider.hue_api
        if hue_api is None:
            self._entertainment_starting = False
            return

        # Stop any active entertainment area first — bridge only allows one
        with suppress(Exception):
            areas = await hue_api.get_entertainment_areas()
            for area in areas:
                with suppress(Exception):
                    await hue_api.stop_entertainment(area.id)

        username = str(self.provider.config.get_value("hue_username") or "")
        clientkey = str(self.provider.config.get_value("hue_clientkey") or "")
        loop = asyncio.get_running_loop()

        try:
            for attempt in range(3):
                try:
                    await hue_api.start_entertainment(self.area.id)
                    await loop.run_in_executor(
                        None,
                        self._dtls_streamer.connect,
                        hue_api.host,
                        username,
                        clientkey,
                        self.area.id,
                    )
                    self._is_streaming = True
                    self._start_render_loop()
                    self.logger.info("Entertainment streaming active for area '%s'", self.area.name)
                    return
                except Exception as err:
                    self.logger.warning(
                        "Entertainment start attempt %d failed for '%s': %s",
                        attempt + 1,
                        self.area.name,
                        err,
                    )
                    with suppress(Exception):
                        self._dtls_streamer.disconnect()
                    if attempt < 2:
                        await asyncio.sleep(0.5)

            self.logger.error(
                "Failed to start entertainment for '%s' after 3 attempts", self.area.name
            )
        finally:
            self._entertainment_starting = False

    async def _stop_entertainment(self) -> None:
        """Stop DTLS and entertainment mode."""
        self._is_streaming = False
        self._entertainment_starting = False
        self._cancel_render_loop()
        if self._analyzer is not None:
            self._analyzer.clear_beats()
        with suppress(Exception):
            self._dtls_streamer.disconnect()
        hue_api = self.provider.hue_api
        if hue_api:
            with suppress(Exception):
                await hue_api.stop_entertainment(self.area.id)

    def _on_stream_start(self, message: object) -> None:
        """Handle stream start — start entertainment mode + DTLS proactively."""
        # Cancel any pending stop from a previous stream end (track transition)
        if self._stop_debounce_task and not self._stop_debounce_task.done():
            self._stop_debounce_task.cancel()
            self._stop_debounce_task = None
        if not self._is_streaming and not self._entertainment_starting:
            self._entertainment_starting = True
            self.logger.info("Stream starting for area '%s', connecting DTLS...", self.area.name)
            self.mass.create_task(self._start_entertainment())

    def _on_stream_end(self, roles: list[str] | None) -> None:
        """Handle stream end — debounce to survive track transitions."""
        if roles and "visualizer" in roles and self._is_streaming:
            # Cancel any pending stop
            if self._stop_debounce_task and not self._stop_debounce_task.done():
                self._stop_debounce_task.cancel()
            self._stop_debounce_task = self.mass.create_task(self._debounced_stop())

    async def _debounced_stop(self) -> None:
        """Wait briefly before stopping — a new stream may start (track change)."""
        await asyncio.sleep(2.0)
        if self._is_streaming:
            self.logger.info("Visualizer stream ended for area '%s'", self.area.name)
            await self._stop_entertainment()

    def _on_visualizer_frames(self, frames: list[VisualizerFrame]) -> None:
        """
        Forward periodic samples, onset peaks, and beat events to the analyzer.

        Frames carrying ``is_downbeat`` are beats. The rest carry spectrum and peak data.
        """
        if not self._is_streaming or self._analyzer is None:
            return
        beats: list[BeatTiming] = []
        for frame in frames:
            if frame.is_downbeat is not None:
                beats.append(
                    BeatTiming(timestamp_us=frame.timestamp_us, is_downbeat=frame.is_downbeat)
                )
                continue
            if frame.spectrum is not None:
                self._analyzer.apply_spectrum(frame.spectrum, frame.timestamp_us)
            if frame.peak_strength is not None:
                self._analyzer.apply_peak(frame.peak_strength, frame.timestamp_us)
        if beats:
            self._analyzer.push_beats(beats)

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
            # Skip this tick when not ready (client/DTLS down) but keep the loop
            # alive so it recovers once the connection is back.
            if (
                self._analyzer is not None
                and self._sendspin_client is not None
                and self._dtls_streamer.is_connected
            ):
                client_now = int(self.mass.loop.time() * 1_000_000)
                # Render slightly ahead of the playhead to compensate for Hue+DTLS lag.
                server_now = self._sendspin_client.compute_server_time(
                    client_now + self._hue_latency_us
                )
                commands = self._analyzer.render(server_now)
                if commands:
                    self._dtls_streamer.send_colors(commands)
        except Exception:
            # One bad tick must not kill the loop: log and reschedule below.
            self.logger.exception("Hue render tick failed for area '%s'", self.area.name)
        finally:
            if self._is_streaming:
                self._render_handle = self.mass.loop.call_later(_RENDER_PERIOD_S, self._render_tick)


class HueEntertainmentBridgeManager:
    """Manages Hue Entertainment bridges for all entertainment areas."""

    def __init__(self, provider: HueEntertainmentProvider) -> None:
        """Initialize the bridge manager."""
        self.provider = provider
        self.mass = provider.mass
        self.logger = LOGGER.getChild("bridge_manager")
        self._bridges: dict[str, HueEntertainmentBridge] = {}

    async def setup_bridges(self, areas: list[EntertainmentArea]) -> None:
        """Set up bridges for all entertainment areas."""
        # Remove bridges for areas that no longer exist
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

            bridge = HueEntertainmentBridge(self.provider, area)
            try:
                await bridge.start()
            except Exception:
                self.logger.warning("Failed to start bridge for area '%s'", area.name)
                with suppress(Exception):
                    await bridge.stop()
                continue

            self._bridges[area.id] = bridge
            self.logger.info("Bridge created for Hue area '%s'", area.name)

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
