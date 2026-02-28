"""
Sendspin Bridge for AirPlay - allows Sendspin to stream to AirPlay devices.

This module enables AirPlay devices to be controlled via the Sendspin protocol.
Sendspin handles all synchronization and timing - AirPlay is just the output.

The bridge:
1. Registers AirPlay players as external Sendspin clients (using MAC as client_id)
2. The Sendspin provider creates a SendspinPlayer for this external client
3. Protocol linking matches the SendspinPlayer with the AirPlayPlayer via MAC
4. When grouped, Sendspin handles timing/sync, AirPlay streams audio

Audio flow:
Sendspin PushStream → BridgePlayerRole.on_audio_chunk → AirPlay CLI process
"""

from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from typing import TYPE_CHECKING, cast

from aiosendspin.models.core import ClientHelloPayload
from aiosendspin.models.core import DeviceInfo as SendspinDeviceInfo
from aiosendspin.models.player import ClientHelloPlayerSupport, SupportedAudioFormat
from aiosendspin.models.types import AudioCodec, PlayerCommand
from music_assistant_models.enums import IdentifierType

from music_assistant.helpers.util import is_valid_mac_address
from music_assistant.providers.sendspin.bridge_role import (
    BRIDGE_BIT_DEPTH,
    BRIDGE_BYTES_PER_SAMPLE,
    BRIDGE_CHANNELS,
    BRIDGE_ROLE_ID,
    BRIDGE_SAMPLE_RATE,
    BridgePlayerRole,
)
from music_assistant.providers.sendspin.helpers import bridge_client_id_from_mac

from .constants import StreamingProtocol
from .helpers import player_id_to_mac_address, unix_time_to_ntp
from .protocols.airplay2 import AirPlay2Stream
from .protocols.raop import RaopStream

if TYPE_CHECKING:
    from aiosendspin.server import ExternalStreamStartRequest, SendspinClient, SendspinServer
    from aiosendspin.server.roles import AudioChunk

    from music_assistant.providers.sendspin.provider import SendspinProvider

    from .player import AirPlayPlayer
    from .protocols._protocol import AirPlayProtocol
    from .provider import AirPlayProvider


def get_bridge_client_id(airplay_player: AirPlayPlayer) -> str | None:
    """Get the Sendspin bridge client ID for an AirPlay player.

    Uses the MAC address as the client_id to enable protocol linking.
    The Sendspin provider will create a SendspinPlayer with this client_id.

    :param airplay_player: The AirPlay player to bridge.
    :return: The MAC address for use as client_id, or None if not available.
    """
    mac = player_id_to_mac_address(airplay_player.player_id)
    if is_valid_mac_address(mac):
        return bridge_client_id_from_mac(mac)
    return None


