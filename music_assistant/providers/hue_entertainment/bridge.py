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
from aiosendspin.models.visualizer import (
    ClientHelloVisualizerSpectrum,
    ClientHelloVisualizerSupport,
    VisualizerFrame,
)

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

    from .provider import HueEntertainmentProvider

LOGGER = logging.getLogger(__name__)

SENDSPIN_WS_URL = "ws://127.0.0.1:8927/sendspin"


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
        self._sendspin_client = SendspinClient(
            client_id=client_id,
            client_name=f"Hue: {self.area.name}",
            roles=[Roles.VISUALIZER],
            visualizer_support=ClientHelloVisualizerSupport(
                buffer_capacity=4096,
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
            await self._sendspin_client.connect(SENDSPIN_WS_URL)
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
        """Start entertainment mode and DTLS connection."""
        hue_api = self.provider.hue_api
        if hue_api is None:
            return

        await hue_api.start_entertainment(self.area.id)

        username = str(self.provider.config.get_value("hue_username") or "")
        clientkey = str(self.provider.config.get_value("hue_clientkey") or "")
        loop = asyncio.get_running_loop()
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

    def _on_stream_end(self, roles: list[str] | None) -> None:
        """Handle stream end — stop entertainment mode."""
        if roles and "visualizer" in roles and self._is_streaming:
            self.logger.info("Visualizer stream ended for area '%s'", self.area.name)
            self.mass.create_task(self._stop_entertainment())

    _entertainment_starting: bool = False

    def _on_visualizer_frames(self, frames: list[VisualizerFrame]) -> None:
        """Handle visualization frames from the Sendspin server.

        These frames arrive at the correct playback time — the connection
        layer handles all scheduling. We just convert to colors and send.
        Entertainment mode + DTLS start on the first frame.
        """
        if not self._analyzer:
            return

        # Start entertainment mode + DTLS on first frame (once)
        if not self._is_streaming and not self._entertainment_starting:
            self._entertainment_starting = True
            self.mass.create_task(self._start_entertainment())
            return

        if not self._is_streaming or not self._dtls_streamer.is_connected:
            return

        for frame in frames:
            commands = self._analyzer.process_frame(frame)
            self._dtls_streamer.send_colors(commands)


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
