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
from aiosendspin.models.visualizer import (
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
    SPECTRUM_RATE_MAX,
)

from .constants import (
    CONF_BRIGHTNESS,
    CONF_COLOR_MODE,
    CONF_INTENSITY,
)

if TYPE_CHECKING:
    from music_assistant.providers.hue_entertainment.hue_sendspin_bridge import (
        EntertainmentArea,
    )
    from music_assistant.providers.sendspin.provider import SendspinProvider

    from .provider import HueEntertainmentProvider

LOGGER = logging.getLogger(__name__)

SENDSPIN_PORT = 8927


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
        self._stop_debounce_task: asyncio.Task[None] | None = None
        self._entertainment_starting: bool = False

    async def start(self) -> None:
        """Start the bridge — connect as a Sendspin visualizer client."""
        self._analyzer = HueAudioAnalyzer(
            channels=self.area.channels,
            color_mode=str(self.provider.config.get_value(CONF_COLOR_MODE) or "spectrum"),
            brightness=int(float(str(self.provider.config.get_value(CONF_BRIGHTNESS) or 100))),
            intensity=int(float(str(self.provider.config.get_value(CONF_INTENSITY) or 70))),
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
            roles=[Roles.VISUALIZER],
            device_info=SendspinDeviceInfo(
                manufacturer="Signify",
                product_name="Hue Entertainment Area",
            ),
            visualizer_support=ClientHelloVisualizerSupport(
                # Small buffer — we want frames at playback time, not seconds ahead.
                # Each viz frame is ~50 bytes, so 100 bytes ≈ 2 frames ≈ 200ms lead.
                buffer_capacity=100,
                types=["loudness", "f_peak", "spectrum"],
                batch_max=1,
                spectrum=ClientHelloVisualizerSpectrum(
                    n_disp_bins=SPECTRUM_BINS,
                    scale="mel",
                    f_min=SPECTRUM_F_MIN,
                    f_max=SPECTRUM_F_MAX,
                    rate_max=SPECTRUM_RATE_MAX,
                ),
            ),
        )

        # Register callbacks
        self._unsubscribe_viz = self._sendspin_client.add_visualizer_listener(
            self._on_visualizer_frames
        )
        self._sendspin_client.add_stream_start_listener(self._on_stream_start)
        self._sendspin_client.add_stream_end_listener(self._on_stream_end)

        # Connect to local Sendspin server
        self._client_task = self.mass.create_task(self._run_client())

        self.logger.info(
            "Hue bridge started for area '%s' (%d channels)",
            self.area.name,
            len(self.area.channels),
        )

    async def stop(self) -> None:
        """Stop the bridge."""
        if self._unsubscribe_viz:
            self._unsubscribe_viz()
            self._unsubscribe_viz = None

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
        intensity: int | None = None,
    ) -> None:
        """Update analyzer settings without restarting the bridge."""
        if self._analyzer:
            self._analyzer.update_settings(
                color_mode=color_mode,
                brightness=brightness,
                intensity=intensity,
            )

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

    # Latency correction in microseconds. Negative = send later (lights are early).
    _HUE_LATENCY_US = -20_000

    def _on_visualizer_frames(self, frames: list[VisualizerFrame]) -> None:
        """Handle visualization frames from the Sendspin server.

        Frames arrive ~100-200ms before their playback time. We schedule
        each send at (play_time - hue_latency) so lights change at the
        exact moment the audio is heard.
        """
        if not self._is_streaming or not self._analyzer or not self._dtls_streamer.is_connected:
            return

        assert self._sendspin_client is not None
        loop = self.mass.loop

        for frame in frames:
            commands = self._analyzer.process_frame(frame)

            # Schedule send at the correct playback moment
            play_us = self._sendspin_client.compute_play_time(frame.timestamp_us)
            send_at_us = play_us - self._HUE_LATENCY_US
            now_us = int(loop.time() * 1_000_000)
            delay_us = send_at_us - now_us

            if delay_us < -50_000:
                # More than 50ms late — skip
                continue
            if delay_us <= 0:
                # Due now — send immediately
                self._dtls_streamer.send_colors(commands)
            else:
                # Schedule for the precise moment
                loop.call_later(
                    delay_us / 1_000_000,
                    self._dtls_streamer.send_colors,
                    commands,
                )


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
        intensity: int | None = None,
    ) -> None:
        """Update settings on all bridges."""
        for bridge in self._bridges.values():
            bridge.update_settings(
                color_mode=color_mode,
                brightness=brightness,
                intensity=intensity,
            )

    async def stop_all(self) -> None:
        """Stop all bridges."""
        for bridge in list(self._bridges.values()):
            with suppress(Exception):
                await bridge.stop()
        self._bridges.clear()
