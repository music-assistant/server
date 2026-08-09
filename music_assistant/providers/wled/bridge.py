"""
WLED audio-sync bridge — in-process Sendspin visualizer client.

Each configured zone (a UDP port) registers as an external Sendspin client
whose visualizer role runs the server's feature extraction on the group's
audio. Extracted loudness/dominant-frequency/spectrum/onset features are
packed into WLED's native "Audio Sync" UDP packet and sent to the fixed
multicast group WLED itself listens on, using this zone's configured port.
"""

from __future__ import annotations

import asyncio
from collections import deque
from contextlib import suppress
from typing import TYPE_CHECKING, cast

from aiosendspin.models.core import ClientHelloPayload
from aiosendspin.models.core import DeviceInfo as SendspinDeviceInfo
from aiosendspin.models.visualizer import (
    ClientHelloVisualizerSpectrum,
    ClientHelloVisualizerSupport,
)
from music_assistant_models.enums import PlayerType

from music_assistant.mass import LOGGER
from music_assistant.providers.sendspin.bridge_role import (
    VISUALIZER_BRIDGE_ROLE_ID,
    BridgeVisualizerRole,
)

from .constants import (
    DEFAULT_GAIN_DB,
    DEFAULT_LATENCY_MS,
    PEAK_MIN_STRENGTH,
    SEND_RATE_HZ,
    SPECTRUM_BINS,
    SPECTRUM_F_MAX,
    SPECTRUM_F_MIN,
    SPECTRUM_SCALE,
    WLED_MULTICAST_GROUP,
)
from .packet import loudness_to_sample, pack_audio_sync_packet, spectrum_to_fft_result

if TYPE_CHECKING:
    from aiosendspin.server import ExternalStreamStartRequest, SendspinClient, SendspinServer
    from aiosendspin.server.roles.visualizer.features import ExtractedFrame

    from music_assistant.providers.sendspin.provider import SendspinProvider

    from .provider import WledProvider

_SEND_PERIOD_S = 1.0 / SEND_RATE_HZ


