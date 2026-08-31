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
from collections import deque
from contextlib import suppress
from typing import TYPE_CHECKING, Any, cast

from aiosendspin.models.core import ClientHelloPayload
from aiosendspin.models.core import DeviceInfo as SendspinDeviceInfo
from aiosendspin.models.player import ClientHelloPlayerSupport, SupportedAudioFormat
from aiosendspin.models.types import AudioCodec, PlayerCommand
from music_assistant_models.enums import IdentifierType, PlaybackState

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

from .constants import (
    AIRPLAY_CLOCK_READY_LEAD_MS,
    AIRPLAY_CLOCK_READY_TIMEOUT_MS,
    AIRPLAY_LATE_JOIN_MIN_HEADROOM_MS,
    AIRPLAY_SPLICE_LEAD_MARGIN_MS,
    ClockReadiness,
    StreamingProtocol,
)
from .helpers import player_id_to_mac_address
from .stream import AirPlayStream

if TYPE_CHECKING:
    from aiosendspin.server import (
        ExternalStreamStartRequest,
        SendspinClient,
        SendspinGroup,
        SendspinServer,
    )
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
# prefill need (the start lead plus its ~2 s buffer) yet far below a whole-track
# backlog.
MAX_DEVICE_BUFFER_SECONDS: float = 8.0

# How long to keep a connected CLI alive after a sendspin stream ends, waiting
# for the next stream (seek/next) to reuse it warm. A real stop tears down after
# this window. Comfortably longer than a sendspin stream restart, short enough
# that a genuine stop stops the device promptly.
BRIDGE_WARM_GRACE_SECONDS: float = 4.0

# Budget (ms) for a cold transport: a process spawn plus connect/session setup
# was measured at 1.66-1.83 s, and 2000 keeps a small margin over the slowest
# measurement.
BRIDGE_COLD_CONNECT_BUDGET_MS: int = 2000

# Lead (ms) reported to Sendspin so it schedules the first chunk that far ahead
# of the instant it wants audible. It has to cover getting the transport ready
# plus the join floor the anchor can never undercut -- a shorter lead makes the
# anchor land past the first chunks and their audio is dropped. A cold start
# adds its connect budget on top; a kept, already-connected CLI pays no connect,
# leaving only the floor.
BRIDGE_COLD_START_LEAD_MS: int = AIRPLAY_LATE_JOIN_MIN_HEADROOM_MS + BRIDGE_COLD_CONNECT_BUDGET_MS
BRIDGE_WARM_START_LEAD_MS: int = AIRPLAY_LATE_JOIN_MIN_HEADROOM_MS

# Buffer (ms) Sendspin keeps the bridge supplied with for the whole stream, and
# the only floor a live source honours -- the start lead above is a hint Sendspin
# may ignore to keep live latency down. It keeps the binary's ring fed rather
# than tracking real time, without charging every other group member the full
# start lead in added latency on a live source.
BRIDGE_MIN_BUFFER_MS: int = 1000

# Defensive cap (µs of audio) on the backlog held while the start anchor is being
# settled. Sendspin bounds what it hands a joiner at 30 s of producer backlog, so
# a hold growing past that means the anchor never resolved; the oldest chunks are
# dropped instead of letting the backlog grow without bound.
MAX_HELD_AUDIO_US: int = 45_000_000
# Companion cap on the number of held chunks: the µs cap cannot bound a run of
# zero-duration chunks. Sits generously above the count of 25 ms frames the
# bridge role delivers across MAX_HELD_AUDIO_US.
MAX_HELD_CHUNKS: int = 4_000

# Silence is queued as repeats of one shared block when filling a hole in the
# Sendspin timeline. A timeline rebase can open a hole of tens of seconds, whose
# silence as one buffer would be megabytes built on the event loop; repeating an
# immutable block leaves only the sub-block remainder to allocate, whatever the
# hole's length.
PAD_BLOCK_FRAMES: int = BRIDGE_SAMPLE_RATE
SILENCE_BLOCK: bytes = b"\x00" * (PAD_BLOCK_FRAMES * BRIDGE_CHANNELS * BRIDGE_BYTES_PER_SAMPLE)

# A transport that dies again this soon after a recovery is not coming back by
# itself (a rebooting device, a receiver that accepts the connection and drops
# it again). Re-anchoring once more would only churn CLI processes, so the
# bridge gives up instead. Measured from the moment the loss is noticed, which
# includes the several seconds a cold recovery needs before audio resumes (a
# process spawn, the clock-readiness wait and the join headroom), so this sits
# far enough beyond that for a recovered transport to prove itself. The same
# window guards the session re-join below: a bridge that gives up again this
# soon after being put back was not fixed by returning it.
BRIDGE_TRANSPORT_RECOVERY_GUARD_SECONDS: float = 30.0

# Delays (seconds) before each attempt to put a bridge back into the Sendspin
# group a give-up took it out of. The first clears a brief blackout or a receiver
# that was slow to answer; the second is what covers a speaker that rebooted,
# which is typically absent from mDNS for far longer than the first. Once they
# run out the player stays out and idle.
BRIDGE_REJOIN_ATTEMPT_DELAYS: tuple[int, ...] = (5, 30)

# Player state fields that mean the AirPlay side moved the volume or the mute, so
# the bridge role's copy of it has to follow.
_VOLUME_STATE_FIELDS: frozenset[str] = frozenset({"volume_level", "volume_muted"})


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


