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

from music_assistant.constants import CONF_SYNC_ADJUST
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

from .helpers import player_id_to_mac_address
from .stream import AirPlayStream

if TYPE_CHECKING:
    from aiosendspin.server import ExternalStreamStartRequest, SendspinClient, SendspinServer
    from aiosendspin.server.roles import AudioChunk

    from music_assistant.models.player import Player
    from music_assistant.providers.sendspin.provider import SendspinProvider

    from .player import AirPlayPlayer
    from .provider import AirPlayProvider


# Upper bound on how far ahead of the audible position the bridge feeds the CLI.
# Sendspin can hand a late joiner its whole producer backlog at once (tens of
# seconds), and dumping that into the binary's stdin ahead of the start anchor
# desyncs playback. Pace writes to keep the device buffered to at most this many
# seconds of audio ahead of real time -- comfortably above the binary's own
# prefill need (~wait_start + its ~2 s buffer) yet far below a whole-track backlog.
MAX_DEVICE_BUFFER_SECONDS: float = 8.0


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


def sendspin_audible_instant_to_unix_ms(
    audible_instant_us: int,
    sendspin_now_us: int,
    unix_now: float,
) -> int:
    """
    Map an audible instant on the Sendspin clock to a unix epoch millisecond.

    Sendspin schedules playback on its own monotonic clock (``server.clock.now_us()``)
    whereas the AirPlay binary's ``--start-unix-ms`` contract is unix wall-clock.
    The two clocks share no epoch, so only the *offset from now* transfers between
    them: this takes how far ``audible_instant_us`` sits in the future on the
    Sendspin clock and applies that same offset to a unix reading captured at the
    same instant. Because the offset is a delta on a single clock, the standing
    offset between the Sendspin clock and unix wall-clock cancels out -- the only
    residual is the rate skew across the (sub-second to a few seconds) lead, which
    stays well under a millisecond. ``sendspin_now_us`` and ``unix_now`` must be
    sampled back to back for that cancellation to hold.

    :param audible_instant_us: Sendspin-clock instant the first sample must be audible.
    :param sendspin_now_us: ``sendspin_server.clock.now_us()`` captured now.
    :param unix_now: ``time.time()`` captured at the same instant as sendspin_now_us.
    :return: The unix epoch millisecond that coincides with ``audible_instant_us``.
    """
    future_s = (audible_instant_us - sendspin_now_us) / 1_000_000
    return int((unix_now + future_s) * 1000)