class WledBridge:
    """
    Manages the WLED audio-sync bridge for a single zone (UDP port).

    Registers with the local Sendspin server as an external visualizer
    client, receives extracted loudness/frequency/spectrum/onset features
    keyed to playback time, and sends WLED's native UDP audio-sync packet
    to the fixed WLED multicast group on this zone's port.
    """

    def __init__(
        self,
        provider: WledProvider,
        port: int,
        sendspin_server: SendspinServer,
        gain_db: float = DEFAULT_GAIN_DB,
    ) -> None:
        """
        Initialize the bridge.

        :param provider: The owning WledProvider instance.
        :param port: The zone's UDP port; WLED devices join by setting the
            same port as their own audioSyncPort.
        :param sendspin_server: The local Sendspin server to register with.
        :param gain_db: Gain boost applied to loudness/spectrum/peak values
            before converting to WLED's scale (see apply_gain_db).
        """
        self.provider = provider
        self.mass = provider.mass
        self.port = port
        self.sendspin_server = sendspin_server
        self.logger = LOGGER.getChild(f"wled.bridge.{port}")

        self._sendspin_client: SendspinClient | None = None
        self._transport: asyncio.DatagramTransport | None = None
        self._is_streaming = False
        self._render_handle: asyncio.TimerHandle | None = None
        self._latency_us: int = DEFAULT_LATENCY_MS * 1000
        self._gain_db: float = gain_db

        # Bounded as a backstop against unbounded growth if draining ever stalls
        # (e.g. an exception in a render tick) -- a few seconds at the visualizer
        # frame rate is far more headroom than the queued-by-timestamp draining
        # in _drain_pending should ever need.
        self._pending_frames: deque[ExtractedFrame] = deque(maxlen=SEND_RATE_HZ * 5)
        self._latest_loudness: int = 0
        self._latest_spectrum: list[int] = [0] * SPECTRUM_BINS
        self._latest_f_peak_freq: int = 0
        self._latest_f_peak_amp: int = 0
        self._peak_pending: bool = False

    async def start(self) -> None:
        """Start the bridge — register as an in-process Sendspin visualizer client."""
        client_id = f"wled-zone-{self.port}"

        # Register this client as a LIGHT player type with the Sendspin provider
        # so the resulting virtual player shows up correctly in the UI.
        sendspin_prov: SendspinProvider | None = self.mass.get_provider("sendspin")  # type: ignore[assignment]
        if sendspin_prov:
            sendspin_prov.register_bridge_player_type(client_id, PlayerType.LIGHT)

        support = ClientHelloVisualizerSupport(
            buffer_capacity=2048,
            rate_max=SEND_RATE_HZ,
            types=["loudness", "f_peak", "spectrum", "peak"],
            spectrum=ClientHelloVisualizerSpectrum(
                n_disp_bins=SPECTRUM_BINS,
                scale=SPECTRUM_SCALE,
                f_min=SPECTRUM_F_MIN,
                f_max=SPECTRUM_F_MAX,
            ),
        )
        hello = ClientHelloPayload(
            client_id=client_id,
            name=f"WLED Sync (port {self.port})",
            version=1,
            supported_roles=[VISUALIZER_BRIDGE_ROLE_ID],
            device_info=SendspinDeviceInfo(manufacturer="WLED", product_name="Audio Sync Zone"),
            visualizer_support=support,
        )
        self._sendspin_client = self.sendspin_server.register_external_player(
            hello, on_stream_start=self._on_external_stream_start
        )

        if viz_roles := self._sendspin_client.roles_by_family("visualizer"):
            viz_role = cast("BridgeVisualizerRole", viz_roles[0])
            viz_role.set_callbacks(
                on_frame=self._on_frame,
                on_beats=lambda _beats: None,
                on_beats_clear=lambda: None,
                on_stream_start=self._on_stream_start,
                on_stream_clear=self._on_stream_clear,
                on_stream_end=self._on_stream_end,
            )
            viz_role.setup_visualizer(support)
        # Subscribes the role to the group's visualizer group role, through which
        # extracted frames flow.
        self._sendspin_client.attach_preinitialized_roles()

        # Multicast sends don't require joining the group -- only receivers do.
        self._transport, _ = await self.mass.loop.create_datagram_endpoint(
            asyncio.DatagramProtocol, remote_addr=(WLED_MULTICAST_GROUP, self.port)
        )

        self.logger.info("WLED sync zone started on port %d", self.port)

    async def stop(self) -> None:
        """Stop the bridge."""
        self._cancel_render_loop()
        self._is_streaming = False
        if self._sendspin_client:
            await self.sendspin_server.remove_client(self._sendspin_client.client_id)
            self._sendspin_client = None
        if self._transport:
            self._transport.close()
            self._transport = None
        self.logger.debug("WLED sync zone stopped on port %d", self.port)

    def update_settings(self, latency_ms: int | None = None, gain_db: float | None = None) -> None:
        """Update bridge settings without restarting the bridge."""
        if latency_ms is not None:
            self._latency_us = latency_ms * 1000
        if gain_db is not None:
            self._gain_db = gain_db

    def _on_external_stream_start(self, request: ExternalStreamStartRequest) -> None:
        """Handle playback dialing this client. Nothing to do beyond logging."""
        self.logger.debug("Sendspin stream start request for zone port %d", self.port)

    def _on_stream_start(self) -> None:
        """Handle stream start — reset state and begin the send loop."""
        self._pending_frames.clear()
        self._peak_pending = False
        self._is_streaming = True
        self._start_render_loop()

    def _on_stream_clear(self) -> None:
        """Handle seek — queued features belong to pre-seek audio, drop them."""
        self._pending_frames.clear()

    def _on_stream_end(self) -> None:
        """Handle stream end — stop sending packets."""
        self._is_streaming = False
        self._cancel_render_loop()

    def _on_frame(self, frame: ExtractedFrame) -> None:
        """Queue an extracted feature frame for the sender."""
        if not self._is_streaming:
            return
        self._pending_frames.append(frame)

    # -- Send loop --

    def _start_render_loop(self) -> None:
        """Begin the fixed-rate send loop."""
        if self._render_handle is not None:
            return
        self._render_handle = self.mass.loop.call_later(_SEND_PERIOD_S, self._render_tick)

    def _cancel_render_loop(self) -> None:
        """Cancel the fixed-rate send loop."""
        if self._render_handle is not None:
            self._render_handle.cancel()
            self._render_handle = None

    def _render_tick(self) -> None:
        """One drain+send iteration, then reschedule while streaming."""
        self._render_handle = None
        if not self._is_streaming:
            return
        try:
            # Send slightly ahead of the playhead to compensate for the grouped
            # speaker's own output latency (typically the dominant delay source
            # over a LAN, far more than the ~1ms UDP send itself).
            now_us = self.sendspin_server.clock.now_us() + self._latency_us
            # Drain unconditionally, even without a transport: on_frame keeps
            # appending frames every tick while streaming, and the deque's
            # maxlen alone isn't a substitute for actually consuming them.
            self._drain_pending(now_us)
            if self._transport is not None:
                sample = loudness_to_sample(self._latest_loudness, self._gain_db)
                packet = pack_audio_sync_packet(
                    sample_raw=sample,
                    sample_smth=sample,
                    sample_peak=self._peak_pending,
                    fft_result=spectrum_to_fft_result(self._latest_spectrum, self._gain_db),
                    fft_magnitude=loudness_to_sample(self._latest_f_peak_amp, self._gain_db),
                    fft_major_peak=float(self._latest_f_peak_freq),
                )
                self._transport.sendto(packet)
            self._peak_pending = False
        except Exception:
            # One bad tick must not kill the loop: log and reschedule below.
            self.logger.exception("WLED send tick failed for zone port %d", self.port)
        finally:
            if self._is_streaming:
                self._render_handle = self.mass.loop.call_later(_SEND_PERIOD_S, self._render_tick)

    def _drain_pending(self, now_us: int) -> None:
        """Promote queued frames up to ``now_us`` into the latest sendable state."""
        while self._pending_frames and self._pending_frames[0].timestamp_us <= now_us:
            frame = self._pending_frames.popleft()
            if frame.loudness is not None:
                self._latest_loudness = frame.loudness
            if frame.spectrum is not None:
                self._latest_spectrum = frame.spectrum.tolist()
            if frame.f_peak_freq is not None and frame.f_peak_amp is not None:
                self._latest_f_peak_freq = frame.f_peak_freq
                self._latest_f_peak_amp = frame.f_peak_amp
            if frame.peak is not None and frame.peak >= PEAK_MIN_STRENGTH:
                self._peak_pending = True


