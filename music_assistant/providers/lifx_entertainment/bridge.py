"""LIFX entertainment bridge: one in-process Sendspin visualizer client per light area."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import TYPE_CHECKING

from aiosendspin.models.types import UndefinedField
from aiosendspin.models.visualizer import ClientHelloVisualizerSpectrum, VisualizerStatePayload
from lifx_entertainment import EntertainmentSession, LightColorCommand, area_client_id

from music_assistant.providers.sendspin_lights.analyzer import AudioAnalyzer
from music_assistant.providers.sendspin_lights.base import SendspinLightBridge

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
    from aiosendspin.server.roles.visualizer.features import ExtractedFrame
    from lifx_entertainment import EntertainmentArea

    from music_assistant.providers.sendspin.provider import SendspinProvider

    from .provider import LifxEntertainmentProvider

LOGGER = logging.getLogger(__name__)

# LIFX recommends at most 20 messages per second per device.
_RENDER_RATE_HZ = 20
# The analyzer's filters (channel rise/decay, bass baseline) are tuned for ~20 Hz input.
_VISUALIZER_RATE_HZ = 20

_SESSION_START_ATTEMPTS = 3
_SESSION_START_BACKOFF_S = 1.0
# Wait this long after a stream ends before restoring the lights: a new stream
# usually starts right away on a track change.
_STOP_DEBOUNCE_S = 2.0

_PALETTE_FIELDS = (
    "background_dark",
    "background_light",
    "primary",
    "accent",
    "on_dark",
    "on_light",
)


class LifxEntertainmentBridge(SendspinLightBridge):
    """Music sync for a single LIFX light area."""

    def __init__(
        self,
        provider: LifxEntertainmentProvider,
        sendspin_provider: SendspinProvider,
        area: EntertainmentArea,
    ) -> None:
        """Initialize the bridge."""
        super().__init__(
            provider,
            sendspin_provider,
            client_id=f"lifx-{area_client_id(area.id, provider.instance_id)}",
            name=f"LIFX: {area.name}",
            manufacturer="LIFX",
            product_name="LIFX Light Group",
            visualizer_request=VisualizerStatePayload(
                rate_max=_VISUALIZER_RATE_HZ,
                # Peaks drive the lights until the beat schedule has been computed.
                types=["beat", "peak", "spectrum"],
                spectrum=ClientHelloVisualizerSpectrum(
                    n_disp_bins=SPECTRUM_BINS,
                    scale=SPECTRUM_SCALE,
                    f_min=SPECTRUM_F_MIN,
                    f_max=SPECTRUM_F_MAX,
                ),
            ),
            buffer_capacity=2048,
            render_rate_hz=_RENDER_RATE_HZ,
            with_color_role=True,
        )
        self.lifx_provider = provider
        self.area = area
        self._analyzer = AudioAnalyzer(
            channels=area.channels,
            color_mode=get_color_mode(provider.config),
            brightness=get_brightness(provider.config),
        )
        self._session: EntertainmentSession | None = None
        self._session_starting = False
        # Serializes session start and stop, so a new session only snapshots the lights
        # once the previous one has fully restored them.
        self._session_lock = asyncio.Lock()
        self._start_task: asyncio.Task[None] | None = None
        self._stop_debounce_task: asyncio.Task[None] | None = None
        self._latency_us = get_light_latency_ms(provider.config) * 1000
        self._transition_ms = get_transition_ms(provider.config)
        self._power_on = get_power_on(provider.config)

    async def start(self) -> None:
        """Register as an in-process Sendspin visualizer client."""
        self.register_client()
        self.logger.info(
            "LIFX bridge started for area '%s' (%d lights, %d channels)",
            self.area.name,
            len(self.area.devices),
            len(self.area.channels),
        )

    async def stop(self) -> None:
        """Remove the Sendspin client and restore the lights."""
        await self._cancel_task(self._stop_debounce_task)
        self._stop_debounce_task = None
        await self.unregister_client()
        # Cancel an in-flight start so it can't adopt a session after we stop.
        await self._cancel_task(self._start_task)
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
        self._analyzer.update_settings(color_mode=color_mode, brightness=brightness)
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
        try:
            async with self._session_lock:
                await self._start_session_locked()
        finally:
            self._session_starting = False

    async def _start_session_locked(self) -> None:
        """Start a session; the caller holds the session lock."""
        api = self.lifx_provider.lifx_api
        if api is None:
            return
        session = EntertainmentSession(
            api, self.area, transition_ms=self._transition_ms, power_on=self._power_on
        )
        # Until adopted, the finally closes the session so a failed or cancelled start
        # never leaves the lights half switched on.
        adopted = False
        try:
            for attempt in range(_SESSION_START_ATTEMPTS):
                try:
                    await session.start()
                except OSError as err:
                    self.logger.warning(
                        "LIFX session start attempt %d failed for '%s': %s",
                        attempt + 1,
                        self.area.name,
                        err,
                    )
                    if attempt + 1 < _SESSION_START_ATTEMPTS:
                        await asyncio.sleep(_SESSION_START_BACKOFF_S)
                    continue
                self._session = session
                adopted = True
                self._is_streaming = True
                self._start_render_loop()
                self.logger.info("Music sync active for LIFX area '%s'", self.area.name)
                return
            self.logger.error(
                "Failed to start music sync for LIFX area '%s' after %d attempts",
                self.area.name,
                _SESSION_START_ATTEMPTS,
            )
        finally:
            if not adopted:
                await session.aclose()

    async def _stop_session(self) -> None:
        """Stop streaming and restore the lights' previous state."""
        self._is_streaming = False
        self._cancel_render_loop()
        self._analyzer.clear_beats()
        async with self._session_lock:
            session, self._session = self._session, None
            if session is not None:
                with suppress(Exception):
                    await session.aclose()

    def _on_stream_start(self) -> None:
        """Prepare the lights and cancel a pending stop from the previous track."""
        if self._stop_debounce_task and not self._stop_debounce_task.done():
            # Only interrupts the delay: a restoration already running is shielded.
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
        self._analyzer.clear_beats()

    async def _debounced_stop(self) -> None:
        """Stop after a short delay, unless a new stream started meanwhile."""
        await asyncio.sleep(_STOP_DEBOUNCE_S)
        await self._cancel_task(self._start_task)
        if self._is_streaming:
            self.logger.info("Visualizer stream ended for area '%s'", self.area.name)
            # Shielded: a stream starting now must not interrupt the restoration halfway.
            await asyncio.shield(self._stop_session())

    def _on_frame(self, frame: ExtractedFrame) -> None:
        """Queue spectrum and onset-peak features for the renderer."""
        # Frames arrive ahead of the playhead; the analyzer queues them by timestamp.
        if not (self._is_streaming or self._session_starting):
            return
        if frame.spectrum is not None:
            self._analyzer.apply_spectrum(frame.spectrum.tolist(), frame.timestamp_us)
        if frame.peak is not None:
            self._analyzer.apply_peak(frame.peak, frame.timestamp_us)

    def _on_beats(self, beats: list[BeatTiming]) -> None:
        """Append a beat schedule segment."""
        # Not gated on streaming: the schedule arrives once per track, possibly before
        # the lights are ready.
        self._analyzer.push_beats(beats)

    def _on_beats_clear(self) -> None:
        """Drop the beat schedule (a track change re-pushes a fresh one)."""
        self._analyzer.clear_beat_schedule()

    def _on_color(self, payload: ServerStatePayload) -> None:
        """Forward color palette updates from the Sendspin server to the analyzer."""
        if isinstance(payload.color, UndefinedField):
            return
        update: dict[str, tuple[int, int, int] | None] = {}
        for name in _PALETTE_FIELDS:
            value = getattr(payload.color, name)
            if not isinstance(value, UndefinedField):
                update[name] = value
        if update:
            self._analyzer.apply_color_palette(update)

    def _render(self) -> None:
        """Render the colors due at the playhead and send them to the lights."""
        session = self._session
        if session is None or not session.is_streaming:
            return
        # Render ahead of the playhead to compensate for Wi-Fi + bulb lag.
        colors = self._analyzer.render(self.sendspin_server.clock.now_us() + self._latency_us)
        if colors:
            session.send([LightColorCommand(c.channel_id, c.red, c.green, c.blue) for c in colors])

    @staticmethod
    async def _cancel_task(task: asyncio.Task[None] | None) -> None:
        """Cancel a task and wait for it to finish."""
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


class LifxEntertainmentBridgeManager:
    """Manages LIFX entertainment bridges for all light areas."""

    def __init__(self, provider: LifxEntertainmentProvider) -> None:
        """Initialize the bridge manager."""
        self.provider = provider
        self.mass = provider.mass
        self.logger = LOGGER.getChild("bridge_manager")
        self._bridges: dict[str, LifxEntertainmentBridge] = {}

    async def setup_bridges(
        self, sendspin_provider: SendspinProvider, areas: list[EntertainmentArea]
    ) -> None:
        """Set up bridges for all light areas."""
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

            bridge = LifxEntertainmentBridge(self.provider, sendspin_provider, area)
            try:
                await bridge.start()
            except Exception as err:  # noqa: BLE001 - one area that fails must not stop the others
                self.logger.warning("Failed to start bridge for LIFX area '%s': %s", area.name, err)
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
