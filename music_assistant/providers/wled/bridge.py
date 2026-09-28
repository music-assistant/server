"""WLED audio-sync bridge: sends Sendspin visualizer features as WLED Audio Sync packets."""

from __future__ import annotations

import asyncio
from collections import deque
from typing import TYPE_CHECKING

from aiosendspin.models.visualizer import (
    ClientHelloVisualizerSpectrum,
    ClientHelloVisualizerSupport,
)

from music_assistant.providers.sendspin_lights.base import SendspinLightBridge

from .constants import (
    PEAK_MIN_STRENGTH,
    SEND_RATE_HZ,
    SPECTRUM_BINS,
    SPECTRUM_F_MAX,
    SPECTRUM_F_MIN,
    SPECTRUM_SCALE,
    WLED_MULTICAST_GROUP,
)
from .packet import (
    fft_magnitude_from_amplitude,
    loudness_to_sample,
    pack_audio_sync_packet,
    spectrum_to_fft_result,
)

if TYPE_CHECKING:
    from aiosendspin.server.roles.visualizer.features import ExtractedFrame

    from music_assistant.providers.sendspin.provider import SendspinProvider

    from .constants import ScalingMode
    from .provider import WledProvider


class WledBridge(SendspinLightBridge):
    """Bridge for a single WLED sync zone (one UDP port)."""

    def __init__(
        self,
        provider: WledProvider,
        sendspin_provider: SendspinProvider,
        port: int,
        gain_db: float,
        scaling_mode: ScalingMode,
        latency_ms: int,
    ) -> None:
        """
        Initialize the bridge.

        :param provider: The owning WledProvider.
        :param sendspin_provider: The loaded Sendspin provider.
        :param port: The zone's UDP port.
        :param gain_db: Gain boost applied before converting to WLED's scale.
        :param scaling_mode: Perceptual curve to apply.
        :param latency_ms: Send-ahead offset compensating for the speaker's output latency.
        """
        super().__init__(
            provider,
            sendspin_provider,
            client_id=f"wled-zone-{port}",
            name=f"WLED Sync (port {port})",
            manufacturer="WLED",
            product_name="Audio Sync Zone",
            support=ClientHelloVisualizerSupport(
                buffer_capacity=2048,
                rate_max=SEND_RATE_HZ,
                types=["loudness", "f_peak", "spectrum", "peak"],
                spectrum=ClientHelloVisualizerSpectrum(
                    n_disp_bins=SPECTRUM_BINS,
                    scale=SPECTRUM_SCALE,
                    f_min=SPECTRUM_F_MIN,
                    f_max=SPECTRUM_F_MAX,
                ),
            ),
            render_rate_hz=SEND_RATE_HZ,
        )
        self.port = port
        self._transport: asyncio.DatagramTransport | None = None
        self._latency_us = latency_ms * 1000
        self._gain_db = gain_db
        self._scaling_mode = scaling_mode
        self._pending_frames: deque[ExtractedFrame] = deque()
        self._latest_loudness = 0
        self._latest_spectrum = [0] * SPECTRUM_BINS
        self._latest_f_peak_freq = 0
        self._latest_f_peak_amp = 0
        self._peak_pending = False

    async def start(self) -> None:
        """Open the UDP transport and register with Sendspin."""
        # Multicast sends don't require joining the group, only receivers do.
        self._transport, _ = await self.mass.loop.create_datagram_endpoint(
            asyncio.DatagramProtocol, remote_addr=(WLED_MULTICAST_GROUP, self.port)
        )
        self.register_client()
        self.logger.info("WLED sync zone started on port %d", self.port)

    async def stop(self) -> None:
        """Remove the Sendspin client and close the UDP transport."""
        await self.unregister_client()
        if self._transport:
            self._transport.close()
            self._transport = None

    def update_settings(self, latency_ms: int, gain_db: float, scaling_mode: ScalingMode) -> None:
        """
        Apply changed playback settings without restarting the bridge.

        :param latency_ms: Send-ahead offset in milliseconds.
        :param gain_db: Gain boost in dB.
        :param scaling_mode: Perceptual curve to apply.
        """
        self._latency_us = latency_ms * 1000
        self._gain_db = gain_db
        self._scaling_mode = scaling_mode

    def _on_stream_start(self) -> None:
        """Reset state and begin the send loop."""
        self._reset_features()
        self._is_streaming = True
        self._start_render_loop()

    def _on_stream_clear(self) -> None:
        """Drop features that belong to pre-seek audio."""
        self._reset_features()

    def _on_stream_end(self) -> None:
        """Stop sending packets."""
        self._is_streaming = False
        self._cancel_render_loop()

    def _on_frame(self, frame: ExtractedFrame) -> None:
        """Queue an extracted feature frame until its playback time."""
        if self._is_streaming:
            self._pending_frames.append(frame)

    def _render(self) -> None:
        """Send one Audio Sync packet for the features due at the current playhead."""
        # Send slightly ahead of the playhead to offset the speaker's output latency.
        self._drain_pending(self.sendspin_server.clock.now_us() + self._latency_us)
        if self._transport is None:
            return
        sample = loudness_to_sample(self._latest_loudness, self._gain_db, self._scaling_mode)
        packet = pack_audio_sync_packet(
            sample_raw=sample,
            sample_smth=sample,
            sample_peak=self._peak_pending,
            fft_result=spectrum_to_fft_result(
                self._latest_spectrum, self._gain_db, self._scaling_mode
            ),
            fft_magnitude=fft_magnitude_from_amplitude(self._latest_f_peak_amp, self._gain_db),
            fft_major_peak=float(self._latest_f_peak_freq),
        )
        self._transport.sendto(packet)
        self._peak_pending = False

    def _reset_features(self) -> None:
        """Clear queued and cached features."""
        self._pending_frames.clear()
        self._latest_loudness = 0
        self._latest_spectrum = [0] * SPECTRUM_BINS
        self._latest_f_peak_freq = 0
        self._latest_f_peak_amp = 0
        self._peak_pending = False

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