class WledBridgeManager:
    """Manages the single WLED bridge for a provider instance."""

    def __init__(self, provider: WledProvider) -> None:
        """Initialize the bridge manager."""
        self.provider = provider
        self.mass = provider.mass
        self.logger = LOGGER.getChild("wled.bridge_manager")
        self._bridge: WledBridge | None = None

    @property
    def sendspin_server(self) -> SendspinServer | None:
        """Get the Sendspin server if available."""
        sendspin_prov = cast("SendspinProvider | None", self.mass.get_provider("sendspin"))
        return sendspin_prov.server_api if sendspin_prov else None

    async def start(self, port: int, gain_db: float = DEFAULT_GAIN_DB) -> None:
        """Start the bridge for this instance's configured port."""
        server = self.sendspin_server
        if server is None:
            self.logger.warning("Sendspin provider not available, WLED sync inactive")
            return
        self._bridge = WledBridge(self.provider, port, server, gain_db=gain_db)
        await self._bridge.start()

    async def stop(self) -> None:
        """Stop the bridge."""
        if self._bridge is not None:
            with suppress(Exception):
                await self._bridge.stop()
            self._bridge = None

    def update_settings(self, latency_ms: int | None = None, gain_db: float | None = None) -> None:
        """Update settings on the active bridge without restarting it."""
        if self._bridge is not None:
            self._bridge.update_settings(latency_ms=latency_ms, gain_db=gain_db)
