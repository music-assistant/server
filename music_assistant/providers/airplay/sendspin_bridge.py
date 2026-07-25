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

# How long to keep a connected CLI alive after a sendspin stream ends, waiting
# for the next stream (seek/next) to reuse it warm. A real stop tears down after
# this window. Comfortably longer than a sendspin stream restart, short enough
# that a genuine stop stops the device promptly.
BRIDGE_WARM_GRACE_SECONDS: float = 4.0


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
    whereas the AirPlay START command uses unix wall-clock.
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
        # Whether the current stream has been anchored with its first START.
        self._started = False
        self._cleanup_task: asyncio.Task[None] | None = None
        # Timer id for the deferred teardown on stream end. A seek/next ends the
        # sendspin stream and immediately starts a new one; deferring the CLI
        # teardown across that gap lets the next stream reuse the connected
        # binary via flush-refill instead of a cold reconnect.
        self._teardown_timer_id = f"bridge_teardown_{airplay_player.player_id}"
        self._lock = asyncio.Lock()

    @property
    def is_registered(self) -> bool:
        """Return whether the bridge is registered with Sendspin."""
        return self._sendspin_client is not None

    @property
    def bridge_client_id(self) -> str | None:
        """Return the Sendspin player ID registered for this bridge."""
        return self._bridge_client_id

    @property
    def owns_airplay_stream(self) -> bool:
        """Return whether this bridge owns the AirPlay player's current stream."""
        return (
            self._airplay_stream is not None and self.airplay_player.stream is self._airplay_stream
        )

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
        self.mass.cancel_timer(self._teardown_timer_id)
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

    def _stream_is_warm_eligible(self) -> bool:
        """
        Return whether the current AirPlayStream can absorb a new stream via flush-refill.

        A kept stream must still be running, already connected, and already
        anchored with its first START. Every streaming route rides the same
        persistent stdin flush-refill.
        """
        stream = self._airplay_stream
        return stream is not None and stream.running and stream.connected and self._started

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
        # A new stream arrived within the grace window: cancel the deferred
        # teardown so the previous stream's still-connected CLI survives to be
        # reused via flush-refill instead of cold-restarting.
        self.mass.cancel_timer(self._teardown_timer_id)
        # Bridge outlives config changes, so re-read the current timing values.
        self._refresh_bridge_timing()
        # Capture and detach old stream resources before scheduling their cleanup.
        # This prevents the async cleanup from accidentally destroying the new
        # stream's resources, which reuse the same instance variables. A warm-
        # eligible stream is kept out of the snapshot entirely so it survives
        # into the new stream instead of being torn down.
        keep_stream = self._stream_is_warm_eligible()
        old_stream = None if keep_stream else self._airplay_stream
        old_writer_task = self._writer_task
        old_stream_start_task = self._airplay_stream_start_task

        if not keep_stream:
            self._airplay_stream = None
            self.airplay_player.stream = None
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
        self._started = keep_stream

    def _on_bridge_stream_start(self) -> None:
        """
        Start the writer task when the PushStream notifies us the stream has started.

        Called via the BridgePlayerRole.on_stream_start callback when the
        PushStream begins delivering audio chunks.
        """
        # The stream might not yet be cleaned up completely (on rapid skips for example).
        # A warm-eligible stream is kept out of the snapshot so it survives into the
        # new stream instead of being torn down (see _stream_is_warm_eligible).
        keep_stream = self._stream_is_warm_eligible()
        old_stream = None if keep_stream else self._airplay_stream
        old_writer_task = self._writer_task
        old_stream_start_task = self._airplay_stream_start_task

        if not keep_stream:
            self._airplay_stream = None
            self.airplay_player.stream = None
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
        self._started = keep_stream
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

            # Derive the audible start instant from _drop_until_us (set on first
            # chunk arrival) so the CLI has enough lead to connect and establish
            # the session. _drop_until_us is on the Sendspin (monotonic) clock;
            # map it to the unix epoch ms used by the START command.
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

            # A kept, still-connected stream (see _stream_is_warm_eligible,
            # checked by the stream-start callbacks) absorbs the new media via a
            # flush-refill on the SAME cli stdin instead of a cold reconnect.
            kept_stream = self._airplay_stream
            if kept_stream is not None:
                if await self._start_warm_stream(kept_stream, start_unix_ms):
                    return
                # Warm handover failed - tear down the kept stream and fall through
                # to the cold path below, which spawns a fresh process.
                with suppress(Exception):
                    await kept_stream.stop(force=True)
                self._airplay_stream = None
                self.airplay_player.stream = None

            # On a rapid skip, _on_bridge_stream_start snapshots self._airplay_stream
            # for cleanup. If we assigned it earlier, the new stream would be missed
            # and leaked. Only publish once connect() succeeds and this task is current.
            new_stream = AirPlayStream(self.airplay_player)
            if not await self._start_cold_stream(new_stream, start_unix_ms):
                return
            self.logger.info(
                "Bridge protocol started for %s (start_unix_ms=%s, lookahead=%.0fms)",
                self.airplay_player.display_name,
                start_unix_ms,
                lead_us / 1000,
            )
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

    async def _start_cold_stream(self, stream: AirPlayStream, start_unix_ms: int) -> bool:
        """
        Connect a fresh bridge transport and anchor its first START.

        :param stream: New AirPlay stream to start.
        :param start_unix_ms: Audible-start instant for the first sample.
        :return: True when the stream is anchored, False when superseded.
        """
        try:
            await stream.connect()
            await stream.wait_for_connection()
            if asyncio.current_task() is not self._airplay_stream_start_task:
                await stream.stop(force=True)
                return False
            self._airplay_stream = stream
            self.airplay_player.stream = stream
            # The binary buffers stdin into its ring from process start; unblock
            # the writer so it feeds PCM, then anchor the start.
            self._airplay_stream_ready.set()
            await stream.start(start_unix_ms)
            self._started = True
        except BaseException:
            with suppress(Exception):
                await stream.stop(force=True)
            raise
        return True

    async def _start_warm_stream(self, stream: AirPlayStream, start_unix_ms: int) -> bool:
        """
        Flush a kept, still-connected stream and resume it on the new track.

        The stream is flushed in place (receiver + ring + stdin drained) while
        its connection stays alive, then the new PCM is fed into the SAME cli
        stdin and re-anchored with a single START. Returns True once resumed; any
        failure returns False so the caller falls back to a cold restart.

        :param stream: The kept AirPlayStream to flush and resume.
        :param start_unix_ms: The audible-start instant to re-anchor at.
        """
        try:
            if not await stream.flush():
                return False
            if asyncio.current_task() is not self._airplay_stream_start_task:
                # A newer stream start owns the bridge; leave the stream to it.
                return False
            # The binary drained its ring + stdin on flush and keeps reading
            # stdin; unblock the writer to feed the new track into the SAME cli
            # stdin, then re-anchor the resumed start.
            self._airplay_stream_ready.set()
            await stream.start(start_unix_ms)
            self._started = True
            self.logger.info(
                "Bridge warm handover for %s (start_unix_ms=%d)",
                self.airplay_player.display_name,
                start_unix_ms,
            )
        except asyncio.CancelledError:
            raise
        except Exception as err:
            self.logger.warning(
                "Warm handover failed for %s (%r), falling back to a cold restart",
                self.airplay_player.display_name,
                err,
            )
            return False
        return True

    def _on_volume_change(self, volume: int) -> None:
        """Forward volume changes to the AirPlay player."""
        self.mass.create_task(self.airplay_player.volume_set(volume))

    def _on_mute_change(self, muted: bool) -> None:
        """Forward mute changes to the AirPlay player."""
        self.mass.create_task(self.airplay_player.volume_mute(muted))

    def _on_bridge_stream_end(self) -> None:
        """
        Handle the sendspin stream ending: defer the CLI teardown briefly.

        A seek or next-track ends the stream and immediately starts a new one.
        Tearing the CLI down here would force every such switch through a cold
        reconnect; instead we keep the connected binary alive for a short grace
        window so the next stream can reuse it via flush-refill. If no new
        stream arrives within the window (a real stop), the deferred cleanup
        kills the CLI so AirPlay stops instead of draining its buffer.
        """
        self._is_streaming = False
        self._next_expected_timestamp_us = None
        self.mass.call_later(
            BRIDGE_WARM_GRACE_SECONDS, self._schedule_cleanup, task_id=self._teardown_timer_id
        )

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
            self._airplay_stream_start_task = self.mass.create_task(
                self._start_protocol_from_chunk()
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

    async def _cli_writer(self) -> None:
        """
        Write queued audio data to the CLI's persistent stdin.

        Waits for any pending cleanup and then for the new stream to be ready
        before writing. Runs as a single task so writes are serialised and
        ordered. A None sentinel signals end-of-stream: write EOF to stdin and
        exit.

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

    async def _stop_streaming(self) -> None:
        """Stop streaming (internal, called with lock held)."""
        self._is_streaming = False
        self._next_expected_timestamp_us = None
        self._airplay_stream_ready.clear()
        self._started = False
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

    def get_transport_command_target(self, airplay_player_id: str) -> str | None:
        """
        Return the visible Sendspin session player controlling an AirPlay bridge.

        :param airplay_player_id: The AirPlay player reporting the command.
        :return: The owning Sendspin session player ID, or None when Sendspin does
            not currently control the AirPlay player.
        """
        if not (bridge := self._bridges.get(airplay_player_id)):
            return None
        if not bridge.owns_airplay_stream:
            return None
        if not (bridge_client_id := bridge.bridge_client_id):
            return None
        if not (bridge_player := self.mass.players.get_player(bridge_client_id)):
            return None

        sync_leader_id = bridge_player.synced_to
        if sync_leader_id:
            if sync_leader := self.mass.players.get_player(sync_leader_id):
                return sync_leader.protocol_parent_id or sync_leader.player_id
            return sync_leader_id
        return bridge_player.protocol_parent_id or bridge_player.player_id

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