def device_buffer_ahead_seconds(
    start_unix_ms: int,
    bytes_written: int,
    bytes_per_second: int,
    unix_now: float,
) -> float:
    """
    Seconds of audio the CLI write cursor is buffered ahead of real time.

    Byte 0 written to the CLI is audible at ``start_unix_ms``; byte N is audible
    at ``start_unix_ms + N / bytes_per_second``. The write cursor (after
    ``bytes_written`` bytes) therefore represents that play instant, and this
    returns how far it sits ahead of ``unix_now``. The writer sleeps on the
    excess over the buffer bound so a late-join backlog is fed at ~real time
    instead of dumped into the device ahead of its start anchor.

    :param start_unix_ms: Unix-epoch ms at which byte 0 is audible.
    :param bytes_written: Bytes already written to the CLI for this stream.
    :param bytes_per_second: PCM byte rate fed to the CLI.
    :param unix_now: Current unix time in seconds.
    """
    cursor_play_time_s = start_unix_ms / 1000 + bytes_written / bytes_per_second
    return cursor_play_time_s - unix_now


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
        # Unix-epoch ms at which byte 0 written to the CLI is audible (0 = unset).
        # Used to pace writes so the device buffer stays bounded (see _cli_writer).
        self._start_unix_ms: int = 0
        self._write_queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._writer_task: asyncio.Task[None] | None = None
        self._airplay_stream_start_task: asyncio.Task[None] | None = None
        self._airplay_stream_ready = asyncio.Event()
        self._cleanup_task: asyncio.Task[None] | None = None
        self._stream_generation: object | None = None
        self._player_generation: int | None = None
        self._pending_stream_start: tuple[ExternalStreamStartRequest, int] | None = None
        self._pending_bridge_start = False
        self._unregistering = False
        self._stop_requests = 0
        self._lock = asyncio.Lock()

    @property
    def is_registered(self) -> bool:
        """Return whether the bridge is registered with Sendspin."""
        return self._sendspin_client is not None

    async def start(self) -> None:
        """Register the AirPlay player as an external Sendspin client."""
        self._unregistering = False
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
        self._unregistering = True
        stop_task = asyncio.create_task(self._stop())
        try:
            await asyncio.shield(stop_task)
        except asyncio.CancelledError:
            await stop_task
            raise

    async def stop_streaming(self) -> None:
        """Stop the active bridge writer and AirPlay stream."""
        self._stop_requests += 1
        cleanup_task = self._schedule_cleanup()
        try:
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                await cleanup_task
                raise
        finally:
            self._stop_requests -= 1
            if self._stop_requests == 0:
                self._resume_pending_start()

    async def _stop(self) -> None:
        """Stop streaming and unregister the bridge as one transaction."""
        self._stop_requests += 1
        cleanup_task = self._schedule_cleanup()
        try:
            await cleanup_task
            async with self._lock:
                if self._sendspin_client and self._bridge_client_id:
                    await self.sendspin_server.remove_client(self._bridge_client_id)
                    self._sendspin_client = None
                    self._bridge_role = None
        finally:
            self._stop_requests -= 1
        self.logger.debug("Sendspin bridge stopped for %s", self.airplay_player.display_name)

    def _resume_pending_start(self) -> None:
        """Resume the latest deferred Sendspin start if it still owns the player."""
        pending_stream_start = self._pending_stream_start
        pending_bridge_start = self._pending_bridge_start
        self._pending_stream_start = None
        self._pending_bridge_start = False
        if pending_stream_start is None or self._unregistering:
            return
        request, generation = pending_stream_start
        if not self.airplay_player.is_stream_generation_current(generation):
            return
        self._on_stream_start(request)
        if pending_bridge_start:
            self._on_bridge_stream_start()

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
        if self._stop_requests:
            generation = self.airplay_player.stream_generation
            self._pending_stream_start = (request, generation)
            self.logger.debug(
                "Deferring Sendspin stream start for %s during bridge teardown",
                self.airplay_player.display_name,
            )
            return
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
        self._stream_generation = object()
        self._player_generation = self.airplay_player.reserve_stream_generation()
        # Bridge outlives config changes, so re-read the current timing values.
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
        self._start_unix_ms = 0

    def _on_bridge_stream_start(self) -> None:
        """
        Start the writer task when the PushStream notifies us the stream has started.

        Called via the BridgePlayerRole.on_stream_start callback when the
        PushStream begins delivering audio chunks.
        """
        if self._stop_requests:
            self._pending_bridge_start = True
            self.logger.debug(
                "Deferring bridge writer start for %s during teardown",
                self.airplay_player.display_name,
            )
            return
        if self._stream_generation is None:
            self.logger.debug(
                "Ignoring stale bridge writer start for %s",
                self.airplay_player.display_name,
            )
            return
        # The stream might not yet be cleaned up completely (on rapid skips for example)
        old_stream = self._airplay_stream
        old_writer_task = self._writer_task
        old_stream_start_task = self._airplay_stream_start_task

        self._airplay_stream = None
        self._writer_task = None
        self._airplay_stream_start_task = None

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
        self._start_unix_ms = 0
        # Drain stale audio data from the previous stream
        while not self._write_queue.empty():
            self._write_queue.get_nowait()
        self.airplay_player.sync_volume_level()
        previous_cleanup = self._cleanup_task
        self._writer_task = self.mass.create_task(self._cli_writer(previous_cleanup))
        self.logger.info(
            "Bridge writer started for %s, awaiting first chunk",
            self.airplay_player.display_name,
        )

    async def _start_protocol_from_chunk(
        self,
        previous_cleanup: asyncio.Task[None] | None = None,
        expected_generation: int | None = None,
    ) -> None:
        """Start the AirPlay CLI process and protocol."""
        if expected_generation is None:
            expected_generation = self.airplay_player.reserve_stream_generation()
        try:
            # Ensure the old CLI process is fully stopped before starting a new one.
            # Without this, both old and new processes could try to connect to the
            # same AirPlay device simultaneously.
            if previous_cleanup and not previous_cleanup.done():
                await previous_cleanup

            # Derive the audible start instant from _drop_until_us (set on first
            # chunk arrival) so the CLI has enough lead to connect and establish
            # the session. _drop_until_us is on the Sendspin (monotonic) clock;
            # map it to the unix epoch ms the binary's --start-unix-ms expects.
            sendspin_clock_now_us = self.sendspin_server.clock.now_us()
            unix_clock_now = time.time()
            start_unix_ms = sendspin_audible_instant_to_unix_ms(
                self._drop_until_us, sendspin_clock_now_us, unix_clock_now
            )
            lead_us = self._drop_until_us - sendspin_clock_now_us
            if lead_us <= 0:
                # Session setup outran the lead budget (e.g. a slow teardown of a
                # previous stream ahead of us). The anchor is already in the past,
                # so the binary cannot honour it and playback starts late relative
                # to the Sendspin timeline. Surface it instead of failing silently.
                self.logger.warning(
                    "AirPlay start anchor for %s is not in the future "
                    "(setup exceeded the %dms lead) - playback may start late",
                    self.airplay_player.display_name,
                    int(self.airplay_player.wait_start),
                )
            sync_adjust = self.airplay_player.config.get_value(CONF_SYNC_ADJUST, 0)
            if isinstance(sync_adjust, int) and sync_adjust != 0:
                start_unix_ms += sync_adjust
            # Publish the audible anchor so the writer can pace against it.
            self._start_unix_ms = start_unix_ms

            new_stream = AirPlayStream(self.airplay_player)
            try:
                await self.airplay_player.start_stream(
                    new_stream,
                    start_unix_ms,
                    expected_generation=expected_generation,
                )
            except BaseException:
                with suppress(Exception):
                    await new_stream.stop(force=True)
                raise
            if (
                asyncio.current_task() is not self._airplay_stream_start_task
                or self.airplay_player.stream is not new_stream
            ):
                with suppress(Exception):
                    await new_stream.stop(force=True)
                return
            self._airplay_stream = new_stream
            self._airplay_stream_ready.set()
            self.logger.info(
                "Bridge protocol started for %s (start_unix_ms=%s, lookahead=%.0fms)",
                self.airplay_player.display_name,
                start_unix_ms,
                lead_us / 1000,
            )
            self.mass.create_task(self._wait_for_airplay_connection())
        except Exception as err:
            if asyncio.current_task() is not self._airplay_stream_start_task:
                return
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
        self._pending_stream_start = None
        self._pending_bridge_start = False
        self._is_streaming = False
        self._next_expected_timestamp_us = None
        # Schedule full streaming cleanup - this kills the CLI process immediately
        # so AirPlay stops playing instead of draining its 30s buffer.
        self._schedule_cleanup()

    def _schedule_cleanup(self) -> asyncio.Task[None]:
        """
        Detach and schedule cleanup of the current stream resources.

        Resources are captured before the cleanup task can yield so a replacement
        generation can never be mistaken for the generation being stopped.
        """
        self._stream_generation = None
        self._player_generation = None
        self._is_streaming = False
        self._next_expected_timestamp_us = None
        self._airplay_stream_ready.clear()
        stream_start_task = self._airplay_stream_start_task
        writer_task = self._writer_task
        stream = self._airplay_stream
        self._airplay_stream_start_task = None
        self._writer_task = None
        self._airplay_stream = None
        while not self._write_queue.empty():
            self._write_queue.get_nowait()

        previous_cleanup = self._cleanup_task
        cleanup_task = self.mass.create_task(
            self._cleanup_old_stream(
                stream,
                writer_task,
                stream_start_task,
                previous_cleanup,
            )
        )
        self._cleanup_task = cleanup_task
        return cleanup_task

    async def _cleanup_old_stream(
        self,
        stream: AirPlayStream | None,
        writer_task: asyncio.Task[None] | None,
        stream_start_task: asyncio.Task[None] | None,
        prev_cleanup: asyncio.Task[None] | None = None,
    ) -> None:
        """
        Clean up captured resources from a previous stream.

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
            if self.airplay_player.stream is stream:
                self.airplay_player.stream = None

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
            # Anchor byte 0 to the instant Sendspin scheduled for the first chunk it
            # actually delivered -- i.e. trust the shared timeline instead of inventing
            # our own "now + wait_start" start. Sendspin already schedules the first
            # sample the AirPlay setup budget ahead because we report
            # required_lead_time_ms = wait_start at registration:
            #   * fresh track  -> the first chunk IS file position 0, so the intro is
            #     kept (the old "now + wait_start" anchor discarded the opening seconds
            #     whenever Sendspin had scheduled position 0 earlier than that instant);
            #   * late join     -> the first chunk is the catch-up target, i.e. the
            #     group's current playback position, so the joiner lands in sync.
            self._drop_until_us = chunk.timestamp_us
            self._start_aligned = False
            previous_cleanup = self._cleanup_task
            expected_generation = self._player_generation
            if expected_generation is None:
                expected_generation = self.airplay_player.reserve_stream_generation()
            self._airplay_stream_start_task = self.mass.create_task(
                self._start_protocol_from_chunk(
                    previous_cleanup,
                    expected_generation,
                )
            )

        # Drop chunks that end entirely before the target start time.
        chunk_end_us = chunk.timestamp_us + chunk.duration_us
        if self._drop_until_us and chunk_end_us <= self._drop_until_us:
            return

        # Align the first written chunk so byte 0 of stdin matches the start time.
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
        Align the first audio chunk so byte 0 of CLI stdin matches the start time.

        Inserts silence if the chunk starts after the target time, or trims
        the beginning if the chunk straddles it.

        :param chunk: The first audio chunk that overlaps with the start time.
        :return: True if aligned audio was queued successfully.
        """
        bytes_per_frame = BRIDGE_CHANNELS * BRIDGE_BYTES_PER_SAMPLE

        if chunk.timestamp_us > self._drop_until_us:
            # Chunk starts after the start time — pad with silence
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
            # Chunk straddles the start time — trim the beginning
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

    async def _cli_writer(
        self,
        previous_cleanup: asyncio.Task[None] | None = None,
    ) -> None:
        """
        Write queued audio data to the CLI process stdin.

        Waits for any pending cleanup and then for the new protocol to be
        ready before writing. Runs as a single task so writes are serialised
        and ordered. A None sentinel signals end-of-stream: write EOF to
        stdin and exit.

        Writes are paced against the audible anchor so the device is never
        buffered more than ``MAX_DEVICE_BUFFER_SECONDS`` ahead of real time.
        Without this, a late joiner's catch-up backlog (Sendspin can deliver
        its whole producer buffer at once) would be dumped into the CLI far
        ahead of the start anchor, desynchronising playback.
        """
        bytes_per_second = BRIDGE_SAMPLE_RATE * BRIDGE_CHANNELS * BRIDGE_BYTES_PER_SAMPLE
        bytes_written = 0
        try:
            # Wait for any pending cleanup from a previous stream to complete
            # so we don't write to a stale/dead protocol.
            if previous_cleanup and not previous_cleanup.done():
                with suppress(Exception):
                    await previous_cleanup
                if self._cleanup_task is previous_cleanup:
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
                # Keep the device buffered at most MAX_DEVICE_BUFFER_SECONDS ahead of
                # real time so a late joiner's catch-up backlog is fed gradually rather
                # than dumped into the CLI ahead of its start anchor (which desyncs it).
                if self._start_unix_ms:
                    ahead = device_buffer_ahead_seconds(
                        self._start_unix_ms, bytes_written, bytes_per_second, time.time()
                    )
                    if ahead > MAX_DEVICE_BUFFER_SECONDS:
                        await asyncio.sleep(ahead - MAX_DEVICE_BUFFER_SECONDS)
                with suppress(Exception):
                    await self._airplay_stream.write_audio(data)
                bytes_written += len(data)
        finally:
            # Only clear if this writer is still the active one.
            if self._writer_task is asyncio.current_task():
                self._writer_task = None


class SendspinBridgeManager(SendspinBridgeManagerBase[SendspinAirPlayBridge]):
    """Manages Sendspin bridges for all AirPlay players."""

    async def stop_streaming(self, airplay_player_id: str) -> bool:
        """
        Stop streaming for a bridged AirPlay player.

        :param airplay_player_id: The AirPlay player ID.
        :return: True if a bridge was found and stopped, False otherwise.
        """
        if bridge := self._bridges.get(airplay_player_id):
            await bridge.stop_streaming()
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
