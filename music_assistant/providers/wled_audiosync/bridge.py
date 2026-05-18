"""
WLED audio-sync bridge — per-device Sendspin VISUALIZER client + UDP fan-out.

Each bridge represents one WLED destination (a discovered MM-capable device,
a manually-added unicast endpoint, or a broadcast / multicast group). The
bridge:

1. Connects to MA's local Sendspin server as a `Roles.VISUALIZER` client
   and registers itself as a `PlayerType.VISUALIZER` bridge player so it
   appears in the MA UI as a sync-group-only "lights" device (mirrors
   `hue_entertainment.bridge.HueEntertainmentBridge`).
2. Consumes pre-computed `VisualizerFrame`s from the Sendspin server
   (loudness / f_peak / 16-bin log spectrum), passes them through the
   `WledAudioAnalyzer` to get a WLED V2 frame, encodes that to a 44-byte
   packet, and schedules the UDP send via `loop.call_later(...)` at the
   precise audible-now moment computed by `compute_play_time(timestamp_us)`.
3. Handles transport failures (rate-limited warnings, auto-reset after
   sustained errors, single `/json/info` probe to confirm device offline).

The bridge intentionally does not register a normal MA `Player`. The
Sendspin provider owns the player surface via `register_bridge_player_type`.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import TYPE_CHECKING

from aiosendspin.client import SendspinClient
from aiosendspin.models import Roles
from aiosendspin.models.core import DeviceInfo as SendspinDeviceInfo
from aiosendspin.models.visualizer import (
    ClientHelloVisualizerSpectrum,
    ClientHelloVisualizerSupport,
)
from music_assistant_models.enums import EventType, PlayerType

from music_assistant.constants import CONF_ICON

from .wled_audiosync_bridge import (
    DEFAULT_F_MAX,
    DEFAULT_F_MIN,
    DestinationKind,
    WledAudioAnalyzer,
    WledV2Transport,
    encode_v2,
)
from .wled_audiosync_bridge.analyzer import (
    DEFAULT_VISUALIZER_RATE_HZ,
    WLED_FFT_BANDS,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from aiosendspin.models.visualizer import VisualizerFrame
    from music_assistant_models.event import MassEvent

    from music_assistant.providers.sendspin.provider import SendspinProvider

    from .provider import WledAudioSyncProvider

_LOGGER = logging.getLogger(__name__)

# Port the local in-process Sendspin server listens on.
_SENDSPIN_PORT = 8927
# Visualizer-frame buffer capacity on the Sendspin client. Frames arrive
# ~100-200 ms before their audible play time at this size — enough lead to
# schedule them precisely without piling up seconds of pre-computed data.
_VISUALIZER_BUFFER_CAPACITY = 100
# Per-bridge latency offset for sends (microseconds). Negative means "send
# slightly later than play-time" if WLED is consistently early; positive
# means "send earlier" if WLED is consistently late.
_DEFAULT_LATENCY_US = 0
# If a frame's scheduled send time has already passed by more than this many
# microseconds, skip the emit entirely (the visual would be visibly late).
_LATE_DROP_BUDGET_US = 50_000
# Wait this long after a "stream end" before tearing down — gives track
# transitions a chance to start a new stream without flicker.
_STREAM_END_DEBOUNCE_S = 2.0
# Default icon shown for bridge players in the MA UI. Applied once, only
# when the user hasn't already customised the icon, so manual changes
# from the UI survive every subsequent provider load.
_DEFAULT_PLAYER_ICON = "mdi-led-strip-variant"


class WledAudioSyncBridge:
    """One Sendspin VISUALIZER client driving one WLED destination."""

    def __init__(
        self,
        provider: WledAudioSyncProvider,
        *,
        client_id: str,
        name: str,
        address: str,
        port: int,
        duplicate_transmit: bool,
        multicast_ttl: int,
        latency_us: int = _DEFAULT_LATENCY_US,
    ) -> None:
        """
        Build a bridge for one WLED destination.

        :param provider: The owning ``WledAudioSyncProvider`` instance.
        :param client_id: Stable Sendspin client id for this WLED (used by
            Sendspin's bridge-player-type override).
        :param name: Human-readable Sendspin client name.
        :param address: IPv4 unicast, broadcast, or multicast destination.
        :param port: UDP destination port.
        :param duplicate_transmit: Send each V2 packet twice back-to-back.
        :param multicast_ttl: IP_MULTICAST_TTL for multicast destinations only.
        :param latency_us: Microsecond offset applied to ``compute_play_time``.
        """
        self.provider = provider
        self.mass = provider.mass
        self.client_id = client_id
        self.name = name
        self.logger = _LOGGER.getChild(client_id)
        self._latency_us = latency_us
        self._transport = WledV2Transport(
            address=address,
            port=port,
            duplicate_transmit=duplicate_transmit,
            multicast_ttl=multicast_ttl,
            on_reset=self._on_transport_reset,
        )
        self._analyzer: WledAudioAnalyzer | None = None
        self._sendspin_client: SendspinClient | None = None
        self._client_task: asyncio.Task[None] | None = None
        self._unsubscribe_viz: Callable[[], None] | None = None
        self._unsubscribe_player_added: Callable[[], None] | None = None
        self._stop_debounce_task: asyncio.Task[None] | None = None
        self._is_streaming = False

    @property
    def destination_address(self) -> str:
        """Return the UDP destination address packets are sent to."""
        return self._transport.address

    @property
    def destination_port(self) -> int:
        """Return the UDP destination port."""
        return self._transport.port

    @property
    def kind(self) -> DestinationKind:
        """Return whether the destination is unicast / broadcast / multicast."""
        return self._transport.kind

    async def start(self) -> None:
        """Open the Sendspin client and subscribe to visualizer frames."""
        # Tell Sendspin to expose this client as a "lights / visualizer"
        # bridge player rather than as a regular speaker.
        sendspin_prov: SendspinProvider | None = self.mass.get_provider("sendspin")  # type: ignore[assignment]
        if sendspin_prov is not None:
            sendspin_prov.register_bridge_player_type(self.client_id, PlayerType.VISUALIZER)
        else:
            self.logger.warning(
                "Sendspin provider not loaded — WLED bridge %s will be silent until it is",
                self.client_id,
            )

        self._analyzer = WledAudioAnalyzer(
            agc_release_frames=DEFAULT_VISUALIZER_RATE_HZ,
        )

        self._sendspin_client = SendspinClient(
            client_id=self.client_id,
            client_name=self.name,
            roles=[Roles.VISUALIZER],
            device_info=SendspinDeviceInfo(
                manufacturer="WLED",
                product_name="WLED Audio Sync receiver",
            ),
            visualizer_support=ClientHelloVisualizerSupport(
                buffer_capacity=_VISUALIZER_BUFFER_CAPACITY,
                types=["loudness", "f_peak", "spectrum"],
                batch_max=1,
                spectrum=ClientHelloVisualizerSpectrum(
                    n_disp_bins=WLED_FFT_BANDS,
                    scale="log",
                    f_min=int(DEFAULT_F_MIN),
                    f_max=int(DEFAULT_F_MAX),
                    rate_max=DEFAULT_VISUALIZER_RATE_HZ,
                ),
            ),
        )
        self._unsubscribe_viz = self._sendspin_client.add_visualizer_listener(
            self._on_visualizer_frames
        )
        self._sendspin_client.add_stream_start_listener(self._on_stream_start)
        self._sendspin_client.add_stream_end_listener(self._on_stream_end)

        self._client_task = self.mass.create_task(self._run_client())

        # The Sendspin handshake registers the player asynchronously after
        # connect, so the player may not exist yet — subscribe so we get a
        # second chance once it shows up, and also try eagerly in case the
        # provider has been reloaded and the player is already there.
        self._unsubscribe_player_added = self.mass.subscribe(
            self._on_player_added_event,
            (EventType.PLAYER_ADDED,),
            id_filter=self.client_id,
        )
        self._apply_default_icon()

        self.logger.info(
            "WLED bridge started: %s -> %s:%d (%s%s)",
            self.client_id,
            self.destination_address,
            self.destination_port,
            self.kind.value,
            ", duplicate-tx" if self._transport.duplicate_transmit else "",
        )

    def _on_player_added_event(self, _event: MassEvent) -> None:
        """Apply the WLED default icon once the bridge's player is registered."""
        self._apply_default_icon()

    def _apply_default_icon(self) -> None:
        """Set the bridge's player icon to ``mdi-led-strip-variant`` if unset."""
        # Best-effort cosmetic — failure here must never block bridge startup,
        # so swallow everything broader than the expected timing miss.
        try:
            if self.mass.players.get_player(self.client_id) is None:
                return
            existing = self.mass.config.get_raw_player_config_value(self.client_id, CONF_ICON)
            if existing is not None:
                return
            self.mass.config.set_raw_player_config_value(
                self.client_id, CONF_ICON, _DEFAULT_PLAYER_ICON
            )
        except Exception:
            self.logger.debug(
                "Could not apply default WLED icon for %s (player not ready yet)",
                self.client_id,
                exc_info=True,
            )

    async def stop(self) -> None:
        """Disconnect from Sendspin and close the UDP transport."""
        if self._unsubscribe_viz is not None:
            self._unsubscribe_viz()
            self._unsubscribe_viz = None

        if self._unsubscribe_player_added is not None:
            self._unsubscribe_player_added()
            self._unsubscribe_player_added = None

        if self._stop_debounce_task and not self._stop_debounce_task.done():
            self._stop_debounce_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._stop_debounce_task
            self._stop_debounce_task = None

        if self._client_task and not self._client_task.done():
            self._client_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._client_task
            self._client_task = None

        if self._sendspin_client is not None and self._sendspin_client.connected:
            await self._sendspin_client.disconnect()
        self._sendspin_client = None

        await self._transport.close()
        self._is_streaming = False
        self._analyzer = None
        self.logger.debug("WLED bridge stopped: %s", self.client_id)

    def set_destination(self, address: str, port: int) -> None:
        """
        Update the UDP destination (e.g. when mDNS re-discovers the device).

        Closes the old socket so the next emit opens a fresh one bound to the
        new endpoint. Safe to call any time; no-op if address+port unchanged.
        """
        address_changed = address != self._transport.address
        port_changed = port != self._transport.port
        if not (address_changed or port_changed):
            return
        self.logger.debug(
            "Destination changed for %s: %s:%d -> %s:%d",
            self.client_id,
            self._transport.address,
            self._transport.port,
            address,
            port,
        )
        old_transport = self._transport
        self._transport = WledV2Transport(
            address=address,
            port=port,
            duplicate_transmit=old_transport.duplicate_transmit,
            multicast_ttl=old_transport._multicast_ttl,
            on_reset=self._on_transport_reset,
        )
        self.mass.create_task(old_transport.close())

    async def _run_client(self) -> None:
        """Keep the Sendspin WebSocket connection alive until stopped."""
        try:
            assert self._sendspin_client is not None
            ws_url = f"ws://{self.mass.streams.bind_ip}:{_SENDSPIN_PORT}/sendspin"
            await self._sendspin_client.connect(ws_url)
            self.logger.info("Connected to Sendspin server as visualizer client")
            while self._sendspin_client is not None and self._sendspin_client.connected:
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.exception("Sendspin client error for %s", self.client_id)

    def _on_stream_start(self, _message: object) -> None:
        """Handle a stream/start — mark active and cancel any pending stop."""
        if self._stop_debounce_task and not self._stop_debounce_task.done():
            self._stop_debounce_task.cancel()
            self._stop_debounce_task = None
        if not self._is_streaming:
            self.logger.info("Visualizer stream starting on %s", self.client_id)
        self._is_streaming = True

    def _on_stream_end(self, roles: list[str] | None) -> None:
        """Handle a stream/end — debounce so we survive track transitions."""
        if not self._is_streaming:
            return
        if roles is not None and "visualizer" not in roles:
            return
        if self._stop_debounce_task and not self._stop_debounce_task.done():
            self._stop_debounce_task.cancel()
        self._stop_debounce_task = self.mass.create_task(self._debounced_stop())

    async def _debounced_stop(self) -> None:
        """Wait briefly before flagging the stream stopped (track-transition cushion)."""
        try:
            await asyncio.sleep(_STREAM_END_DEBOUNCE_S)
        except asyncio.CancelledError:
            return
        if self._is_streaming:
            self.logger.info("Visualizer stream ended on %s", self.client_id)
            self._is_streaming = False

    def _on_visualizer_frames(self, frames: list[VisualizerFrame]) -> None:
        """
        Schedule a UDP send for each visualizer frame at its audible-now moment.

        Sendspin delivers frames ~100-200 ms before play time, with each frame's
        original server-side timestamp. ``compute_play_time`` converts that to a
        local monotonic deadline we can hand to ``loop.call_later``.
        """
        if not self._is_streaming or self._analyzer is None:
            return
        assert self._sendspin_client is not None
        loop = self.mass.loop

        for frame in frames:
            wled_frame = self._analyzer.process_frame(frame)
            if wled_frame is None:
                continue
            packet = encode_v2(wled_frame)

            play_us = self._sendspin_client.compute_play_time(frame.timestamp_us)
            send_at_us = play_us - self._latency_us
            now_us = int(loop.time() * 1_000_000)
            delay_us = send_at_us - now_us

            if delay_us < -_LATE_DROP_BUDGET_US:
                # Frame is so late it would be visibly behind the audio. Drop.
                continue
            if delay_us <= 0:
                self.mass.create_task(self._transport.send(packet))
                continue
            loop.call_later(
                delay_us / 1_000_000,
                self._spawn_send,
                packet,
            )

    def _spawn_send(self, packet: bytes) -> None:
        """Spawn the async UDP send from the scheduled (sync) callback."""
        self.mass.create_task(self._transport.send(packet))

    async def _on_transport_reset(self) -> None:
        """Probe /json/info after a transport auto-reset and log the outcome."""
        # Imported lazily to avoid a circular import on package load.
        from .provider import probe_audioreactive  # noqa: PLC0415

        address = self.destination_address
        if not address or self.kind is not DestinationKind.UNICAST:
            self.logger.info(
                "%s transport reset (kind=%s — skipping /json/info probe)",
                self.client_id,
                self.kind.value,
            )
            return
        self.logger.info(
            "%s transport reset; probing /json/info to confirm reachability",
            self.client_id,
        )
        try:
            reachable = await probe_audioreactive(self.mass.http_session, address)
        except Exception:
            self.logger.exception(
                "%s: error while probing /json/info after transport reset",
                self.client_id,
            )
            return
        if reachable:
            self.logger.info(
                "%s responded to /json/info — sends will resume on the next frame",
                self.client_id,
            )
        else:
            self.logger.warning(
                "%s did not respond to /json/info after a transport reset; "
                "device may be offline or AudioReactive is no longer loaded",
                self.client_id,
            )
