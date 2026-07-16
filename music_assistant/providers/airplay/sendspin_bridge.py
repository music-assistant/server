"""
Sendspin Bridge for AirPlay - allows Sendspin to stream to AirPlay devices.

This module enables AirPlay devices to be controlled via the Sendspin protocol.
Sendspin handles all synchronization and timing - AirPlay is just the output.

The bridge:
1. Registers AirPlay players as external Sendspin clients (using MAC as client_id)
2. The Sendspin provider creates a SendspinPlayer for this external client
3. Protocol linking parents the SendspinPlayer next to the AirPlayPlayer via the
   declared underlying player (derived-transport edge)
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
from music_assistant.providers.sendspin.bridge_manager import SendspinBridgeManagerBase
from music_assistant.providers.sendspin.bridge_role import (
    BRIDGE_BIT_DEPTH,
    BRIDGE_BYTES_PER_SAMPLE,
    BRIDGE_CHANNELS,
    BRIDGE_ROLE_ID,
    BRIDGE_SAMPLE_RATE,
    BridgePlayerRole,
)
from music_assistant.providers.sendspin.helpers import bridge_client_id_from_mac

from .helpers import player_id_to_mac_address, unix_time_to_ntp
from .stream import AirPlayStream

if TYPE_CHECKING:
    from aiosendspin.server import ExternalStreamStartRequest, SendspinClient, SendspinServer
    from aiosendspin.server.roles import AudioChunk

    from music_assistant.models.player import Player
    from music_assistant.providers.sendspin.provider import SendspinProvider

    from .player import AirPlayPlayer
    from .provider import AirPlayProvider


def get_bridge_client_id(airplay_player: AirPlayPlayer) -> str | None:
    """
    Get the Sendspin bridge client ID for an AirPlay player.

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
    """
    Manages the Sendspin to AirPlay bridge for a single player.

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
        """
        Initialize the bridge.

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
        self._airplay_stream: AirPlayStream | None = None
        self._is_streaming = False
        self._next_expected_timestamp_us: int | None = None
        self._drop_until_us: int = 0
        self._start_aligned = False
        self._write_queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._writer_task: asyncio.Task[None] | None = None
        self._airplay_stream_start_task: asyncio.Task[None] | None = None
        self._airplay_stream_ready = asyncio.Event()
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
            # While the player@v1 role will never be used for bridges, it is required by
            # aiosendspin to parse the player@v1_support/ClientHelloPlayerSupport object
            supported_roles=[BRIDGE_ROLE_ID, "player@v1"],
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
            sendspin_prov.register_bridge_underlying_player(
                self._bridge_client_id, self.airplay_player.player_id
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
                on_mute_change=self._on_mute_change,
                on_stream_start=self._on_bridge_stream_start,
                on_stream_end=self._on_bridge_stream_end,
                initial_volume=self.airplay_player.volume_level or 25,
            )
            self._bridge_role.setup_audio_requirements()
            self._refresh_bridge_timing()

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

    def _refresh_bridge_timing(self) -> None:
        """
        Push the AirPlay startup latency to the bridge role.

        ``wait_start`` is the lead time the device needs before audio begins, so
        Sendspin schedules the first chunk that far ahead instead of dropping it.
        ``min_buffer_ms`` is 0 — the device carries its own jitter buffer.
        """
        if self._bridge_role is None:
            return
        self._bridge_role.set_timing(
            required_lead_time_ms=int(self.airplay_player.wait_start),
            min_buffer_ms=0,
        )

    def _on_stream_start(self, request: ExternalStreamStartRequest) -> None:
        """
        Handle stream start request from Sendspin server.

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
        # Bridge outlives config changes, so re-read wait_start for the current protocol.
        self._refresh_bridge_timing()
        # Capture and detach old stream resources before scheduling their cleanup.
        # This prevents the async cleanup from accidentally destroying the new
        # stream's resources, which reuse the same instance variables.
        old_stream = self._airplay_stream
        old_writer_task = self._writer_task
        old_stream_start_task = self._airplay_stream_start_task

        self._airplay_stream = None
        self._writer_task = None
        self._airplay_stream_start_task = None
        self.airplay_player.stream = None
        self._airplay_stream_ready.clear()

        if old_stream or old_writer_task or old_stream_start_task:
            prev_cleanup = self._cleanup_task
            self._cleanup_task = self.mass.create_task(
                self._cleanup_old_stream(
                    old_stream, old_writer_task, old_stream_start_task, prev_cleanup
                )
            )

        self._is_streaming = True
        self._next_expected_timestamp_us = None
        self._drop_until_us = 0
        self._start_aligned = False

    def _on_bridge_stream_start(self) -> None:
        """
        Start the writer task when the PushStream notifies us the stream has started.

        Called via the BridgePlayerRole.on_stream_start callback when the
        PushStream begins delivering audio chunks.
        """
        # The stream might not yet be cleaned up completely (on rapid skips for example)
        old_stream = self._airplay_stream
        old_writer_task = self._writer_task
        old_stream_start_task = self._airplay_stream_start_task

        self._airplay_stream = None
        self._writer_task = None
        self._airplay_stream_start_task = None
        self.airplay_player.stream = None

        if old_stream or old_writer_task or old_stream_start_task:
            prev_cleanup = self._cleanup_task
            self._cleanup_task = self.mass.create_task(
                self._cleanup_old_stream(
                    old_stream, old_writer_task, old_stream_start_task, prev_cleanup
                )
            )

        self._is_streaming = True
        self._airplay_stream_ready.clear()
        self._next_expected_timestamp_us = None
        self._drop_until_us = 0
        self._start_aligned = False
        # Drain stale audio data from the previous stream
        while not self._write_queue.empty():
            self._write_queue.get_nowait()
        self.airplay_player.sync_volume_level()
        self._writer_task = self.mass.create_task(self._cli_writer())
        self.logger.info(
            "Bridge writer started for %s, awaiting first chunk",
            self.airplay_player.display_name,
        )

    async def _start_protocol_from_chunk(self) -> None:
        """Start the AirPlay CLI process and protocol."""
        try:
            # Ensure the old CLI process is fully stopped before starting a new one.
            # Without this, both old and new processes could try to connect to the
            # same AirPlay device simultaneously.
            cleanup = self._cleanup_task
            if cleanup and not cleanup.done():
                await cleanup

            # Derive start_ntp from _drop_until_us (set on first chunk arrival)
            # to give the CLI enough lead time to connect and fill the output buffer.
            # _drop_until_us may use a different clock, convert to NTP
            sendspin_clock_now_us = self.sendspin_server.clock.now_us()
            unix_clock_now = time.time()
            future_s = (self._drop_until_us - sendspin_clock_now_us) / 1_000_000
            start_ntp = unix_time_to_ntp(unix_clock_now + future_s)

            # On a rapid skip, _on_bridge_stream_start snapshots self._airplay_stream
            # for cleanup. If we assigned it earlier, the new stream would be missed
            # and leaked. Only publish once start() succeeds and this task is current.
            new_stream = AirPlayStream(self.airplay_player)
            try:
                await new_stream.start(start_ntp)
            except BaseException:
                with suppress(Exception):
                    await new_stream.stop(force=True)
                raise
            if asyncio.current_task() is not self._airplay_stream_start_task:
                with suppress(Exception):
                    await new_stream.stop(force=True)
                return
            self._airplay_stream = new_stream
            self.airplay_player.stream = new_stream
            self._airplay_stream_ready.set()
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
            # Stop accepting chunks, unblock the writer, and schedule full cleanup
            self._is_streaming = False
            self._airplay_stream_ready.set()
            self._schedule_cleanup()

    async def _wait_for_airplay_connection(self) -> None:
        """Wait for AirPlay connection in the background and log the result."""
        if not self._airplay_stream:
            return
        try:
            await self._airplay_stream.wait_for_connection()
            self.logger.info(
                "AirPlay connection established for %s", self.airplay_player.display_name
            )
        except Exception as err:
            self.logger.warning(
                "AirPlay connection failed for %s: %s",
                self.airplay_player.display_name,
                err,
            )

    def _on_volume_change(self, volume: int) -> None:
        """Forward volume changes to the AirPlay player."""
        self.mass.create_task(self.airplay_player.volume_set(volume))

    def _on_mute_change(self, muted: bool) -> None:
        """Forward mute changes to the AirPlay player."""
        self.mass.create_task(self.airplay_player.volume_mute(muted))

    def _on_bridge_stream_end(self) -> None:
        """
        Stop the AirPlay protocol immediately when the stream ends.

        Rather than just sending EOF (which lets the CLI play out its buffer),
        we schedule a full cleanup that kills the CLI process immediately.
        """
        self._is_streaming = False
        self._next_expected_timestamp_us = None
        # Schedule full streaming cleanup - this kills the CLI process immediately
        # so AirPlay stops playing instead of draining its 30s buffer.
        self._schedule_cleanup()

    def _schedule_cleanup(self) -> None:
        """
        Schedule cleanup of the current stream resources under the bridge lock.

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
        stream: AirPlayStream | None,
        writer_task: asyncio.Task[None] | None,
        stream_start_task: asyncio.Task[None] | None,
        prev_cleanup: asyncio.Task[None] | None = None,
    ) -> None:
        """
        Clean up captured resources from a previous stream.

        Unlike _stop_streaming(), this operates on explicitly captured references
        rather than instance variables. This prevents a race condition where the
        async cleanup runs after a new stream has already reused the instance
        variables, accidentally destroying the new stream's protocol/writer.

        :param stream: The old AirPlay stream to stop.
        :param writer_task: The old writer task to cancel.
        :param stream_start_task: The old stream start task to cancel.
        :param prev_cleanup: A prior cleanup task to await first (chaining).
        """
        # Wait for any chained prior cleanup to complete first
        if prev_cleanup and not prev_cleanup.done():
            with suppress(Exception):
                await prev_cleanup

        if stream_start_task and not stream_start_task.done():
            stream_start_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await stream_start_task
        if writer_task and not writer_task.done():
            writer_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await writer_task
        if stream:
            with suppress(Exception):
                await stream.stop(force=True)

    def _on_audio_chunk(self, chunk: AudioChunk) -> None:
        """Handle audio chunk from Sendspin PushStream."""
        if not self._is_streaming:
            return

        # Detect a done/failed protocol start task and stop streaming
        if self._airplay_stream_start_task is not None and self._airplay_stream_start_task.done():
            exc = (
                self._airplay_stream_start_task.exception()
                if not self._airplay_stream_start_task.cancelled()
                else None
            )
            if self._airplay_stream_start_task.cancelled() or exc:
                self.logger.warning(
                    "Protocol start task failed for %s, stopping streaming",
                    self.airplay_player.display_name,
                )
                self._is_streaming = False
                self._schedule_cleanup()
                return

        if self._airplay_stream_start_task is None:
            # Set the target start time (wait_start) in the future so the CLI
            # has enough time to connect and fill the device's output buffer.
            wait_start_us = int(self.airplay_player.wait_start * 1_000)
            self._drop_until_us = self.sendspin_server.clock.now_us() + wait_start_us
            self._start_aligned = False
            self._airplay_stream_start_task = self.mass.create_task(
                self._start_protocol_from_chunk()
            )

        # Drop chunks that end entirely before the target start time.
        chunk_end_us = chunk.timestamp_us + chunk.duration_us
        if self._drop_until_us and chunk_end_us <= self._drop_until_us:
            return

        # Align the first written chunk so byte 0 of stdin matches start_ntp.
        if not self._start_aligned:
            if self._align_first_chunk(chunk):
                self._start_aligned = True
                self._next_expected_timestamp_us = chunk.timestamp_us + chunk.duration_us
            return

        if self._next_expected_timestamp_us is not None:
            gap_us = chunk.timestamp_us - self._next_expected_timestamp_us
            if abs(gap_us) > 1_000:
                self.logger.warning(
                    "Unexpected timestamp gap of %d µs for %s",
                    gap_us,
                    self.airplay_player.display_name,
                )

        self._next_expected_timestamp_us = chunk.timestamp_us + chunk.duration_us
        self._write_queue.put_nowait(chunk.data)

    def _align_first_chunk(self, chunk: AudioChunk) -> bool:
        """
        Align the first audio chunk so byte 0 of CLI stdin matches start_ntp.

        Inserts silence if the chunk starts after the target time, or trims
        the beginning if the chunk straddles it.

        :param chunk: The first audio chunk that overlaps with the start time.
        :return: True if aligned audio was queued successfully.
        """
        bytes_per_frame = BRIDGE_CHANNELS * BRIDGE_BYTES_PER_SAMPLE

        if chunk.timestamp_us > self._drop_until_us:
            # Chunk starts after start_ntp — pad with silence
            gap_us = chunk.timestamp_us - self._drop_until_us
            silence_frames = int(gap_us * BRIDGE_SAMPLE_RATE / 1_000_000)
            if silence_frames > 0:
                self.logger.debug(
                    "Inserting %d frames of silence to align start for %s",
                    silence_frames,
                    self.airplay_player.display_name,
                )
                self._write_queue.put_nowait(b"\x00" * (silence_frames * bytes_per_frame))
            self._write_queue.put_nowait(chunk.data)
            return True
        if chunk.timestamp_us < self._drop_until_us:
            # Chunk straddles start_ntp — trim the beginning
            trim_us = self._drop_until_us - chunk.timestamp_us
            trim_frames = int(trim_us * BRIDGE_SAMPLE_RATE / 1_000_000)
            trim_bytes = trim_frames * bytes_per_frame
            if trim_bytes < len(chunk.data):
                self.logger.debug(
                    "Trimming %d frames from first chunk for %s",
                    trim_frames,
                    self.airplay_player.display_name,
                )
                self._write_queue.put_nowait(chunk.data[trim_bytes:])
                return True
            return False
        self._write_queue.put_nowait(chunk.data)
        return True

    async def _cli_writer(self) -> None:
        """
        Write queued audio data to the CLI process stdin.

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
                await asyncio.wait_for(self._airplay_stream_ready.wait(), timeout=30.0)
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
                if not self._airplay_stream:
                    if data is None:
                        return
                    continue
                if data is None:
                    with suppress(Exception):
                        await self._airplay_stream.write_audio_eof()
                    return
                with suppress(Exception):
                    await self._airplay_stream.write_audio(data)
        finally:
            # Only clear if this writer is still the active one.
            if self._writer_task is asyncio.current_task():
                self._writer_task = None

    async def _stop_streaming(self) -> None:
        """Stop streaming (internal, called with lock held)."""
        self._is_streaming = False
        self._next_expected_timestamp_us = None
        self._airplay_stream_ready.clear()
        if self._airplay_stream_start_task:
            self._airplay_stream_start_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await self._airplay_stream_start_task
            self._airplay_stream_start_task = None
        if self._writer_task:
            self._writer_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await self._writer_task
            self._writer_task = None
        while not self._write_queue.empty():
            self._write_queue.get_nowait()
        if self._airplay_stream:
            await self._airplay_stream.stop(force=True)
            self._airplay_stream = None
            self.airplay_player.stream = None


class SendspinBridgeManager(SendspinBridgeManagerBase[SendspinAirPlayBridge]):
    """Manages Sendspin bridges for all AirPlay players."""

    def stop_streaming(self, airplay_player_id: str) -> bool:
        """
        Stop streaming for a bridged AirPlay player.

        :param airplay_player_id: The AirPlay player ID.
        :return: True if a bridge was found and stopped, False otherwise.
        """
        if bridge := self._bridges.get(airplay_player_id):
            bridge._on_bridge_stream_end()
            return True
        return False

    def _bridge_client_id(self, player: Player) -> str | None:
        """Return the Sendspin client_id used to bridge an AirPlay player."""
        return get_bridge_client_id(cast("AirPlayPlayer", player))

    def _create_bridge(self, player: Player) -> SendspinAirPlayBridge:
        """Create a bridge instance for an AirPlay player."""
        sendspin_server = self.sendspin_server
        assert sendspin_server is not None  # guaranteed by _lifecycle_allows_bridge
        return SendspinAirPlayBridge(
            cast("AirPlayProvider", self.provider),
            cast("AirPlayPlayer", player),
            sendspin_server,
        )

    def _should_have_bridge(self, player: Player) -> bool:
        """Return whether an AirPlay player should have a Sendspin bridge."""
        return get_bridge_client_id(cast("AirPlayPlayer", player)) is not None