class SendspinAirPlayBridge:
    """Manages the Sendspin to AirPlay bridge for a single player.

    This class handles:
    1. Registering the AirPlay player as an external Sendspin client
    2. Creating a BridgePlayerRole to receive audio from PushStream
    3. Streaming audio to the AirPlay device via RAOP/AirPlay2 protocol
    """

    def __init__(
        self,
        provider: AirPlayProvider,
        airplay_player: AirPlayPlayer,
        sendspin_server: SendspinServer,
    ) -> None:
        """Initialize the bridge.

        :param provider: The AirPlay provider instance.
        :param airplay_player: The AirPlay player to bridge.
        :param sendspin_server: The Sendspin server to register with.
        """
        self.provider = provider
        self.mass = provider.mass
        self.airplay_player = airplay_player
        self.sendspin_server = sendspin_server
        self.logger = provider.logger.getChild(f"bridge.{airplay_player.player_id}")

        self._sendspin_client: SendspinClient | None = None
        self._bridge_client_id: str | None = None
        self._bridge_role: BridgePlayerRole | None = None
        self._protocol: AirPlayProtocol | None = None
        self._is_streaming = False
        self._next_expected_timestamp_us: int | None = None
        self._write_queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=500)
        self._writer_task: asyncio.Task[None] | None = None
        self._protocol_start_task: asyncio.Task[None] | None = None
        self._protocol_ready = asyncio.Event()
        self._cleanup_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    @property
    def is_registered(self) -> bool:
        """Return whether the bridge is registered with Sendspin."""
        return self._sendspin_client is not None

    async def start(self) -> None:
        """Register the AirPlay player as an external Sendspin client."""
        self._bridge_client_id = get_bridge_client_id(self.airplay_player)
        if not self._bridge_client_id:
            self.logger.warning(
                "Cannot create Sendspin bridge for %s: no valid MAC address",
                self.airplay_player.display_name,
            )
            return

        hello = ClientHelloPayload(
            client_id=self._bridge_client_id,
            name=f"{self.airplay_player.display_name} (AirPlay)",
            version=1,
            supported_roles=[BRIDGE_ROLE_ID],
            device_info=SendspinDeviceInfo(
                product_name=self.airplay_player.device_info.model,
                manufacturer=self.airplay_player.device_info.manufacturer,
            ),
            player_support=ClientHelloPlayerSupport(
                supported_formats=[
                    SupportedAudioFormat(
                        codec=AudioCodec.PCM,
                        channels=BRIDGE_CHANNELS,
                        sample_rate=BRIDGE_SAMPLE_RATE,
                        bit_depth=BRIDGE_BIT_DEPTH,
                    )
                ],
                buffer_capacity=1_000,
                supported_commands=[PlayerCommand.VOLUME, PlayerCommand.MUTE],
            ),
        )

        self.logger.debug(
            "Registering Sendspin bridge for %s with client_id=%s",
            self.airplay_player.display_name,
            self._bridge_client_id,
        )

        # Pre-register the AirPlay player_id so the resulting SendspinPlayer
        # carries it as an AIRPLAY_ID identifier for cross-protocol matching.
        if sendspin_prov := cast("SendspinProvider | None", self.mass.get_provider("sendspin")):
            sendspin_prov.register_bridge_identifiers(
                self._bridge_client_id,
                {IdentifierType.AIRPLAY_ID: self.airplay_player.player_id},
            )

        self._sendspin_client = self.sendspin_server.register_external_player(
            hello, on_stream_start=self._on_stream_start
        )

        # Role is created by register_external_player via the factory registry.
        # Retrieve it and wire up the bridge callbacks.
        roles = self._sendspin_client.roles_by_family("player")
        if roles:
            self._bridge_role = cast("BridgePlayerRole", roles[0])
            self._bridge_role.set_callbacks(
                on_audio_chunk=self._on_audio_chunk,
                on_volume_change=self._on_volume_change,
                on_stream_start=self._on_bridge_stream_start,
                on_stream_end=self._on_bridge_stream_end,
                initial_volume=self.airplay_player.volume_level or 25,
            )
            self._bridge_role.setup_audio_requirements()

        self.logger.info(
            "Sendspin bridge registered for %s (client_id=%s)",
            self.airplay_player.display_name,
            self._bridge_client_id,
        )

    async def stop(self) -> None:
        """Stop and unregister the Sendspin bridge."""
        async with self._lock:
            await self._stop_streaming()
            if self._sendspin_client and self._bridge_client_id:
                await self.sendspin_server.remove_client(self._bridge_client_id)
                self._sendspin_client = None
                self._bridge_role = None

        self.logger.debug("Sendspin bridge stopped for %s", self.airplay_player.display_name)

    def _on_stream_start(self, request: ExternalStreamStartRequest) -> None:
        """Handle stream start request from Sendspin server.

        Called when Sendspin wants to play audio to this bridge player.
        aiosendspin handles role lifecycle (on_connect, push stream join).
        We clean up any previous stream state before starting a new one.
        """
        self.logger.debug(
            "Sendspin stream start request for %s (reason=%s)",
            self.airplay_player.display_name,
            request.connection_reason,
        )
        if not self.airplay_player.available:
            self.logger.warning(
                "Cannot start Sendspin stream for %s: player not available",
                self.airplay_player.display_name,
            )
            return
        # Capture and detach old stream resources before scheduling their cleanup.
        # This prevents the async cleanup from accidentally destroying the new
        # stream's resources, which reuse the same instance variables.
        old_protocol = self._protocol
        old_writer_task = self._writer_task
        old_protocol_start_task = self._protocol_start_task

        self._protocol = None
        self._writer_task = None
        self._protocol_start_task = None
        self.airplay_player.stream = None
        self._protocol_ready.clear()

        if old_protocol or old_writer_task or old_protocol_start_task:
            prev_cleanup = self._cleanup_task
            self._cleanup_task = self.mass.create_task(
                self._cleanup_old_stream(
                    old_protocol, old_writer_task, old_protocol_start_task, prev_cleanup
                )
            )

        self._is_streaming = True
        self._next_expected_timestamp_us = None

    def _on_bridge_stream_start(self) -> None:
        """Start the writer task when the PushStream notifies us the stream has started.

        Called via the BridgePlayerRole.on_stream_start callback when the
        PushStream begins delivering audio chunks.
        """
        # Cancel any existing writer task (leftover from previous stream)
        if self._writer_task is not None and not self._writer_task.done():
            self._writer_task.cancel()
        # Re-assert streaming state and clear protocol references so the first
        # audio chunk triggers a fresh protocol start. This is needed because
        # the async cleanup scheduled by _on_stream_start may have cleared
        # _is_streaming and _protocol_start_task between then and now.
        self._is_streaming = True
        self._protocol_start_task = None
        self._protocol_ready.clear()
        self._next_expected_timestamp_us = None
        # Drain stale audio data from the previous stream
        while not self._write_queue.empty():
            self._write_queue.get_nowait()
        self.airplay_player.sync_volume_level()
        self._writer_task = self.mass.create_task(self._cli_writer())
        self.logger.info(
            "Bridge writer started for %s, awaiting first chunk",
            self.airplay_player.display_name,
        )

    async def _start_protocol_from_chunk(self, chunk: AudioChunk) -> None:
        """Start the AirPlay protocol, deriving start_ntp from the first chunk's timestamp.

        :param chunk: The first audio chunk delivered by the PushStream.
        """
        try:
            # Ensure the old CLI process is fully stopped before starting a new one.
            # Without this, both old and new processes could try to connect to the
            # same AirPlay device simultaneously.
            cleanup = self._cleanup_task
            if cleanup and not cleanup.done():
                await cleanup

            future_s = (chunk.timestamp_us - time.monotonic() * 1_000_000) / 1_000_000
            start_ntp = unix_time_to_ntp(time.time() + future_s)

            if self.airplay_player.protocol == StreamingProtocol.AIRPLAY2:
                self._protocol = AirPlay2Stream(self.airplay_player)
            else:
                self._protocol = RaopStream(self.airplay_player)
            self.airplay_player.stream = self._protocol

            await self._protocol.start(start_ntp)
            self._protocol_ready.set()
            self.logger.info(
                "Bridge protocol started for %s (NTP=%s, lookahead=%.0fms)",
                self.airplay_player.display_name,
                start_ntp,
                future_s * 1000,
            )
            self.mass.create_task(self._wait_for_airplay_connection())
        except Exception as err:
            self.logger.error(
                "Failed to start AirPlay protocol for %s: %s",
                self.airplay_player.display_name,
                err,
            )
            # Clean up partially created protocol
            if self._protocol:
                with suppress(Exception):
                    await self._protocol.stop(force=True)
                self._protocol = None
                self.airplay_player.stream = None
            # Stop accepting chunks, unblock the writer, and schedule full cleanup
            self._is_streaming = False
            self._protocol_ready.set()
            self._schedule_cleanup()

    async def _wait_for_airplay_connection(self) -> None:
        """Wait for AirPlay connection in the background and log the result."""
        if not self._protocol:
            return
        try:
            await self._protocol.wait_for_connection()
            self.logger.info(
                "AirPlay connection established for %s", self.airplay_player.display_name
            )
        except Exception as err:
            self.logger.warning(
                "AirPlay connection failed for %s: %s",
                self.airplay_player.display_name,
                err,
            )

    def _on_volume_change(self, volume: int, muted: bool) -> None:
        """Forward volume/mute changes to the AirPlay CLI."""
        effective_volume = 0 if muted else volume
        self.mass.create_task(self._send_volume_command(effective_volume))

    async def _send_volume_command(self, volume: int) -> None:
        """Send VOLUME command to the AirPlay CLI."""
        if self._protocol and self._protocol.running:
            await self._protocol.send_cli_command(f"VOLUME={volume}")

    def _on_bridge_stream_end(self) -> None:
        """Stop the AirPlay protocol immediately when the stream ends.

        Rather than just sending EOF (which lets the CLI play out its buffer),
        we schedule a full cleanup that kills the CLI process immediately.
        """
        self._is_streaming = False
        self._next_expected_timestamp_us = None
        # Schedule full streaming cleanup - this kills the CLI process immediately
        # so AirPlay stops playing instead of draining its 30s buffer.
        self._schedule_cleanup()

    def _schedule_cleanup(self) -> None:
        """Schedule cleanup of the current stream resources under the bridge lock.

        Uses _stop_streaming_locked which acquires self._lock, so concurrent
        cleanups are serialized safely.
        """
        self._cleanup_task = self.mass.create_task(self._stop_streaming_locked())

    async def _stop_streaming_locked(self) -> None:
        """Serialize streaming teardown with other stop/start operations."""
        async with self._lock:
            await self._stop_streaming()

    async def _cleanup_old_stream(
        self,
        protocol: AirPlayProtocol | None,
        writer_task: asyncio.Task[None] | None,
        protocol_start_task: asyncio.Task[None] | None,
        prev_cleanup: asyncio.Task[None] | None = None,
    ) -> None:
        """Clean up captured resources from a previous stream.

        Unlike _stop_streaming(), this operates on explicitly captured references
        rather than instance variables. This prevents a race condition where the
        async cleanup runs after a new stream has already reused the instance
        variables, accidentally destroying the new stream's protocol/writer.

        :param protocol: The old AirPlay protocol to stop.
        :param writer_task: The old writer task to cancel.
        :param protocol_start_task: The old protocol start task to cancel.
        :param prev_cleanup: A prior cleanup task to await first (chaining).
        """
        # Wait for any chained prior cleanup to complete first
        if prev_cleanup and not prev_cleanup.done():
            with suppress(Exception):
                await prev_cleanup

        if protocol_start_task and not protocol_start_task.done():
            protocol_start_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await protocol_start_task
        if writer_task and not writer_task.done():
            writer_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await writer_task
        if protocol:
            with suppress(Exception):
                await protocol.stop(force=True)

    def _on_audio_chunk(self, chunk: AudioChunk) -> None:
        """Handle audio chunk from Sendspin PushStream."""
        if not self._is_streaming:
            return

        # Detect a done/failed protocol start task and stop streaming
        if self._protocol_start_task is not None and self._protocol_start_task.done():
            exc = (
                self._protocol_start_task.exception()
                if not self._protocol_start_task.cancelled()
                else None
            )
            if self._protocol_start_task.cancelled() or exc:
                self.logger.warning(
                    "Protocol start task failed for %s, stopping streaming",
                    self.airplay_player.display_name,
                )
                self._is_streaming = False
                self._schedule_cleanup()
                return

        if self._protocol_start_task is None:
            self._protocol_start_task = self.mass.create_task(
                self._start_protocol_from_chunk(chunk)
            )

        if self._next_expected_timestamp_us is not None:
            gap_us = chunk.timestamp_us - self._next_expected_timestamp_us
            if gap_us > 1_000:
                # Forward gap: fill with silence, capped at 2 seconds to avoid huge fills on seeks
                fill_us = min(gap_us, 2_000_000)
                bytes_per_us = (
                    BRIDGE_SAMPLE_RATE * BRIDGE_CHANNELS * BRIDGE_BYTES_PER_SAMPLE / 1_000_000
                )
                silence = bytes(int(fill_us * bytes_per_us))
                try:
                    self._write_queue.put_nowait(silence)
                except asyncio.QueueFull:
                    self.logger.debug("Write queue full, dropping audio chunk")
                    return
            elif gap_us < -1_000:
                self.logger.debug("Discarding late audio chunk (%d µs behind)", -gap_us)
                return

        self._next_expected_timestamp_us = chunk.timestamp_us + chunk.duration_us
        try:
            self._write_queue.put_nowait(chunk.data)
        except asyncio.QueueFull:
            self.logger.debug("Write queue full, dropping audio chunk")

    async def _cli_writer(self) -> None:
        """Write queued audio data to the CLI process stdin.

        Waits for any pending cleanup and then for the new protocol to be
        ready before writing. Runs as a single task so writes are serialised
        and ordered. A None sentinel signals end-of-stream: write EOF to
        stdin and exit.
        """
        try:
            # Wait for any pending cleanup from a previous stream to complete
            # so we don't write to a stale/dead protocol.
            cleanup_task = self._cleanup_task
            if cleanup_task and not cleanup_task.done():
                with suppress(Exception):
                    await cleanup_task
                if self._cleanup_task is cleanup_task:
                    self._cleanup_task = None
            try:
                await asyncio.wait_for(self._protocol_ready.wait(), timeout=30.0)
            except TimeoutError:
                self.logger.warning(
                    "Timed out waiting for AirPlay protocol to become ready for %s",
                    self.airplay_player.display_name,
                )
                self._is_streaming = False
                self._schedule_cleanup()
                return
            while True:
                data = await self._write_queue.get()
                if not self._protocol:
                    if data is None:
                        return
                    continue
                if data is None:
                    with suppress(Exception):
                        await self._protocol.write_audio_eof()
                    return
                with suppress(Exception):
                    await self._protocol.write_audio(data)
        finally:
            # Only clear if this writer is still the active one.
            if self._writer_task is asyncio.current_task():
                self._writer_task = None

    async def _stop_streaming(self) -> None:
        """Stop streaming (internal, called with lock held)."""
        self._is_streaming = False
        self._next_expected_timestamp_us = None
        self._protocol_ready.clear()
        if self._protocol_start_task:
            self._protocol_start_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await self._protocol_start_task
            self._protocol_start_task = None
        if self._writer_task:
            self._writer_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await self._writer_task
            self._writer_task = None
        while not self._write_queue.empty():
            self._write_queue.get_nowait()
        if self._protocol:
            await self._protocol.stop(force=True)
            self._protocol = None
            self.airplay_player.stream = None