def unix_ms_to_sendspin_audible_instant(
    unix_ms: int,
    sendspin_now_us: int,
    unix_now: float,
) -> int:
    """
    Map a unix epoch millisecond onto an audible instant on the Sendspin clock.

    The inverse of :func:`sendspin_audible_instant_to_unix_ms`, used to place the
    instant the binary acked back on the shared Sendspin timeline so chunk
    timestamps can be measured against it. It carries the same requirement:
    ``sendspin_now_us`` and ``unix_now`` must be sampled back to back, because
    only the offset from now transfers between the two clocks.

    :param unix_ms: Unix epoch millisecond to map.
    :param sendspin_now_us: ``sendspin_server.clock.now_us()`` captured now.
    :param unix_now: ``time.time()`` captured at the same instant as sendspin_now_us.
    :return: The Sendspin-clock instant (µs) that coincides with ``unix_ms``.
    """
    future_s = unix_ms / 1000 - unix_now
    return sendspin_now_us + round(future_s * 1_000_000)


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
        # Shared-PTP decision the bridge's current cli process was spawned with
        # (None when no process carries one). A warm reuse keeps the flag baked
        # into that process, so the group matches what is really running instead
        # of deciding again per stream.
        self._use_shared_ptp: bool | None = None
        # Frames handed to the CLI since the start anchor. Byte 0 of that stream is
        # audible at _drop_until_us and the device plays on at a fixed rate, so this
        # counter is also the write cursor's position on the Sendspin timeline.
        self._queued_frames: int = 0
        self._drop_until_us: int = 0
        # Playout shift (seconds) already folded into the anchor from the binary's
        # own mid-stream re-anchors. The binary zeroes its running total on every
        # START, so this baseline is cleared wherever the anchor is (re)established.
        self._applied_shift_seconds: float = 0.0
        # Whether a folded playout shift is still being trimmed off the content.
        # Distinguishes the realignment that correction produces from Sendspin
        # timeline drift, which is the only one worth reporting.
        self._absorbing_shift: bool = False
        # Unix-epoch ms at which byte 0 written to the CLI is audible (0 = unset).
        # Used to pace writes so the device buffer stays bounded (see _cli_writer).
        self._start_unix_ms: int = 0
        self._write_queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._writer_task: asyncio.Task[None] | None = None
        # The start that currently owns the bridge. Every step of the start path
        # compares itself against this to tell whether it may still touch the
        # bridge, so it is published before that task runs (see _on_audio_chunk).
        self._airplay_stream_start_task: asyncio.Task[None] | None = None
        self._airplay_stream_ready = asyncio.Event()
        # Whether the current stream has been anchored with its first START.
        self._started = False
        # Whether _drop_until_us reflects the instant the binary acked. Until it
        # does, arriving chunks are held instead of placed.
        self._anchor_settled = False
        # Chunks delivered while the anchor is still being negotiated. Held as
        # chunks rather than queued bytes because _align_chunk places bytes
        # relative to _drop_until_us, which the ack is about to move.
        self._held_chunks: deque[AudioChunk] = deque()
        # Running total of the held chunks' duration, kept alongside the deque so
        # the cap does not re-sum the whole backlog on every chunk.
        self._held_us: int = 0
        self._cleanup_task: asyncio.Task[None] | None = None
        # Monotonic instant of the last recovery from a lost transport (None when
        # none happened yet), used to tell a one-off loss from a flapping device.
        self._last_transport_recovery: float | None = None
        self._rejoin_task: asyncio.Task[None] | None = None
        # Monotonic instant the bridge was last put back into the group a give-up
        # took it out of (None when never). Giving up again this soon after means
        # the speaker did not hold its place, so it is left out instead.
        self._last_rejoin: float | None = None
        # Timer id for the deferred teardown on stream end. A seek/next ends the
        # sendspin stream and immediately starts a new one; deferring the CLI
        # teardown across that gap lets the next stream reuse the connected
        # binary via flush-refill instead of a cold reconnect.
        self._teardown_timer_id = f"bridge_teardown_{airplay_player.player_id}"
        # Guards the bridge's own registration teardown. Held across the stream
        # teardown, which takes the player's stream_spawn_lock, so a start path
        # must never be wrapped in this one: that would invert the two.
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

    @property
    def sendspin_group(self) -> SendspinGroup | None:
        """Return the Sendspin group this bridge belongs to, or None when unregistered."""
        return self._sendspin_client.group if self._sendspin_client else None

    @property
    def active_shared_ptp(self) -> bool | None:
        """
        Return the shared-PTP decision this bridge's live cli process runs with.

        None when the bridge has no process carrying one, so a group member
        resolving its own start never matches a decision nothing is running. A
        process kept for a warm reuse still answers: the next stream rides it
        with the flag it was spawned with.
        """
        if self._is_streaming or self._stream_is_warm_eligible():
            return self._use_shared_ptp
        return None

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
                on_explicit_stop=self._on_bridge_explicit_stop,
                initial_volume=self.airplay_player.volume_level or 25,
                initial_muted=bool(self.airplay_player.volume_muted),
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
        self._cancel_rejoin()
        async with self._lock:
            await self._stop_streaming()
            if self._sendspin_client and self._bridge_client_id:
                await self.sendspin_server.remove_client(self._bridge_client_id)
                self._sendspin_client = None
                self._bridge_role = None

        self.logger.debug("Sendspin bridge stopped for %s", self.airplay_player.display_name)

    def sync_role_volume_state(self) -> None:
        """
        Adopt the AirPlay player's volume and mute into the bridge role.

        The role is what the visible Sendspin player reports, so without this a
        change made on the AirPlay side -- the device's own volume feedback, or a
        mute released when a stream starts -- would leave the parent showing a
        level and mute state the speaker is not at.
        """
        if not self._bridge_role:
            return
        # Both sides hold the device-scale level, so a value read back here compares
        # equal to the one the role just sent instead of bouncing between the two.
        self._bridge_role.update_player_state(
            volume=self.airplay_player.volume_level,
            muted=bool(self.airplay_player.volume_muted),
        )

    def stop_streaming(self) -> None:
        """
        Stop feeding the speaker because the user asked for it.

        Unlike a Sendspin stream ending -- which keeps the connected binary warm
        for a moment so the next track can ride it -- an explicit stop tears the
        transport down straight away, so the device stops instead of playing out
        the seconds it still has buffered. The bridge leaves the session with it,
        without lining up a return, so the visible player stops reporting
        playback the speaker is no longer producing.
        """
        self.mass.cancel_timer(self._teardown_timer_id)
        self._cancel_rejoin()
        # Nothing is being fed and no transport is left playing one out, so there
        # is no playback here for a stop to end (and no session to leave over it).
        leaves_session = self._is_streaming or self._airplay_stream is not None
        self._release_streaming()
        if leaves_session:
            self.mass.create_task(self._leave_sendspin_session(rejoin=False))

    def _refresh_bridge_timing(self) -> None:
        """
        Push the AirPlay startup lead and ongoing buffer to the bridge role.

        The lead is how far ahead of the audible instant Sendspin schedules the
        first chunk; a kept, already-connected CLI needs less of it than a cold
        one.
        """
        if self._bridge_role is None:
            return
        lead_ms = (
            BRIDGE_WARM_START_LEAD_MS
            if self._can_reuse_stream_warm()
            else BRIDGE_COLD_START_LEAD_MS
        )
        self._bridge_role.set_timing(
            required_lead_time_ms=lead_ms,
            min_buffer_ms=BRIDGE_MIN_BUFFER_MS,
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

    def _can_reuse_stream_warm(self) -> bool:
        """
        Return whether the next Sendspin stream can ride the current cli process.

        On top of the process being reusable, the shared-PTP flag it was spawned
        with has to still be the one its group runs on: a bridge regrouped onto
        the other timing source needs a fresh process to change that flag.
        """
        if not self._stream_is_warm_eligible():
            return False
        if self._use_shared_ptp is None:
            # A legacy RAOP process carries no timing decision, so it cannot be
            # on the wrong one and respawning it would change nothing.
            return True
        group_decision = self.provider.bridge_manager.group_shared_ptp(self)
        return group_decision is None or group_decision == self._use_shared_ptp

    def _resolve_shared_ptp(self) -> bool | None:
        """
        Decide whether the cli process about to be spawned attaches to the shared PTP clock.

        None for a player that does not stream native AirPlay 2, whose process
        carries no timing decision for the group to match.
        """
        if self.airplay_player.protocol != StreamingProtocol.AIRPLAY2:
            return None
        return self.provider.bridge_manager.resolve_shared_ptp(self)

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
        # Each Sendspin stream gets its own recovery budget: a loss on the
        # previous one says nothing about the device's health on this one. This
        # is settled before anything can return early, so no stream ever
        # inherits the previous one's verdict.
        self._last_transport_recovery = None
        # Joining a session by any means supersedes a pending re-join.
        self._cancel_rejoin()
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
        keep_stream = self._can_reuse_stream_warm()
        old_stream = None if keep_stream else self._airplay_stream
        old_writer_task = self._writer_task
        old_stream_start_task = self._airplay_stream_start_task

        if not keep_stream:
            if old_stream is not None:
                old_stream.superseded = True
            self._airplay_stream = None
            # The player's reference is left in place either way: a native
            # session is the start task's to stop before it spawns, and the
            # bridge's own old stream stays published until the cleanup below
            # actually has it off the receiver.
            self._use_shared_ptp = None
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
        self._queued_frames = 0
        self._drop_until_us = 0
        self._start_unix_ms = 0
        self._started = keep_stream
        self._anchor_settled = False
        self._held_chunks.clear()
        self._held_us = 0

    def _on_bridge_stream_start(self) -> None:
        """
        Start the writer task when the PushStream notifies us the stream has started.

        Called via the BridgePlayerRole.on_stream_start callback when the
        PushStream begins delivering audio chunks.
        """
        self._restart_transport()

    def _restart_transport(self) -> None:
        """
        Release the current transport and arm a fresh one for the next chunk.

        Leaves the bridge unanchored with a running writer, so the next chunk
        Sendspin delivers starts a transport and anchors it where that chunk
        sits on the group's timeline. A warm-eligible stream is the exception:
        it is kept so the new media rides its flush-refill instead.
        """
        # A teardown deferred by an earlier stream end would otherwise fire into
        # the transport armed here, so it is dropped along with the old one.
        self.mass.cancel_timer(self._teardown_timer_id)
        # The stream might not yet be cleaned up completely (on rapid skips for example).
        # A warm-eligible stream is kept out of the snapshot so it survives into the
        # new stream instead of being torn down (see _can_reuse_stream_warm).
        keep_stream = self._can_reuse_stream_warm()
        old_stream = None if keep_stream else self._airplay_stream
        old_writer_task = self._writer_task
        old_stream_start_task = self._airplay_stream_start_task

        if not keep_stream:
            if old_stream is not None:
                old_stream.superseded = True
            self._airplay_stream = None
            # Left published for the start task or the cleanup to clear, exactly
            # as in _on_stream_start.
            self._use_shared_ptp = None
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
        self._queued_frames = 0
        self._drop_until_us = 0
        self._start_unix_ms = 0
        self._started = keep_stream
        self._anchor_settled = False
        self._held_chunks.clear()
        self._held_us = 0
        # Drain stale audio data from the previous stream
        while not self._write_queue.empty():
            self._write_queue.get_nowait()
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
            # same AirPlay device simultaneously. Awaited BEFORE the spawn lock
            # below and never under it: the cleanup takes that same lock, so the
            # two orders cannot be swapped.
            cleanup = self._cleanup_task
            if cleanup and not cleanup.done():
                await cleanup

            if asyncio.current_task() is not self._airplay_stream_start_task:
                # A newer stream start owns the bridge and everything it holds:
                # neither the kept stream nor the receiver is this task's to touch.
                return

            # The native start path takes the same lock around its own displace-
            # connect-publish sequence, so the two can no longer interleave into
            # two cli processes on one receiver.
            async with self.airplay_player.stream_spawn_lock:
                if asyncio.current_task() is not self._airplay_stream_start_task:
                    # A newer stream start took the bridge over while this one
                    # waited for the spawn lock.
                    return

                # A player stream the bridge does not own is a live native session
                # being displaced by this start. Stop it before the new process
                # pairs with the receiver: two cli processes on one receiver reset
                # each other's RTSP channel and both sessions die.
                displaced_stream = self.airplay_player.stream
                if displaced_stream is not None and displaced_stream is not self._airplay_stream:
                    await self._stop_displaced_stream(displaced_stream)
                    if self.airplay_player.stream is displaced_stream:
                        self.airplay_player.stream = None
                    if asyncio.current_task() is not self._airplay_stream_start_task:
                        # A newer stream start took the bridge over while the
                        # displaced session was being stopped.
                        return

                # A kept, still-connected stream (see _stream_is_warm_eligible,
                # checked by the stream-start callbacks) absorbs the new media via a
                # flush-refill on the SAME cli stdin instead of a cold reconnect.
                kept_stream = self._airplay_stream
                if kept_stream is not None:
                    if await self._start_warm_stream(kept_stream):
                        return
                    if asyncio.current_task() is not self._airplay_stream_start_task:
                        # Not a failure: a newer stream start owns the bridge and the
                        # kept stream, so neither is this task's to tear down.
                        return
                    # Warm handover failed - tear down the kept stream and fall through
                    # to the cold path below, which spawns a fresh process.
                    with suppress(Exception):
                        await kept_stream.stop(force=True)
                    self._airplay_stream = None
                    if self.airplay_player.stream is kept_stream:
                        self.airplay_player.stream = None
                    self._use_shared_ptp = None

                # On a rapid skip, _on_bridge_stream_start snapshots self._airplay_stream
                # for cleanup. If we assigned it earlier, the new stream would be missed
                # and leaked. Only publish once connect() succeeds and this task is current.
                await self._start_cold_stream(AirPlayStream(self.airplay_player))
        except Exception as err:
            self.logger.error(
                "Failed to start AirPlay protocol for %s: %s",
                self.airplay_player.display_name,
                err,
            )
            if asyncio.current_task() is not self._airplay_stream_start_task:
                # A newer stream start owns the bridge: its backlog, writer gate
                # and stream must survive this task's failure untouched.
                self.logger.debug(
                    "Superseded AirPlay start for %s failed, leaving the newer stream alone",
                    self.airplay_player.display_name,
                )
                return
            self._abandon_streaming()

    async def _stop_displaced_stream(self, stream: AirPlayStream) -> None:
        """
        Release the speaker from a session the bridge is taking it away from.

        Lets a failure to release the transport propagate, so the caller never
        adds a second cli process to a receiver that is still serving one.

        :param stream: The stream currently published on the AirPlay player.
        """
        # Only the transport and its session are this method's: native group membership
        # belongs to the player controller, which releases it in the grouping command that
        # hands the speaker over.
        if stream.running:
            # An idle player keeps its last stream published, so only a live one
            # is worth reporting as displaced.
            self.logger.debug(
                "Stopping the session already running on %s before bridging it to Sendspin",
                self.airplay_player.display_name,
            )
        await stream.stop(force=True)
        if stream.session is None:
            return
        try:
            # Bookkeeping only, the transport is already gone: this releases the
            # speaker from the session's client list and ends a session left
            # without clients, so its audio source is not left running.
            await stream.session.remove_client(
                self.airplay_player, reason="displaced by sendspin bridge"
            )
        except Exception as err:
            self.logger.warning(
                "Could not release %s from its previous session: %s",
                self.airplay_player.display_name,
                err,
            )

    async def _start_cold_stream(self, stream: AirPlayStream) -> bool:
        """
        Connect a fresh bridge transport and anchor its first START.

        :param stream: New AirPlay stream to start.
        :return: True when the stream is anchored, False when superseded.
        """
        if asyncio.current_task() is not self._airplay_stream_start_task:
            # A newer stream start owns the bridge. The stream handed in is not
            # connected yet, so there is nothing to tear down: returning here
            # keeps a second session off the receiver and leaves the timing
            # decision the newer start recorded alone.
            return False
        try:
            # Resolving and recording the decision never awaits, so two bridges
            # starting together cannot both find their group still undecided.
            self._use_shared_ptp = self._resolve_shared_ptp()
            # A cold connect is the only path that can still act on the latch, so the
            # release belongs here: a kept process never reaches this point.
            self.airplay_player.release_foreign_mute_latch()
            await stream.connect(self._use_shared_ptp)
            await stream.wait_for_connection()
            if asyncio.current_task() is not self._airplay_stream_start_task:
                await stream.stop(force=True)
                return False
            self._airplay_stream = stream
            self.airplay_player.stream = stream
            if await self._anchor_stream(stream, warm=False):
                return True
            # A superseded cold stream is this task's to tear down: it was
            # connected here and no other start owns it (unlike the warm path,
            # where the kept stream belongs to the newer start). Only unpublish
            # what still points at it, so a newer owner's assignment survives.
            with suppress(Exception):
                await stream.stop(force=True)
            if self._airplay_stream is stream:
                self._airplay_stream = None
            if self.airplay_player.stream is stream:
                self.airplay_player.stream = None
            return False
        except BaseException:
            with suppress(Exception):
                await stream.stop(force=True)
            raise

    async def _start_warm_stream(self, stream: AirPlayStream) -> bool:
        """
        Flush a kept, still-connected stream and resume it on the new track.

        The stream is flushed in place (the binary's input ring and stdin
        drained) while its connection stays alive, then the new PCM is fed into
        the SAME cli stdin and re-anchored with a single START. Returns True once
        resumed; any failure returns False so the caller falls back to a cold
        restart.

        :param stream: The kept AirPlayStream to flush and resume.
        """
        try:
            if not await stream.flush():
                return False
            if asyncio.current_task() is not self._airplay_stream_start_task:
                # A newer stream start owns the bridge; leave the stream to it.
                return False
            return await self._anchor_stream(stream, warm=True)
        except asyncio.CancelledError:
            raise
        except Exception as err:
            self.logger.warning(
                "Warm handover failed for %s (%r), falling back to a cold restart",
                self.airplay_player.display_name,
                err,
            )
            return False

    async def _anchor_stream(self, stream: AirPlayStream, *, warm: bool) -> bool:
        """
        Anchor a connected stream on the Sendspin timeline and release the writer.

        Commands the START, then maps the bridge's content onto the instant the
        binary acked rather than the one that was asked for, so the device plays
        the same audio at the same time as the rest of the Sendspin group. The
        writer stays blocked until that mapping is in place.

        :param stream: The connected AirPlay stream to anchor.
        :param warm: True when re-anchoring a kept, still-connected stream, whose
            queued audio the new anchor has to clear.
        :return: True when the stream is anchored, False when superseded.
        """
        readiness, ready_at_unix_ms = await stream.wait_clock_ready(
            timeout=AIRPLAY_CLOCK_READY_TIMEOUT_MS / 1000
        )
        if readiness is ClockReadiness.STALLED:
            # Anchored anyway, like a group start and unlike a late joiner: a
            # joiner is dropped because the session plays on without it, while
            # here it would stop the speaker - and a stall is not evidence
            # enough for that. The binary reports it as a diagnosis rather than
            # a verdict (a receiver that begins probing late still comes good)
            # and re-arms that reporting per cycle, so a warm re-anchor is
            # reading the cycle before it.
            self.logger.warning(
                "%s never answered the server's PTP clock, so this stream will be silent "
                "until it does; anchoring anyway",
                self.airplay_player.display_name,
            )
        sendspin_now_us = self.sendspin_server.clock.now_us()
        unix_now = time.time()
        now_ms = int(unix_now * 1000)
        # The instant Sendspin wants the first chunk we hold to be audible; the
        # anchor never precedes it, so nothing Sendspin scheduled is thrown away.
        first_chunk_unix_ms = sendspin_audible_instant_to_unix_ms(
            self._drop_until_us, sendspin_now_us, unix_now
        )
        anchor_ms = max(now_ms + AIRPLAY_LATE_JOIN_MIN_HEADROOM_MS, first_chunk_unix_ms)
        if ready_at_unix_ms:
            anchor_ms = max(anchor_ms, ready_at_unix_ms + AIRPLAY_CLOCK_READY_LEAD_MS)
        sync_adjust = self.airplay_player.config.get_value(CONF_SYNC_ADJUST, 0)
        adjust_ms = sync_adjust if isinstance(sync_adjust, int) else 0
        if warm:
            # A splice-timeline receiver honours the commanded instant only when
            # it lands beyond the audio still queued behind the flushed head. A
            # NEGATIVE sync_adjust moves the commanded instant earlier, eating
            # into the lead, so it has to be added back to the requirement.
            if stream.warm_lead_ms > 0:
                anchor_ms = max(
                    anchor_ms,
                    now_ms
                    + stream.warm_lead_ms
                    - min(0, adjust_ms)
                    + AIRPLAY_SPLICE_LEAD_MARGIN_MS,
                )
            if stream.flushed_head_unix_ms > 0:
                anchor_ms = max(
                    anchor_ms,
                    stream.flushed_head_unix_ms - adjust_ms + AIRPLAY_SPLICE_LEAD_MARGIN_MS,
                )
        commanded_ms = anchor_ms + adjust_ms

        # The writer stays gated until this anchor is settled, so the bridge has
        # written nothing for this stream and anything the binary reports pending
        # was left behind by an earlier one. The START would anchor that as its
        # first sample and leave every chunk after it late by the same amount,
        # which no later realignment can see: the cursor counts only what the
        # bridge queued itself. Reported rather than acted on, because the reading
        # is best effort -- it depends on the status line having been parsed by
        # now -- so falling back to a cold restart on it would trade a fault that
        # should no longer happen for one that would.
        if stream.audio_pending_ms:
            self.logger.warning(
                "%s still had %d ms of earlier audio pending when its anchor was "
                "commanded; its content will play that late",
                self.airplay_player.display_name,
                stream.audio_pending_ms,
            )

        # Always a join: the Sendspin timeline is the group's, never the bridge's
        # to set, so the binary reports the instant it really scheduled - holding
        # its ack until the receiver clock verification resolves whenever that
        # verification is armed.
        acked_adjusted = await stream.start(commanded_ms, join=True)

        # The ack can be held for seconds, so re-check ownership before touching
        # any state a newer stream start may already own.
        if (
            asyncio.current_task() is not self._airplay_stream_start_task
            or self._airplay_stream is not stream
        ):
            return False
        # The command carries the device's sync_adjust; the group timeline does not.
        acked_timeline_ms = acked_adjusted - adjust_ms
        sendspin_now_us = self.sendspin_server.clock.now_us()
        unix_now = time.time()
        anchor_us = unix_ms_to_sendspin_audible_instant(
            acked_timeline_ms, sendspin_now_us, unix_now
        )
        skipped_us = anchor_us - self._drop_until_us
        self._start_unix_ms = acked_adjusted
        self._drop_until_us = anchor_us
        self._queued_frames = 0
        # A START re-anchors the binary's playout from scratch and zeroes the
        # total it reports, so the bridge's baseline starts over with it.
        self._applied_shift_seconds = 0.0
        self._absorbing_shift = False
        self._anchor_settled = True
        self._started = True
        # Replay what arrived while the anchor was being settled before yielding
        # to the loop, so the backlog stays ahead of whatever _on_audio_chunk
        # delivers next.
        held = list(self._held_chunks)
        self._held_chunks.clear()
        self._held_us = 0
        for chunk in held:
            if chunk.timestamp_us + chunk.duration_us <= self._drop_until_us:
                continue
            self._align_chunk(chunk)
        self._airplay_stream_ready.set()

        # The content shift is signed: a later anchor skips content, an earlier
        # one (the binary is free to ack either way) pads silence up to it.
        self.logger.info(
            "Bridge protocol %s for %s (start_unix_ms=%d, acked %+d ms off the commanded "
            "instant, content shifted %+.0f ms)",
            "resumed" if warm else "started",
            self.airplay_player.display_name,
            acked_adjusted,
            acked_adjusted - commanded_ms,
            skipped_us / 1000,
        )
        if skipped_us > 500_000:
            self.logger.warning(
                "AirPlay start for %s dropped %.0fms of audio: Sendspin scheduled the first "
                "chunk %.0fms ahead but the device could not be audible before %.0fms",
                self.airplay_player.display_name,
                skipped_us / 1000,
                first_chunk_unix_ms - now_ms,
                acked_timeline_ms - now_ms,
            )
        return True

    def _on_volume_change(self, volume: int) -> None:
        """Forward volume changes to the AirPlay player."""
        # The level is already on the AirPlay device's scale, so it is applied as-is.
        # Where the role is the parent's volume control - a parent with no volume of
        # its own, while the bridge streams - a volume from Music Assistant arrives
        # here having passed the controller and been scaled into the parent's min/max
        # range, so routing it back through cmd_volume_set would scale it a second
        # time and return over the same route, settling the speaker below the level
        # that was asked for. A volume a Sendspin controller set never passed the
        # parent at all: that is an externally changed volume like any other, clamped
        # to the limits on the parent's state update.
        self.mass.create_task(self.airplay_player.volume_set(volume))

    def _on_mute_change(self, muted: bool) -> None:
        """Forward mute changes to the AirPlay player."""
        # Applied here for the same reason as the volume above. A mute has no scale
        # to get wrong, but it would come straight back through the role, and an
        # unchanged mute is not a command the controller drops.
        self.mass.create_task(self.airplay_player.volume_mute(muted))

    def _on_bridge_stream_end(self) -> None:
        """
        Handle the sendspin stream ending: defer the CLI teardown briefly.

        A stream end that is immediately followed by a new stream (a seek or
        next-track on setups that end the stream for those) would force every
        such switch through a cold reconnect if the CLI were torn down here;
        instead the connected binary is kept alive for a short grace window so
        the next stream can reuse it via flush-refill. If no new stream arrives
        within the window, the deferred cleanup kills the CLI so AirPlay stops
        instead of draining its buffer. A stop the user asked for skips the
        window through _on_bridge_explicit_stop.
        """
        self._is_streaming = False
        self._queued_frames = 0
        if self._transport_state_empty():
            # A stream end with no transport behind it (an explicit stop already
            # tore it down, or no chunk ever started one): nothing to keep warm
            # and nothing to defer.
            self.logger.debug(
                "Sendspin stream ended for %s with no transport to keep warm",
                self.airplay_player.display_name,
            )
            return
        self.logger.debug(
            "Sendspin stream ended for %s; keeping the CLI warm for %.0fs",
            self.airplay_player.display_name,
            BRIDGE_WARM_GRACE_SECONDS,
        )
        self.mass.call_later(
            BRIDGE_WARM_GRACE_SECONDS, self._deferred_cleanup, task_id=self._teardown_timer_id
        )

    def _on_bridge_explicit_stop(self) -> None:
        """
        Tear the transport down at once for a stop the user asked for.

        The stream end preceding this callback armed the warm grace window,
        which exists for a next track to ride the connected binary. Nothing
        follows an explicit stop, and the device holds seconds of buffered
        audio, so waiting the window out would play out what the user asked to
        end. The bridge stays registered in its group, so a later play includes
        this speaker again.
        """
        self.mass.cancel_timer(self._teardown_timer_id)
        if self._is_streaming:
            # A new stream already took the bridge over; the transport belongs to it now.
            return
        if self._transport_state_empty():
            return
        self.logger.debug(
            "Explicit stop for %s: tearing the transport down without the warm grace",
            self.airplay_player.display_name,
        )
        self._schedule_cleanup()

    def _transport_state_empty(self) -> bool:
        """Return whether the bridge holds no transport, writer or pending start."""
        return (
            self._airplay_stream is None
            and self._writer_task is None
            and self._airplay_stream_start_task is None
        )

    def _deferred_cleanup(self) -> None:
        """
        Run the teardown a stream end deferred, unless a new stream took the bridge over.

        cancel_timer cannot recall a handle that already fired, so a stream
        arriving right at the end of the grace window leaves this call on its
        way. Tearing down here would cancel that stream's writer and drain its
        queue, leaving the speaker silent for its whole track.
        """
        if self._is_streaming:
            return
        self._schedule_cleanup()

    def _schedule_cleanup(self) -> None:
        """
        Detach the current stream resources and tear them down in the background.

        The detach is synchronous, so the teardown works on what the bridge held
        at this instant instead of reading the fields back across its awaits, by
        which time a new stream may own them.
        """
        stream, writer_task, start_task = self._detach_stream_state()
        prev_cleanup = self._cleanup_task
        self._cleanup_task = self.mass.create_task(
            self._cleanup_old_stream(stream, writer_task, start_task, prev_cleanup)
        )

    def _abandon_streaming(self) -> None:
        """
        Give up on this stream: stop accepting chunks and tear the transport down.

        Sendspin reports playback from the group's own state, so a bridge that
        stops feeding its speaker would otherwise hold the visible player on
        PLAYING while nothing is audible. The bridge leaves the session instead,
        so that silence reaches the player.
        """
        self._release_streaming()
        self.mass.create_task(self._leave_sendspin_session())

    def _release_streaming(self) -> None:
        """Stop accepting chunks and hand the current transport to the cleanup path."""
        self._is_streaming = False
        self._use_shared_ptp = None
        self._held_chunks.clear()
        self._held_us = 0
        self._schedule_cleanup()
        # unblock a writer still waiting for a stream that will never be ready
        self._airplay_stream_ready.set()

    async def _leave_sendspin_session(self, *, rejoin: bool = True) -> None:
        """
        Take the bridge out of Sendspin playback and line up its return.

        A shared group plays on without this speaker, and the bridge is given a
        bounded attempt to re-join it so a device that was only briefly away
        comes back on its own. A solo group has nothing to re-join: leaving is
        what stops it. Either way the client stays registered, so the player
        survives to be grouped again.

        :param rejoin: Whether to line up a return to the group the bridge left.
            False for a stop the user asked for, which is not a failure to
            recover the speaker from.
        """
        if not (client := self._sendspin_client):
            return
        # Captured before leaving, which is what moves the client to a solo group.
        group = client.group
        try:
            previous_group_id = await client.quiesce_to_solo_stopped()
        except Exception as err:
            self.logger.warning(
                "Could not take %s out of its Sendspin session: %s",
                self.airplay_player.display_name,
                err,
            )
            return
        if previous_group_id is None:
            return
        # Whichever way this give-up goes, an older schedule must not outlive it.
        self._cancel_rejoin()
        if not rejoin:
            return
        last_rejoin = self._last_rejoin
        if (
            last_rejoin is not None
            and time.monotonic() - last_rejoin < BRIDGE_TRANSPORT_RECOVERY_GUARD_SECONDS
        ):
            self.logger.warning(
                "%s gave up again within %.0fs of being re-joined, leaving it out of the group",
                self.airplay_player.display_name,
                BRIDGE_TRANSPORT_RECOVERY_GUARD_SECONDS,
            )
            return
        self._rejoin_task = self.mass.create_task(self._rejoin_attempts(group))

    async def _rejoin_attempts(self, group: SendspinGroup) -> None:
        """
        Put the bridge back into the group a give-up took it out of.

        :param group: The Sendspin group the bridge left.
        """
        max_attempts = len(BRIDGE_REJOIN_ATTEMPT_DELAYS)
        for attempt, delay in enumerate(BRIDGE_REJOIN_ATTEMPT_DELAYS, start=1):
            await asyncio.sleep(delay)
            if not (client := self._sendspin_client):
                return
            if len(client.group.clients) > 1 or client.group.has_active_stream:
                # given a group or a stream of its own meanwhile: re-joining
                # would take the speaker away from whatever it was just given,
                # and adding it elsewhere stops the session it is playing
                self.logger.debug(
                    "Not re-joining %s: it is in use again already",
                    self.airplay_player.display_name,
                )
                return
            if self.airplay_player.stream is not None and not self.owns_airplay_stream:
                # the speaker is streaming natively: the re-join would restart
                # the transport and tear down a session that is not the bridge's
                self.logger.debug(
                    "Not re-joining %s: it is streaming outside the bridge",
                    self.airplay_player.display_name,
                )
                return
            if not self.airplay_player.available:
                # a speaker that rebooted is missing from discovery for a while
                # after it starts answering again, so this is worth another go
                self.logger.debug(
                    "Not re-joining %s yet: the speaker is offline",
                    self.airplay_player.display_name,
                )
                continue
            if not group.clients:
                self.logger.debug(
                    "Not re-joining %s: the group it left no longer exists",
                    self.airplay_player.display_name,
                )
                return
            try:
                await group.add_client(client)
            except Exception as err:
                self.logger.warning(
                    "Could not re-join %s to its Sendspin group (attempt %d/%d): %s",
                    self.airplay_player.display_name,
                    attempt,
                    max_attempts,
                    err,
                )
                continue
            # Stamped where the speaker actually rejoins, not where the attempt
            # was scheduled, so the guard holds whatever the delays above are.
            self._last_rejoin = time.monotonic()
            self.logger.info("Re-joined %s to its Sendspin group", self.airplay_player.display_name)
            return
        self.logger.warning(
            "Giving up on re-joining %s to its Sendspin group after %d attempt(s); "
            "the player stays idle",
            self.airplay_player.display_name,
            max_attempts,
        )

    def _cancel_rejoin(self) -> None:
        """Drop any pending attempt to re-join the Sendspin group."""
        rejoin_task = self._rejoin_task
        # never self-cancel: a re-join announces itself through the same
        # stream-start path that clears stale schedules. The handle also
        # survives such a call, so a later user action can still cancel the
        # retry loop between attempts.
        if rejoin_task is None or rejoin_task is asyncio.current_task():
            return
        self._rejoin_task = None
        if not rejoin_task.done():
            rejoin_task.cancel()

    async def _cleanup_old_stream(
        self,
        stream: AirPlayStream | None,
        writer_task: asyncio.Task[None] | None,
        stream_start_task: asyncio.Task[None] | None,
        prev_cleanup: asyncio.Task[None] | None = None,
    ) -> None:
        """
        Clean up captured resources from a previous stream.

        Leaves the player idle at position 0 unless a newer stream owns it.

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

        # Cancelled before the spawn lock is taken: a start task holding it only
        # lets go once it unwinds, so waiting for the lock first would leave
        # nobody to ask it to.
        if stream_start_task and not stream_start_task.done():
            stream_start_task.cancel()
        if writer_task and not writer_task.done():
            writer_task.cancel()
        # The player still points at this stream: that reference is what tells a
        # start there is a process to displace, so it is only dropped below,
        # together with the release itself. Held so no start can land between
        # the two and read the speaker as free.
        async with self.airplay_player.stream_spawn_lock:
            if stream_start_task:
                with suppress(asyncio.CancelledError, Exception):
                    await stream_start_task
            if writer_task:
                with suppress(asyncio.CancelledError, Exception):
                    await writer_task
            if stream:
                # Only unpublish what this bridge put there: a deferred teardown
                # can fire after the native path already replaced the player's
                # stream, and dropping that reference would strand a session the
                # bridge never owned.
                # Cleared ahead of the release rather than after it so stop()
                # reports through the reset below instead of its own, which the
                # clear would drop anyway. Both sit under the lock, so no start
                # can read the speaker as free in between.
                if self.airplay_player.stream is stream:
                    self.airplay_player.stream = None
                with suppress(Exception):
                    await stream.stop(force=True)
                # Restore the IDLE reset that stop() dropped over the clear above
                if self.airplay_player.stream is None:
                    self.airplay_player.set_state_from_stream(
                        state=PlaybackState.IDLE, elapsed_time=0
                    )

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
                self._abandon_streaming()
                return

        if self._transport_lost() and not self._recover_transport():
            return

        if self._airplay_stream_start_task is None:
            # Provisionally anchor byte 0 to the instant Sendspin scheduled for the
            # first chunk it actually delivered, which keeps the bridge on the group's
            # shared timeline. That instant is at least the bridge lead ahead of now,
            # because that is what _refresh_bridge_timing reports as
            # required_lead_time_ms:
            #   * fresh track  -> the first chunk IS file position 0, so an anchor on
            #     its own scheduled instant has no audio ahead of it to discard and
            #     the intro is kept;
            #   * late join     -> the first chunk is the catch-up target, i.e. the
            #     group's current playback position, so the joiner lands in sync.
            # _anchor_stream re-bases this onto the instant the binary acks.
            self._drop_until_us = chunk.timestamp_us
            # Started on the next loop iteration so the handle below is published
            # first: the start path compares itself against that handle to tell
            # whether it still owns the bridge, and an eager start would run its
            # first checks while the handle is still unset.
            self._airplay_stream_start_task = self.mass.create_task(
                self._start_protocol_from_chunk(), eager_start=False
            )

        if not self._anchor_settled:
            # The binary has not acked its anchor yet, and that ack moves
            # _drop_until_us. Hold the chunk instead of placing it against a
            # reference that is about to change.
            self._hold_chunk(chunk)
            return

        self._absorb_playout_shift()

        # Drop chunks that end entirely before the target start time.
        chunk_end_us = chunk.timestamp_us + chunk.duration_us
        if self._drop_until_us and chunk_end_us <= self._drop_until_us:
            return

        self._align_chunk(chunk)

    def _absorb_playout_shift(self) -> None:
        """
        Fold the binary's own mid-stream re-anchors into the bridge's timeline mapping.

        cliairplay re-anchors its playout later when it runs out of PCM on stdin,
        which makes every byte it was handed audible that much later than the
        anchor promised. Moving the anchor by the same amount keeps the content
        the bridge feeds on the group's timeline instead of leaving the device
        permanently behind the rest of it.
        """
        stream = self._airplay_stream
        if stream is None:
            return
        shift_seconds = stream.cumulative_shift_seconds
        delta_seconds = shift_seconds - self._applied_shift_seconds
        if delta_seconds < 0:
            # The binary zeroes its running total on every START, so a total that
            # went backwards means the stream re-anchored outside _anchor_stream:
            # take the new baseline rather than walking a mapping that START has
            # already replaced, and drop a correction that mapping no longer owes.
            self._applied_shift_seconds = shift_seconds
            self._absorbing_shift = False
            return
        if not delta_seconds:
            return
        self._applied_shift_seconds = shift_seconds
        self._drop_until_us += round(delta_seconds * 1_000_000)
        self._start_unix_ms += round(delta_seconds * 1000)
        self._absorbing_shift = True
        self.logger.warning(
            "%s re-anchored its playout %.0f ms later after running out of audio; "
            "re-aligning its content with the group (%.0f ms in total)",
            self.airplay_player.display_name,
            delta_seconds * 1000,
            shift_seconds * 1000,
        )

    def _transport_lost(self) -> bool:
        """
        Return whether the anchored transport died while the bridge kept feeding it.

        The CLI drops writes without raising once its process is gone, so a dead
        transport is only visible on the stream itself. Only an anchored stream
        with no start in flight can be judged: while a cold start or a warm
        handover runs it owns the transport and reports its own failures. A
        stream the bridge no longer owns is not its to judge either -- the
        native path can stop or replace the player's stream without telling the
        bridge, and recovering from that would put a second cli process on the
        receiver and clobber the native session's stream back.
        """
        start_task = self._airplay_stream_start_task
        if start_task is None or not start_task.done():
            return False
        stream = self._airplay_stream
        if stream is None or not self._started or not self.owns_airplay_stream:
            return False
        # The binary also ends its stderr loop on a clean end of stream (an eof
        # or its idle cap), which stops the stream without the transport ever
        # having been lost; restarting one of those cold spawns a process for
        # audio that is already over.
        return not stream.running and not stream.ended_cleanly

    def _recover_transport(self) -> bool:
        """
        Re-anchor the bridge after a transport loss, unless the device keeps dropping.

        :return: True when a fresh transport is armed for the current chunk,
            False when the bridge gave up and stopped streaming.
        """
        now = time.monotonic()
        last_recovery = self._last_transport_recovery
        if (
            last_recovery is not None
            and now - last_recovery < BRIDGE_TRANSPORT_RECOVERY_GUARD_SECONDS
        ):
            self.logger.error(
                "AirPlay transport for %s died again within %.0fs of the previous recovery, "
                "giving up and taking it out of the Sendspin session",
                self.airplay_player.display_name,
                BRIDGE_TRANSPORT_RECOVERY_GUARD_SECONDS,
            )
            self._abandon_streaming()
            return False
        self._last_transport_recovery = now
        self.logger.warning(
            "AirPlay transport for %s was lost mid-stream, re-joining the Sendspin timeline",
            self.airplay_player.display_name,
        )
        self._restart_transport()
        return True

    def _hold_chunk(self, chunk: AudioChunk) -> None:
        """
        Keep a chunk until the start anchor settles, capping the backlog.

        The backlog is bounded by both its duration and its chunk count; the
        count is what bounds a run of zero-duration chunks.

        :param chunk: The chunk to replay once the anchor is known.
        """
        self._held_chunks.append(chunk)
        self._held_us += chunk.duration_us
        while len(self._held_chunks) > 1 and (
            self._held_us > MAX_HELD_AUDIO_US or len(self._held_chunks) > MAX_HELD_CHUNKS
        ):
            self._held_us -= self._held_chunks.popleft().duration_us

    def _align_chunk(self, chunk: AudioChunk) -> None:
        """
        Queue a chunk at the byte offset its timestamp claims on the CLI stream.

        The device plays the stream at a fixed rate from the anchor, so a chunk
        only stays on the group's clock when it is written at the offset matching
        its timestamp. A hole in the Sendspin timeline is filled with silence and
        an overlapping head is trimmed; without that, the device would run
        permanently ahead of (or behind) the rest of the group by the size of the
        discontinuity.

        :param chunk: The audio chunk to queue.
        """
        bytes_per_frame = BRIDGE_CHANNELS * BRIDGE_BYTES_PER_SAMPLE
        target_frames = round(
            (chunk.timestamp_us - self._drop_until_us) * BRIDGE_SAMPLE_RATE / 1_000_000
        )
        drift_frames = target_frames - self._queued_frames
        if drift_frames >= 0:
            # The cursor caught back up, so any shift folded into the anchor has
            # been worked off and a later realignment is timeline drift again.
            self._absorbing_shift = False
        drift_us = round(drift_frames * 1_000_000 / BRIDGE_SAMPLE_RATE)
        # The first placement after an anchor sits at the gap between the anchor
        # and the chunk by construction, which is not drift; only a cursor that
        # already advanced can have drifted away from the timeline. Trimming off a
        # folded playout shift is a correction the bridge asked for, so it stays
        # quiet as well until the cursor is back on the timeline.
        if self._queued_frames and abs(drift_us) > 1_000 and not self._absorbing_shift:
            self.logger.warning(
                "Realigned %d µs of Sendspin timeline drift for %s",
                drift_us,
                self.airplay_player.display_name,
            )

        data = chunk.data
        if drift_frames < 0:
            trim_bytes = -drift_frames * bytes_per_frame
            if trim_bytes >= len(data):
                # Fully covered by what was already written.
                return
            data = data[trim_bytes:]
        elif drift_frames > 0:
            self._queue_silence(drift_frames)
            self._queued_frames += drift_frames

        self._write_queue.put_nowait(data)
        self._queued_frames += len(data) // bytes_per_frame

    def _queue_silence(self, frames: int) -> None:
        """
        Hand the CLI a run of silence to fill a hole in the Sendspin timeline.

        :param frames: Number of silent frames to queue.
        """
        bytes_per_frame = BRIDGE_CHANNELS * BRIDGE_BYTES_PER_SAMPLE
        whole_blocks, remainder = divmod(frames, PAD_BLOCK_FRAMES)
        for _ in range(whole_blocks):
            self._write_queue.put_nowait(SILENCE_BLOCK)
        if remainder:
            self._write_queue.put_nowait(b"\x00" * (remainder * bytes_per_frame))

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
                if self._writer_task is asyncio.current_task():
                    # Only the writer that still feeds the bridge may give up on
                    # it; one left behind by a slow teardown speaks for a stream
                    # that is already gone.
                    self._abandon_streaming()
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
        stream, writer_task, start_task = self._detach_stream_state()
        await self._cleanup_old_stream(stream, writer_task, start_task)

    def _detach_stream_state(
        self,
    ) -> tuple[AirPlayStream | None, asyncio.Task[None] | None, asyncio.Task[None] | None]:
        """
        Take the live transport off the bridge and reset the stream state around it.

        Runs to completion without awaiting, so what it hands back is no longer
        reachable through the bridge: a teardown spanning several awaits cannot
        reach into a stream that started meanwhile, and a task awaiting that
        teardown is no longer among the handles it cancels.

        :return: The detached stream, writer task and stream start task.
        """
        stream = self._airplay_stream
        writer_task = self._writer_task
        start_task = self._airplay_stream_start_task
        if stream is not None:
            stream.superseded = True
        self._airplay_stream = None
        self._writer_task = None
        self._airplay_stream_start_task = None
        # The player's reference stays until the teardown has the process off the
        # receiver (see _cleanup_old_stream): it is what a start reads to decide
        # there is something to displace, and clearing it here would tell one
        # that the speaker is free while the old process still holds it.
        self._is_streaming = False
        self._use_shared_ptp = None
        self._queued_frames = 0
        self._applied_shift_seconds = 0.0
        self._absorbing_shift = False
        self._airplay_stream_ready.clear()
        self._started = False
        self._anchor_settled = False
        self._held_chunks.clear()
        self._held_us = 0
        while not self._write_queue.empty():
            self._write_queue.get_nowait()
        return stream, writer_task, start_task


class SendspinBridgeManager(SendspinBridgeManagerBase[SendspinAirPlayBridge]):
    """Manages Sendspin bridges for all AirPlay players."""

    def __init__(self, provider: AirPlayProvider) -> None:
        """
        Initialize the bridge manager.

        :param provider: The AirPlay provider owning the bridged players.
        """
        super().__init__(provider)
        self._unsubs.append(
            self.mass.players.subscribe_player_state_update(self._on_player_state_updated)
        )

    def resolve_shared_ptp(self, bridge: SendspinAirPlayBridge) -> bool:
        """
        Return whether a bridge's next cli process attaches to the shared PTP clock.

        One Sendspin group must not mix the two timing sources: a member that
        does not attach times itself off NTP instead, which the binary anchors
        differently from PTP, and the two drift audibly apart. Bridges in a group
        can start minutes apart and keep a warm process across a track change, so
        the decision its group already runs on wins; only a group without one
        asks the daemon.

        The daemon is read as it stands rather than waited for (unlike a native
        sync group, which can afford the wait): a bridge's start lead is fixed
        before this point, so waiting here would push its anchor past the audio
        Sendspin already scheduled. A group starting inside the daemon's spawn
        window therefore degrades to NTP together, for that session.

        :param bridge: The bridge about to spawn a cli process.
        """
        decision = self.group_shared_ptp(bridge)
        if decision is None:
            decision = cast("AirPlayProvider", self.provider).ptp_daemon_ready
        return decision

    def group_shared_ptp(self, bridge: SendspinAirPlayBridge) -> bool | None:
        """
        Return the shared-PTP decision the rest of a bridge's Sendspin group runs on.

        None when no other member of the group has a cli process carrying one,
        which leaves the bridge free to decide for the group itself.

        :param bridge: The bridge asking about its group.
        """
        if not (group := bridge.sendspin_group):
            return None
        for sibling in self._bridges.values():
            if sibling is bridge or sibling.sendspin_group is not group:
                continue
            if (decision := sibling.active_shared_ptp) is not None:
                return decision
        return None

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
            bridge.stop_streaming()
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

    def _on_player_state_updated(
        self, updated_player: Player, changed_values: dict[str, tuple[Any, Any]]
    ) -> None:
        """Feed an AirPlay player's own volume/mute changes back into its bridge role."""
        if not changed_values.keys() & _VOLUME_STATE_FIELDS:
            return
        if bridge := self._bridges.get(updated_player.player_id):
            bridge.sync_role_volume_state()