class SendspinBridgeManager:
    """Manages Sendspin bridges for all AirPlay players."""

    def __init__(self, provider: AirPlayProvider) -> None:
        """Initialize the bridge manager.

        :param provider: The AirPlay provider instance.
        """
        self.provider = provider
        self.mass = provider.mass
        self.logger = provider.logger.getChild("bridge_manager")
        self._bridges: dict[str, SendspinAirPlayBridge] = {}
        self._lock = asyncio.Lock()

    @property
    def sendspin_provider(self) -> SendspinProvider | None:
        """Get the Sendspin provider if available."""
        return cast(
            "SendspinProvider | None",
            self.mass.get_provider("sendspin"),
        )

    @property
    def sendspin_server(self) -> SendspinServer | None:
        """Get the Sendspin server if available."""
        if provider := self.sendspin_provider:
            return provider.server_api
        return None

    async def setup_bridge(self, airplay_player: AirPlayPlayer) -> None:
        """Set up a Sendspin bridge for an AirPlay player."""
        async with self._lock:
            player_id = airplay_player.player_id

            sendspin_server = self.sendspin_server
            if not sendspin_server:
                self.logger.debug(
                    "Sendspin provider not available, skipping bridge for %s",
                    airplay_player.display_name,
                )
                return

            if player_id in self._bridges:
                self.logger.debug("Bridge already exists for %s", airplay_player.display_name)
                return

            bridge = SendspinAirPlayBridge(self.provider, airplay_player, sendspin_server)

            try:
                await bridge.start()
            except Exception:
                self.logger.warning(
                    "Failed to start Sendspin bridge for %s", airplay_player.display_name
                )
                with suppress(Exception):
                    await bridge.stop()
                return

            if not bridge.is_registered:
                return

            self._bridges[player_id] = bridge

            self.logger.info("Sendspin bridge created for %s", airplay_player.display_name)

    async def remove_bridge(self, airplay_player_id: str) -> None:
        """Remove the Sendspin bridge for an AirPlay player."""
        async with self._lock:
            if bridge := self._bridges.pop(airplay_player_id, None):
                await bridge.stop()

            self.logger.debug("Sendspin bridge removed for AirPlay player %s", airplay_player_id)

    async def stop_all(self) -> None:
        """Stop all Sendspin bridges."""
        async with self._lock:
            for bridge in list(self._bridges.values()):
                with suppress(Exception):
                    await bridge.stop()
            self._bridges.clear()

        self.logger.debug("All Sendspin bridges stopped")

    def get_bridge(self, airplay_player_id: str) -> SendspinAirPlayBridge | None:
        """Get the bridge for an AirPlay player."""
        return self._bridges.get(airplay_player_id)
