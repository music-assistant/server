"""Unified AirPlay/RAOP stream session logic for AirPlay devices."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator, Coroutine
from contextlib import aclosing, suppress
from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import ContentType, PlaybackState
from music_assistant_models.errors import MusicAssistantError, PlayerCommandFailed

from music_assistant.controllers.streams.audio_processing import get_media_session_id
from music_assistant.helpers.ffmpeg import FFMpeg

from .constants import (
    AIRPLAY_ANCHOR_SEAT_LEAD_MS,
    AIRPLAY_CLOCK_READY_LEAD_MS,
    AIRPLAY_CLOCK_READY_TIMEOUT_MS,
    AIRPLAY_COLD_GROUP_START_LEAD_MS,
    AIRPLAY_FEED_START_TIMEOUT,
    AIRPLAY_GROUP_START_LEAD_MS,
    AIRPLAY_LATE_JOIN_MIN_HEADROOM_MS,
    AIRPLAY_LATE_JOIN_RING_MARGIN_SECONDS,
    AIRPLAY_LATE_JOIN_RING_MAX_BYTES,
    AIRPLAY_LATE_JOIN_RING_MIN_SECONDS,
    AIRPLAY_REPLACEMENT_EOF_TIMEOUT,
    AIRPLAY_REPLACEMENT_POLL_INTERVAL,
    AIRPLAY_SILENT_MEMBER_JOIN_POLL_INTERVAL,
    AIRPLAY_SILENT_MEMBER_JOIN_TIMEOUT,
    AIRPLAY_SILENT_MEMBER_REJOIN_DELAY,
    AIRPLAY_SILENT_MEMBER_RESET_WINDOW,
    AIRPLAY_SILENT_MEMBER_TIMEOUT,
    AIRPLAY_SPLICE_LEAD_MARGIN_MS,
    AIRPLAY_STALL_RESYNC_HOLDOFF,
    AIRPLAY_STALL_RESYNC_MAX_HOLDOFF,
    AIRPLAY_STALL_SETTLE_SECONDS,
    AIRPLAY_STALL_SHIFT_TOLERANCE_MS,
    AIRPLAY_START_LEAD_MS,
    ClockReadiness,
    StreamingProtocol,
)
from .helpers import cli_clock_time, get_final_output_format
from .stream import AirPlayStream

if TYPE_CHECKING:
    from music_assistant_models.media_items import AudioFormat

    from music_assistant.models.player import PlayerMedia

    from .player import AirPlayPlayer
    from .provider import AirPlayProvider

# What each readiness outcome means for the join anchor: a projection only moves
# it when it clears the join floor, which otherwise carries the anchor alone, so
# the note says which bound the outcome set. STALLED has no note: the binary only
# reports a stall well after this wait has given up, and a joiner that stalls is
# removed from the session then (see remove_stalled_joiner).
_CLOCK_READINESS_NOTES: dict[ClockReadiness, str] = {
    ClockReadiness.PROJECTED: "usable in {out:.2f}s; anchoring no earlier than that",
    ClockReadiness.NOT_APPLICABLE: "runs on NTP timing, so there is none to wait for; "
    "anchoring on the join floor",
    ClockReadiness.UNREPORTED: "was not reported within {timeout:.1f}s (a slow device, or a "
    "receiver that never answered); anchoring on the join floor",
}
# A locked clock projects an instant that has already passed - the common case,
# since only a cold receiver is still probing - so its note reads back in time.
_CLOCK_LOCKED_NOTE = "became usable {ago:.2f}s ago; anchoring on the join floor"


class AirPlayStreamSession:
    """Stream session (RAOP or AirPlay2) to one or more players."""

    def __init__(
        self,
        airplay_provider: AirPlayProvider,
        sync_clients: list[AirPlayPlayer],
        pcm_format: AudioFormat,
        media: PlayerMedia,
    ) -> None:
        """
        Initialize AirPlayStreamSession.

        :param airplay_provider: The AirPlay provider instance.
        :param sync_clients: List of AirPlay players to stream to.
        :param pcm_format: PCM format of the input stream.
        :param media: Queue media that owns the stream session.
        """
        assert sync_clients
        self.prov = airplay_provider
        self.mass = airplay_provider.mass
        self.pcm_format = pcm_format
        self.media = media
        self.sync_clients = sync_clients
        self._audio_source_task: asyncio.Task[None] | None = None
        self._player_ffmpeg: dict[str, FFMpeg] = {}
        self._lock = asyncio.Lock()
        self._ptp_lock = asyncio.Lock()
        self.start_unix_ms: int = 0
        self.start_time: float = 0.0
        self.seconds_streamed: float = 0
        # Parked in standby: the members stay connected but nothing is fed and
        # their binaries hold until a START, so only a re-anchor (play_media)
        # revives them. It outlives the group that parked it, which is why the
        # session - not the group membership - owns this.
        self.parked: bool = False
        # Timing source for the whole session, decided once in start() and applied
        # identically to every native AirPlay 2 member (and any late joiner) so a
        # sync group can never mix shared-PTP and NTP members.
        self.use_shared_ptp: bool = False
        self._shared_ptp_resolved = False
        self._ptp_degraded_warning_logged = False
        # Raw PCM ring buffer for late joiners. When a late joiner arrives we
        # send this buffer to prime its pipeline so it starts playing quickly
        # instead of waiting for the full pipeline to fill from scratch.
        # It must cover the write-head lead - how far the feed runs ahead of the
        # audible position - because that is exactly the span a joiner's anchor
        # maps into. The lead is not a constant of the protocol: it is the sum
        # of every buffer downstream of this counter (see
        # AIRPLAY_LATE_JOIN_RING_MIN_SECONDS), so it is measured per session and
        # the ring is grown to match.
        self._pcm_buffer = bytearray()
        # Largest write-head lead this session has shown, and the ring size
        # derived from it.
        self._peak_lead_seconds: float = 0.0
        # Raw byte sizes of the PCM actually on the wire. At 24-bit the binary
        # is fed s32le carriers (bit_depth stays 24 for the ALAC encode), so
        # sizes MUST come from the content type: bit_depth-derived sizes are
        # 6 bytes/frame while the wire frames are 8 — slicing or timing the
        # feed on those boundaries cuts mid-sample (loud noise on a late
        # joiner's prime) and inflates the position clock by 4/3.
        bytes_per_sample = {
            ContentType.PCM_S16LE: 2,
            ContentType.PCM_S24LE: 3,
            ContentType.PCM_S32LE: 4,
            ContentType.PCM_F32LE: 4,
        }.get(pcm_format.content_type, pcm_format.bit_depth // 8)
        self._pcm_frame_size = bytes_per_sample * pcm_format.channels
        self._pcm_byte_rate = self._pcm_frame_size * pcm_format.sample_rate
        self._pcm_buffer_max = self._ring_bytes_for(0.0)
        # Bytes still to skip off the head of the live feed for a late joiner
        # whose anchor lands ahead of the current write head, keyed by player id.
        self._client_skip_bytes: dict[str, int] = {}
        # Cumulative bytes fed to the members (absolute stream coordinate of
        # the write head). Source chunking makes no frame-alignment promise,
        # so any slice or skip of the live feed must be aligned against THIS
        # counter — aligning lengths in isolation can still land mid-sample,
        # which a joiner renders as pure static.
        self._pcm_total_fed: int = 0
        # Set once the first audio of a start cycle has been handed to the
        # members, and also when the source ends without ever handing any over,
        # so a waiter is never left holding on for a feed that is not coming.
        self._feed_settled = asyncio.Event()
        # Starvation shift of the group timeline a late joiner was mapped onto,
        # keyed by player id. Its own reported shift counts from there.
        self._member_shift_base: dict[str, float] = {}
        # Pending comparison of the members' shifts after a stall.
        self._shift_check_task_id = f"airplay_stall_check_{id(self)}"
        # Earliest moment of the next stall restart, and the hold-off after it.
        self._resync_not_before: float = 0.0
        self._resync_holdoff: float = AIRPLAY_STALL_RESYNC_HOLDOFF
        # How often each member that is not heard (the Apple TV of a set) was added, by
        # player id: it joins after the members it goes with, and is added once more at
        # most after it left. Its pending join, by player id.
        self._silent_member_joins: dict[str, int] = {}
        self._silent_member_tasks: dict[str, asyncio.Task[None]] = {}
        # When a speaker of its set last joined, by player id of the member that is not heard.
        self._speaker_joined_at: dict[str, float] = {}
        # The members that are not heard and were started since the last (re)start: before
        # its START a member backs up the feed like any other, which is no stall.
        self._started_silent_members: set[str] = set()

    @property
    def effective_start_time(self) -> float:
        """
        Return the session anchor adjusted for the reference member's clock shift.

        ``start_time`` is the cliairplay timeline anchor set at the last group (re)start.
        A member's cliairplay can re-anchor its playout LATER after recovering
        from a PCM starvation, shifting the group's real timeline; that shift is
        tracked per member. The first sync client that is heard is taken as the
        reference and its accumulated shift is added, so a late joiner maps its
        first sample to where the group actually plays. Divergent per-member shifts
        make any single reference imperfect; the current first client is the best
        available choice and the reference transfers to the new first client when
        the old one is removed.
        """
        if not (audible := self._audible_clients):
            return self.start_time
        return self.start_time + self._member_shift(audible[0])

    async def start(self, audio_source: AsyncGenerator[bytes]) -> None:
        """
        Connect every member and anchor synchronized playback.

        Spawns and connects each member's CLI, wires the per-seek ffmpeg into its
        persistent stdin and starts feeding audio, then commands one shared
        audible start instant. On any failure the whole session is stopped so the
        caller can fall back to a cold restart.
        """
        ap2_members = sum(1 for p in self.sync_clients if p.protocol == StreamingProtocol.AIRPLAY2)
        if ap2_members:
            # Resolve the timing source before calculating the audible anchor so
            # a bounded daemon-readiness wait cannot consume the setup lead.
            self.use_shared_ptp = await self._resolve_shared_ptp(ap2_members)
            self._shared_ptp_resolved = True
        position_ms = int((self.media.elapsed_time or 0) * 1000)
        try:
            async with asyncio.TaskGroup() as task_group:
                for player in self.sync_clients:
                    task_group.create_task(
                        self._member_start_step(
                            player, "spawn its cli", self._start_client(player, self.use_shared_ptp)
                        )
                    )
            await asyncio.gather(
                *[
                    self._member_start_step(
                        player, "connect to its device", player.stream.wait_for_connection()
                    )
                    for player in self.sync_clients
                    if player.stream
                ]
            )
            # The binary buffers stdin into its ring from process start; feed
            # audio first and wait for every member to confirm it flowing, then
            # anchor with a short lead. Readiness is fully event-driven
            # (connected + audio), so no guessed setup time is needed; the
            # binary bursts the receiver pre-fill after START.
            self._audio_source_task = asyncio.create_task(self._audio_streamer(audio_source))
            await self._wait_members_audio_present()
            # Members of a group have to agree on one instant, and a freshly
            # connected receiver needs about a second before it even starts
            # probing — too late for the binary to raise its own commit floor,
            # and a first start is the one case it will not correct afterwards.
            # So a group anchor waits for the receivers to say when they can
            # play, rather than trusting the lead to have covered it.
            ready_at_unix_ms = await self._wait_members_clock_ready()
            start_unix_ms = self._anchor_start_unix_ms(ready_at_unix_ms=ready_at_unix_ms)
            await self._start_members(
                position_ms,
                await self._seat_member_anchors(position_ms, start_unix_ms),
            )
            self._join_silent_members()
        except asyncio.CancelledError:
            await self.stop()
            raise
        except Exception as err:
            # playback failed to start, cleanup. This runs for every failure. A
            # per-member one has already been named where it happened, so here
            # the line says the whole session went down with it; for a failure
            # no single member owns, it is the only line there is.
            self.prov.logger.warning(
                "AirPlay start failed for a session of %d member(s): %s",
                len(self.sync_clients),
                err,
            )
            await self.stop()
            # A member can fail for a specific, user-actionable reason (a device
            # that needs its password configured, for example). That error must
            # reach the caller intact instead of being flattened into the generic
            # message; the TaskGroup above nests it inside an exception group.
            if (specific := _first_music_assistant_error(err)) is not None:
                raise specific from err
            raise PlayerCommandFailed("Playback failed to start") from err

    def can_replace(self, sync_clients: list[AirPlayPlayer], pcm_format: AudioFormat) -> bool:
        """
        Return whether this live session can absorb a new play_media warm.

        A warm replacement needs the same member set, the same session PCM
        format and a connected stream still taking audio on every member;
        anything else takes the cold path. A member that is not heard (the
        Apple TV of a set) does not count: the replacement goes on without it.
        """
        audible = self._audible_clients
        if {p.protocol_id for p in sync_clients} != {p.protocol_id for p in audible}:
            return False
        # the encoding matters as much as the depth here (a 24-bit session carries
        # PCM_S32LE): replace() wires the new source into the session's declared format
        if pcm_format != self.pcm_format:
            return False
        return all(
            p.stream is not None and p.stream.accepts_audio and p.stream.connected for p in audible
        )

    async def replace(self, audio_source: AsyncGenerator[bytes], media: PlayerMedia) -> bool:
        """
        Warm-replace the playing media with a new source (seek/next-track).

        Stops feeding old audio and kills each member's ffmpeg (never the
        persistent cli stdin), flushes every member's live stream in place, then
        feeds a fresh ffmpeg into the same stdin and anchors all members at one
        shared instant. A group flushes every member and awaits all acks before
        the shared start. Returns False when anything fails so the caller can
        fall back to the cold path.
        """
        position_ms = int((media.elapsed_time or 0) * 1000)
        # the shared START below puts every member back on one timeline
        self.mass.cancel_timer(self._shift_check_task_id)
        try:
            # Stop feeding old audio and drop each member's buffered ffmpeg output
            # before flushing: the binary drains stdin to EAGAIN on FLUSH, so no
            # bytes may be written between the old ffmpeg dying and the flush ack.
            # Killing ffmpeg never closes the cli stdin (MA holds the write end),
            # so the binary keeps its stdin reader alive across the seek.
            if self._audio_source_task and not self._audio_source_task.done():
                self._audio_source_task.cancel()
                with suppress(asyncio.CancelledError):
                    await self._audio_source_task
            members = list(self.sync_clients)
            self._started_silent_members.clear()
            for player in members:
                if ffmpeg := self._player_ffmpeg.pop(player.protocol_id, None):
                    await ffmpeg.kill()
            flushed = await asyncio.gather(*[self._flush_member(player) for player in members])
            if not all(
                ok for player, ok in zip(members, flushed, strict=True) if player.renders_audio
            ):
                raise PlayerCommandFailed("warm flush was not acknowledged")  # noqa: TRY301 - triggers cold restart fallback
            for player, ok in zip(members, flushed, strict=True):
                if not ok:
                    # the music goes on without a member that is not heard, which is added once
                    # more: each seek or resume it misses gives it a retry of its own
                    self._silent_member_joins.pop(player.protocol_id, None)
                    await self.remove_silent_member(
                        player, rejoin=True, reason="warm flush was not acknowledged"
                    )
            for player in self.sync_clients:
                stream = player.stream
                assert stream
                await self._start_player_ffmpeg(player, media, stream)
            self.media = media
            # The stream position counter and the late-join prime buffer both
            # describe the OLD timeline; restart them before the new source pumps.
            self.seconds_streamed = 0
            self._pcm_total_fed = 0
            self._feed_settled.clear()
            self._pcm_buffer.clear()
            # The shared START below re-establishes start_time, so the per-client
            # late-join skip counters and every member's accumulated starvation
            # shift describe a timeline that no longer exists: reset them.
            self._client_skip_bytes.clear()
            self._reset_member_shifts()
            self._audio_source_task = asyncio.create_task(self._audio_streamer(audio_source))
            # Anchor only after every member confirms the new audio flowing;
            # the live connection and clock survive the flush, so a short
            # re-anchor lead replaces the full setup lead.
            await self._wait_members_audio_present()
            await self._start_members(position_ms, self._anchor_start_unix_ms(warm=True))
            self._join_silent_members()
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 - any failure falls back to a cold restart
            self.prov.logger.warning(
                "Warm replacement failed (%r); falling back to a cold restart", err
            )
            return False
        return True

    async def standby(self) -> bool:
        """
        Park the session: stall every member but keep the connections alive.

        The next play_media (resume or seek) replaces the media warm over the
        live connections — the same coordinated flush-refill as seek/next.
        Returns False when any member lacks a connected stream that still takes
        audio, or its standby command cannot be delivered, so the caller can fall
        back to a full stop.
        """
        if not all(
            p.stream is not None and p.stream.accepts_audio and p.stream.connected
            for p in self._audible_clients
        ):
            return False
        self.mass.cancel_timer(self._shift_check_task_id)
        if self._audio_source_task and not self._audio_source_task.done():
            self._audio_source_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._audio_source_task
        for player in list(self.sync_clients):
            if ffmpeg := self._player_ffmpeg.pop(player.protocol_id, None):
                await ffmpeg.kill()
            if not player.renders_audio:
                # the music is parked whether or not a member that is not heard parks along
                if not await self._command_silent_member(player, "ACTION=STANDBY"):
                    # it joins again once the music plays again, as one that found none to join
                    self._silent_member_joins.pop(player.protocol_id, None)
                    await self.remove_client(player, reason="standby command was not delivered")
                continue
            stream = player.stream
            assert stream
            try:
                command_delivered = await stream.send_cli_command("ACTION=STANDBY")
            except Exception as err:  # noqa: BLE001 - any failure falls back to a cold restart
                self.prov.logger.warning(
                    "Could not park AirPlay player %s: %s", player.protocol_id, err
                )
                return False
            if not command_delivered:
                self.prov.logger.warning(
                    "Could not park AirPlay player %s: standby command was not delivered",
                    player.protocol_id,
                )
                return False
            stream.start_pending = False
            player.set_state_from_stream(state=PlaybackState.PAUSED, stream=stream)
        # a parked session has no live timeline; the resume re-anchors it
        self.seconds_streamed = 0
        self._pcm_total_fed = 0
        self._feed_settled.clear()
        self._pcm_buffer.clear()
        self._client_skip_bytes.clear()
        self._reset_member_shifts()
        self.parked = True
        return True

    async def stop(self) -> None:
        """Stop playback and cleanup."""
        self.mass.cancel_timer(self._shift_check_task_id)
        for task in self._silent_member_tasks.values():
            # a join that fails ends a session that has nobody left to play
            if task is not asyncio.current_task():
                task.cancel()
        if self._audio_source_task and not self._audio_source_task.done():
            self._audio_source_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._audio_source_task
        await asyncio.gather(
            *[self.remove_client(x, reason="session stop") for x in self.sync_clients],
        )

    async def remove_client(
        self, airplay_player: AirPlayPlayer, reason: str = "client removed"
    ) -> None:
        """
        Remove a sync client from the session.

        :param airplay_player: The player to remove from the session.
        :param reason: Short human-readable reason for the removal, used in teardown logs.
        """
        async with self._lock:
            if airplay_player not in self.sync_clients:
                return
            self.sync_clients.remove(airplay_player)
        await self._cleanup_after_removal(airplay_player, reason=reason)

    async def remove_stalled_joiner(self, airplay_player: AirPlayPlayer) -> None:
        """
        Remove a late joiner whose receiver never answered the server's PTP clock.

        Such a receiver renders silence, so the group plays on without it and the
        player reports idle instead of playing.

        :param airplay_player: The late joiner to remove.
        """
        async with self._lock:
            if airplay_player not in self.sync_clients:
                return
            self.sync_clients.remove(airplay_player)
        self.prov.logger.warning(
            "Removed late joiner %s from the group: its receiver never answered the "
            "server's PTP clock, so it would only render silence",
            airplay_player.display_name,
        )
        await self._cleanup_after_removal(airplay_player, reason="receiver clock stalled")

    async def stop_client(
        self, airplay_player: AirPlayPlayer, reason: str = "stop_client called"
    ) -> None:
        """
        Stop a client's stream and ffmpeg.

        :param airplay_player: The player to stop.
        :param reason: Short human-readable reason for the teardown, used in debug logs.
        """
        self.prov.logger.debug(
            "AirPlay session teardown: session=%s client=%s reason=%s",
            id(self),
            airplay_player.protocol_id,
            reason,
        )
        self._client_skip_bytes.pop(airplay_player.protocol_id, None)
        self._member_shift_base.pop(airplay_player.protocol_id, None)
        self._started_silent_members.discard(airplay_player.protocol_id)
        ffmpeg = self._player_ffmpeg.pop(airplay_player.protocol_id, None)
        # note that we use kill instead of graceful close here,
        # because otherwise it can take a very long time for the process to exit.
        if ffmpeg and not ffmpeg.closed:
            await ffmpeg.kill()
        if airplay_player.stream and airplay_player.stream.session == self:
            airplay_player.stream.reset_reanchor_shift()
            await airplay_player.stream.stop(force=True)

    def add_silent_member(self, airplay_player: AirPlayPlayer) -> None:
        """
        Add a member that is not heard (the Apple TV of a set) as a late joiner, in a task.

        :param airplay_player: The member to add.
        """
        self._silent_member_joins.pop(airplay_player.protocol_id, None)
        self._schedule_silent_member_join(airplay_player)

    async def remove_silent_member(
        self,
        airplay_player: AirPlayPlayer,
        *,
        rejoin: bool,
        reason: str = "stream of a member that is not heard ended",
    ) -> None:
        """
        Take a member that is not heard out of the session, also when it is still joining.

        The other members play on.

        :param airplay_player: The member to take out.
        :param rejoin: Whether to add it once more, after a short wait, unless it was
            added again before.
        :param reason: Short reason for the teardown logs.
        """
        pending = self._silent_member_tasks.get(airplay_player.protocol_id)
        if not rejoin and pending and not pending.done() and pending is not asyncio.current_task():
            pending.cancel()
        await self.remove_client(airplay_player, reason=reason)
        if rejoin:
            self._rejoin_silent_member(airplay_player)

    async def end_silent_member(self, airplay_player: AirPlayPlayer, *, taken_over: bool) -> None:
        """
        Take a member that is not heard out of the session after its stream ended.

        It is added once more, unless another sender took its device over. Right after a
        speaker of its set joined, that report is the reset the join caused and no other sender.

        :param airplay_player: The member whose stream ended.
        :param taken_over: Whether its device reported that another sender took it over.
        """
        joined_at = self._speaker_joined_at.get(airplay_player.protocol_id)
        reset = (
            joined_at is not None
            and time.monotonic() - joined_at < AIRPLAY_SILENT_MEMBER_RESET_WINDOW
        )
        await self.remove_silent_member(airplay_player, rejoin=not taken_over or reset)

    async def add_client(  # noqa: PLR0915
        self, airplay_player: AirPlayPlayer, *, returning: bool = False
    ) -> None:
        """
        Add a sync client to the session as a late joiner.

        The joiner cannot honour an anchor in the past — cliairplay makes the
        first post-START stdin byte audible exactly at the instant it acks and
        then freezes the anchor, with no catch-up. So the anchor is commanded
        just past the instant the receiver reports its clock becomes usable (and
        never inside the join floor), the binary acks the instant it can truly
        honour, and the stream position due at that instant is derived from the
        group's effective (shift-adjusted) timeline. Depending on where that
        position falls relative to the ring buffer the joiner is either primed
        from the ring tail (position at or behind the write head) or has the
        leading bytes of the live feed skipped (position ahead of the write
        head), so its first audible sample lands exactly where the group is
        playing.

        Timing-source readiness, the receiver's clock projection and the START
        ack are all awaited without the session lock so the rest of the group
        keeps being fed meanwhile; all buffer and anchor math stays under the
        lock so ``seconds_streamed``, the ring buffer and the per-client skip
        counter stay consistent with the live feed.

        A speaker of a set that cannot join takes its set along, as a start of the set fails
        as a whole, unless it comes back into the session its set already plays in.

        :param airplay_player: The player to add.
        :param returning: Whether a speaker comes back into the session its set plays in: one
            that cannot join is then only left out, and the set plays on what is there.
        """
        await self._resolve_late_joiner_ptp(airplay_player)
        async with self._lock:
            if not self._session_is_live():
                return
        try:
            await self._start_client(airplay_player, self.use_shared_ptp)
            stream = airplay_player.stream
            assert stream
            await stream.wait_for_connection()
            # A receiver starts probing its clock at connect, so how long it
            # still needs is measurable before any anchor is announced. Waiting
            # for that projection here — outside the session lock, so the rest
            # of the group keeps being fed — lets the join anchor on the
            # device's own readiness instead of a fixed guess.
            readiness, ready_at_unix_ms = await stream.wait_clock_ready(
                timeout=AIRPLAY_CLOCK_READY_TIMEOUT_MS / 1000
            )
        except asyncio.CancelledError:
            await self.stop_client(airplay_player, reason="late joiner start cancelled")
            raise
        except Exception as err:  # noqa: BLE001 - a failing late joiner must not stop the group
            self.prov.logger.warning(
                "Late joiner %s: failed to connect pipeline: %s",
                airplay_player.protocol_id,
                err,
            )
            await self.stop_client(airplay_player, reason="late joiner connection failed")
            self._join_failed(airplay_player, returning=returning)
            return

        pcm_sample_size = self._pcm_byte_rate
        frame_size = self._pcm_frame_size

        def map_to(anchor_at: float, *, committed: bool) -> tuple[float, float, bytes, int]:
            """
            Map an anchor instant onto the live feed (call under the lock).

            Snapshots the ring and derives which stream position the group
            plays at ``anchor_at``, returning the anchor, the due feed
            position, the prime slice and the live skip.

            :param anchor_at: Instant at which the joiner's first delivered
                sample becomes audible.
            :param committed: True once the binary has acked the instant, which
                fixes it. A due position the ring can no longer reach is then
                covered with silence rather than by moving the anchor, because
                moving an instant the binary already owns would offset the
                joiner from the group by exactly that much.
            """
            # Snapshot the ring, which ends at the write head: it covers the
            # stream positions [seconds_streamed - len(ring)/rate, seconds_streamed].
            effective_start_time = self.effective_start_time
            buffered_pcm = bytes(self._pcm_buffer)
            due = anchor_at - effective_start_time
            skip = 0
            prime_slice = b""
            if due <= self.seconds_streamed:
                # The due position is at or behind the write head: prime
                # the joiner from the ring so the live feed then continues
                # seamlessly.
                keep_bytes = int((self.seconds_streamed - due) * pcm_sample_size)
                # Frame-align the prime START in ABSOLUTE stream
                # coordinates. The prime must end exactly at the write head
                # (the live feed continues byte-contiguously from there),
                # so only the start may move: shift it forward to the next
                # frame boundary (dropping under one frame, ~23 us).
                start_abs = self._pcm_total_fed - keep_bytes
                realign = (frame_size - start_abs % frame_size) % frame_size
                keep_bytes -= realign
                if realign:
                    self.prov.logger.debug(
                        "Late joiner %s: prime start realigned +%d bytes "
                        "(write head sits mid-frame)",
                        airplay_player.protocol_id,
                        realign,
                    )
                # The ring's own head is frame-aligned only by accident - it is
                # trimmed on byte overflow - so align it too before measuring
                # how much of the requested prime it can really serve.
                ring_head_abs = self._pcm_total_fed - len(buffered_pcm)
                ring_realign = (frame_size - ring_head_abs % frame_size) % frame_size
                servable = buffered_pcm[ring_realign:]
                if keep_bytes > len(servable):
                    # The due position predates the ring: the write head ran
                    # further ahead of the audible position than the ring was
                    # sized for. Those samples are gone, so the join either
                    # starts a little later (anchor still free to move) or opens
                    # with that much silence (anchor already acked) - both keep
                    # the real content on the instant the group plays it, which
                    # dropping the missing head would not.
                    missing = keep_bytes - len(servable)
                    lead = self.seconds_streamed - (
                        cli_clock_time(self.mass.timeline_clock) - effective_start_time
                    )
                    if committed:
                        # One ring's worth is already far past any real
                        # shortfall, so it bounds what an implausible ack (one
                        # mapping back near the session start) can allocate.
                        pad = min(missing, self._pcm_buffer_max)
                        prime_slice = bytes(pad) + servable
                        # A clamped pad cannot reach the due position, so the
                        # joiner really does end up ahead of the group by the
                        # remainder. Say which of the two happened.
                        residual = (missing - pad) / pcm_sample_size
                        self.prov.logger.warning(
                            "Late joiner %s: the feed runs %.2fs ahead of the audible "
                            "position, past the %.2fs of audio kept for a join; opening "
                            "with %.2fs of silence to cover the missing head. %s Please "
                            "report this with a debug log.",
                            airplay_player.protocol_id,
                            lead,
                            len(servable) / pcm_sample_size,
                            pad / pcm_sample_size,
                            f"That still leaves it {residual:.2f}s ahead of the group."
                            if residual
                            else "The content itself still lands in sync; the joiner is "
                            "only audible that much late.",
                        )
                    else:
                        due += missing / pcm_sample_size
                        anchor_at = effective_start_time + due
                        prime_slice = servable
                        self.prov.logger.debug(
                            "Late joiner %s: due position predates the %.2fs ring "
                            "(feed runs %.2fs ahead); anchoring %.2fs later, on the "
                            "ring's oldest sample",
                            airplay_player.protocol_id,
                            len(servable) / pcm_sample_size,
                            lead,
                            missing / pcm_sample_size,
                        )
                elif keep_bytes > 0:
                    prime_slice = servable[len(servable) - keep_bytes :]
            else:
                # The due position is ahead of the write head: skip that
                # many bytes off the head of the live feed so the joiner's
                # first delivered byte is the sample due at the anchor.
                skip = int((due - self.seconds_streamed) * pcm_sample_size)
                # The first delivered live byte must land on a frame
                # boundary in ABSOLUTE stream coordinates (round the skip
                # up, under one frame).
                first_abs = self._pcm_total_fed + skip
                skip += (frame_size - first_abs % frame_size) % frame_size
            return anchor_at, due, prime_slice, skip

        now = cli_clock_time(self.mass.timeline_clock)
        clock_out = ready_at_unix_ms / 1000 - now
        if readiness is ClockReadiness.PROJECTED and clock_out <= 0:
            clock_note = _CLOCK_LOCKED_NOTE.format(ago=abs(clock_out))
        else:
            clock_note = _CLOCK_READINESS_NOTES.get(readiness, "").format(
                out=clock_out,
                timeout=AIRPLAY_CLOCK_READY_TIMEOUT_MS / 1000,
            )
        self.prov.logger.debug(
            "Late joiner %s: receiver clock %s",
            airplay_player.protocol_id,
            clock_note,
        )
        try:
            async with self._lock:
                if not self._session_is_live():
                    await self.stop_client(airplay_player, reason="session ended during late join")
                    return
                # cliairplay makes the first post-START stdin byte audible exactly at
                # the instant it acks, so the anchor is commanded first and the
                # content mapped onto the ack afterwards. It sits just past the
                # instant the receiver's clock becomes usable; the floor is only a
                # lower bound, and carries the whole anchor when no projection came.
                min_headroom = AIRPLAY_LATE_JOIN_MIN_HEADROOM_MS / 1000
                anchor_at = now + min_headroom
                if ready_at_unix_ms:
                    anchor_at = max(
                        anchor_at,
                        (ready_at_unix_ms + AIRPLAY_CLOCK_READY_LEAD_MS) / 1000,
                    )
                requested_at, fed_pos_due = map_to(anchor_at, committed=False)[:2]
                start_unix_ms = int(requested_at * 1000)
                adjust_ms = airplay_player.sync_offset_ms
                position_ms = int(((self.media.elapsed_time or 0) + fed_pos_due) * 1000)
            # Anchor the joiner BEFORE feeding the prime: pre-START the binary
            # only buffers stdin into its bounded ring and sends nothing, so a
            # prime longer than that ring would wedge that write — and, through
            # the session lock, stall the whole group's feed. Anchored, the
            # binary drains the prime as it streams in. The START does not
            # re-anchor the session timeline (the group keeps playing). Its ack
            # is awaited WITHOUT the session lock: the binary can hold that ack
            # until its receiver clock verification resolves, which would
            # otherwise starve every other member's feed for that whole wait.
            actual = await stream.start(start_unix_ms + adjust_ms, position_ms, join=True)
        except asyncio.CancelledError:
            await self.stop_client(airplay_player, reason="late joiner start cancelled")
            raise
        except Exception as err:  # noqa: BLE001 - a failing late joiner must not stop the group
            self.prov.logger.warning(
                "Late joiner %s: failed to start/prime pipeline: %r",
                airplay_player.protocol_id,
                err,
            )
            await self.stop_client(airplay_player, reason="late joiner start/prime failed")
            self._join_failed(airplay_player, returning=returning)
            return

        try:
            async with self._lock:
                if not self._session_is_live():
                    await self.stop_client(airplay_player, reason="session ended during late join")
                    return
                if airplay_player.stream is not stream or not stream.running:
                    await self.stop_client(
                        airplay_player, reason="late joiner stopped during start"
                    )
                    return
                if not self._admit_silent_member(airplay_player):
                    await self.stop_client(airplay_player, reason="set left during late join")
                    return
                # Map the content onto the instant the binary acked — its
                # verified truth. The ring is re-snapshotted here, so the mapping
                # covers whatever the group was fed while the ack was outstanding.
                acked_at = (actual - adjust_ms) / 1000
                start_at, fed_pos_due, prime, skip_bytes = map_to(acked_at, committed=True)
                self._client_skip_bytes[airplay_player.protocol_id] = skip_bytes
                self._member_shift_base[airplay_player.protocol_id] = (
                    self.effective_start_time - self.start_time
                )
                # An instant that moved carries the content mapped onto it
                # further into the stream than the position sent with the
                # command, so progress is reported against the sample that
                # actually lands on the anchor.
                acked_position_ms = int(((self.media.elapsed_time or 0) + fed_pos_due) * 1000)
                if acked_position_ms != position_ms:
                    stream.rebase_position(acked_position_ms)

                self.prov.logger.debug(
                    "Late joiner %s: priming %.2fs, skipping %.2fs, stream_pos=%.2fs, "
                    "fed_pos_due=%.2fs, start_at is %.2fs from now, acked %+d ms off the "
                    "commanded instant (min_headroom=%.2fs, effective_shift=%.2fs, "
                    "write_head_lead=%.2fs, peak=%.2fs, ring=%.2fs)",
                    airplay_player.protocol_id,
                    len(prime) / pcm_sample_size,
                    skip_bytes / pcm_sample_size,
                    self.seconds_streamed,
                    fed_pos_due,
                    start_at - cli_clock_time(self.mass.timeline_clock),
                    int(acked_at * 1000) - start_unix_ms,
                    min_headroom,
                    self.effective_start_time - self.start_time,
                    self.seconds_streamed
                    - (cli_clock_time(self.mass.timeline_clock) - self.effective_start_time),
                    self._peak_lead_seconds,
                    self._pcm_buffer_max / pcm_sample_size,
                )

                if prime:
                    try:
                        await self._write_chunk_to_player(airplay_player, prime)
                    except Exception as err:  # noqa: BLE001 - a failing late joiner must not stop the group
                        self.prov.logger.warning(
                            "Late joiner %s: failed to start/prime pipeline: %r",
                            airplay_player.protocol_id,
                            err,
                        )
                        await self.stop_client(
                            airplay_player, reason="late joiner start/prime failed"
                        )
                        self._join_failed(airplay_player, returning=returning)
                        return

                if airplay_player not in self.sync_clients:
                    self.sync_clients.append(airplay_player)
                if (
                    airplay_player.protocol == StreamingProtocol.AIRPLAY2
                    and not self.use_shared_ptp
                ):
                    ap2_members = sum(
                        1
                        for player in self.sync_clients
                        if player.protocol == StreamingProtocol.AIRPLAY2
                    )
                    self._warn_degraded_shared_ptp(ap2_members)
        except asyncio.CancelledError:
            # the joiner is anchored and audible from here on, so a cancellation
            # before it is part of the session must still tear it down
            await self.stop_client(airplay_player, reason="late joiner start cancelled")
            raise

        self.prov.logger.debug(
            "Late joiner %s: started after %.2fs",
            airplay_player.protocol_id,
            cli_clock_time(self.mass.timeline_clock) - now,
        )
        if airplay_player.renders_audio:
            self._join_silent_members(joined=airplay_player)

    def on_member_reanchored(self) -> None:
        """
        Handle a member shifting its playout after its audio feed ran dry.

        Once the stall is over, a group whose members came out of it on different
        timelines is restarted at the current position to bring it back in sync.
        """
        if len(self._audible_clients) < 2:
            return
        # every further report of the same stall postpones the comparison
        self.mass.call_later(
            AIRPLAY_STALL_SETTLE_SECONDS,
            self._check_member_shifts,
            task_id=self._shift_check_task_id,
        )

    def _ring_bytes_for(self, lead_seconds: float) -> int:
        """
        Return the late-join ring size that covers a given write-head lead.

        :param lead_seconds: Lead the ring has to span, before margin.
        """
        seconds = max(
            lead_seconds + AIRPLAY_LATE_JOIN_RING_MARGIN_SECONDS,
            AIRPLAY_LATE_JOIN_RING_MIN_SECONDS,
        )
        size = min(int(seconds * self._pcm_byte_rate), AIRPLAY_LATE_JOIN_RING_MAX_BYTES)
        # Keep it a whole number of frames so it can bound a silence pad without
        # knocking the prime off its frame boundaries.
        return size - size % self._pcm_frame_size

    def _observe_write_head_lead(self) -> None:
        """
        Track how far the feed runs ahead of the audible position (call under the lock).

        A joiner's anchor maps into exactly this span, so the ring is grown to
        the largest lead the session has shown. It only ever grows: shrinking it
        mid-session would discard history a joiner still needs, and the lead is
        at its largest right after the anchor is placed - the whole downstream
        pipeline is full by then - so the ring is sized long before any late
        join can arrive.
        """
        if self.start_time <= 0:
            return  # not anchored yet, so there is no audible position to measure against
        now = cli_clock_time(self.mass.timeline_clock)
        if now < self.effective_start_time:
            # Still inside the start lead: everything fed is ahead of an anchor
            # that has not arrived, which would read as a lead seconds larger
            # than the pipeline really holds.
            return
        lead = self.seconds_streamed - (now - self.effective_start_time)
        if lead <= self._peak_lead_seconds:
            return
        self._peak_lead_seconds = lead
        self._pcm_buffer_max = self._ring_bytes_for(lead)

    def _session_is_live(self) -> bool:
        """Return whether the session still plays a joinable timeline (call under the lock)."""
        if not (audible := self._audible_clients):
            return False
        reference = audible[0]
        reference_stream = reference.stream
        # A stream that has been sent its audio EOF keeps running while it plays
        # out, but it is on its way to exiting and can never be fed again, so a
        # joiner would land in a session that is ending.
        if reference_stream is None or not reference_stream.accepts_audio:
            return False
        # A parked (standby) session keeps every member's stream running while
        # its timeline is gone - the anchor is stale and nothing is being fed -
        # so only a member that is actually playing can absorb a joiner.
        return reference.playback_state == PlaybackState.PLAYING

    async def _resolve_shared_ptp(self, ap2_members: int | None = None) -> bool:
        """
        Decide, once per session, whether members attach to the shared PTP daemon.

        The decision is session-wide and gated on the daemon actually being ready
        (bound to 319/320 with its control channel open), not merely spawned, so a
        group start cannot race the daemon and end up mixing PTP and NTP members.

        :param ap2_members: Number of native AirPlay 2 members that will use the
            decision. Defaults to the current session members.
        :return: True if every native AirPlay 2 member should use the shared PTP
            clock; False to degrade the whole session consistently (no member
            attaches to the daemon).
        """
        if ap2_members is None:
            ap2_members = sum(
                1 for p in self.sync_clients if p.protocol == StreamingProtocol.AIRPLAY2
            )
        if not ap2_members:
            return False
        if await self.prov.wait_ptp_daemon_ready():
            return True
        # Daemon not ready: keep the group coherent by attaching no one to the
        # shared clock. A lone native AP2 player can still self-bind its own PTP
        # with no partner to drift against; only a real multi-room group loses
        # tight sync, so warn just for that case.
        self._warn_degraded_shared_ptp(ap2_members)
        return False

    async def _resolve_late_joiner_ptp(self, airplay_player: AirPlayPlayer) -> None:
        """Resolve the session timing source before its first late AirPlay 2 join."""
        if airplay_player.protocol != StreamingProtocol.AIRPLAY2:
            return
        async with self._ptp_lock:
            if self._shared_ptp_resolved:
                return
            async with self._lock:
                ap2_members = sum(
                    1
                    for player in self.sync_clients
                    if player.protocol == StreamingProtocol.AIRPLAY2
                ) + (airplay_player not in self.sync_clients)
            self.use_shared_ptp = await self._resolve_shared_ptp(ap2_members)
            self._shared_ptp_resolved = True

    def _warn_degraded_shared_ptp(self, ap2_members: int) -> None:
        """Warn once when multiple AirPlay 2 members cannot share the PTP clock."""
        if ap2_members <= 1 or self._ptp_degraded_warning_logged:
            return
        self._ptp_degraded_warning_logged = True
        self.prov.logger.warning(
            "Shared PTP clock daemon not ready - native AirPlay 2 multi-room sync "
            "for this group of %d players is degraded and members may drift. The "
            "server likely cannot bind the privileged PTP ports (UDP 319/320); "
            "running without root or CAP_NET_BIND_SERVICE is the common cause.",
            ap2_members,
        )

    async def _cleanup_after_removal(
        self, airplay_player: AirPlayPlayer, reason: str = "client removed"
    ) -> None:
        """
        Clean up processes and state after a client has been removed from sync_clients.

        :param airplay_player: The player whose processes should be stopped.
        :param reason: Short human-readable reason, forwarded to stop_client for logging.
        """
        stream = airplay_player.stream
        if stream is not None and stream.session != self:
            stream = None
        await self.stop_client(airplay_player, reason=reason)
        # Only set IDLE if the player's stream still belongs to this session,
        # otherwise a re-add to a new session may have already set a new state.
        if stream is not None:
            airplay_player.set_state_from_stream(PlaybackState.IDLE, stream=stream)
        # Re-check sync_clients under the lock to avoid racing with add_client. Members
        # that are not heard do not keep the session going on their own.
        async with self._lock:
            should_stop = not self._audible_clients
        if should_stop:
            await self.stop()

    async def _audio_streamer(self, audio_source: AsyncGenerator[bytes]) -> None:
        """Stream audio to all players."""
        stream_error: BaseException | None = None
        try:
            # the loop below leaves early once the clients are gone; closing the source
            # from here releases its decoders instead of waiting on the garbage collector
            async with aclosing(audio_source):
                async for chunk in audio_source:
                    if not self.sync_clients:
                        break

                    has_running_clients = await self._write_chunk_to_all_players(chunk)
                    if not has_running_clients:
                        self.prov.logger.debug(
                            "No running clients remaining, stopping audio streamer"
                        )
                        break
        except asyncio.CancelledError:
            self.prov.logger.debug("Audio streamer cancelled after %.1fs", self.seconds_streamed)
            raise
        except Exception as err:
            stream_error = err
            self.prov.logger.error(
                "Audio source error after %.1fs of streaming: %s",
                self.seconds_streamed,
                err,
                exc_info=err,
            )
        finally:
            # a source that ends - or is cancelled - without handing anything
            # over settles the question for a start still waiting on the feed
            self._feed_settled.set()
            if stream_error:
                self.prov.logger.warning(
                    "Stream ended prematurely due to error - notifying players"
                )
        # A source that ends is not the same thing as a stream that is over: a
        # seek or a next-track ends this one while the queue is already loading
        # the stream that takes over. Closing the binary's stdin there would end
        # the stream for good - it cannot be reopened - and cost that replacement
        # a full cold restart. Everywhere else the EOF is what ends playback: the
        # binary plays out, reports eof and exits, and only that makes the player
        # report idle, which a queue waiting to restart its flow depends on.
        end_of_stream = not self._replacement_expected()
        async with self._lock:
            await asyncio.gather(
                *[
                    self._retire_player_ffmpeg(x, end_of_stream=end_of_stream)
                    for x in self.sync_clients
                    if x.stream and x.stream.running
                ],
                return_exceptions=True,
            )
        if not end_of_stream:
            await self._end_stream_if_no_replacement_lands()

    async def _write_chunk_to_all_players(self, chunk: bytes) -> bool:
        """
        Write a chunk to all connected players.

        :return: True if there are still running clients, False otherwise.
        """
        async with self._lock:
            sync_clients = [x for x in self.sync_clients if x.stream and x.stream.running]
            if not sync_clients:
                return False

            # Update seconds_streamed and ring buffer under the lock so
            # add_client always reads consistent values.
            self.seconds_streamed += len(chunk) / self._pcm_byte_rate
            self._pcm_total_fed += len(chunk)
            self._feed_settled.set()
            self._observe_write_head_lead()
            self._pcm_buffer.extend(chunk)
            overflow = len(self._pcm_buffer) - self._pcm_buffer_max
            if overflow > 0:
                del self._pcm_buffer[:overflow]

            # Write chunk to all players
            write_tasks = [self._write_chunk_to_player(x, chunk) for x in sync_clients if x.stream]
            results = await asyncio.gather(*write_tasks, return_exceptions=True)

            # Check for write errors or timeouts
            players_to_remove: list[tuple[AirPlayPlayer, str]] = []
            for i, result in enumerate(results):
                if i >= len(sync_clients):
                    continue
                player = sync_clients[i]

                if isinstance(result, TimeoutError):
                    self.prov.logger.warning(
                        "Removing player %s from session: stopped reading data (write timeout)",
                        player.protocol_id,
                    )
                    players_to_remove.append((player, "audio write timeout"))
                elif isinstance(result, Exception):
                    self.prov.logger.warning(
                        "Removing player %s from session due to write error: %s",
                        player.protocol_id,
                        result,
                    )
                    players_to_remove.append((player, f"audio write error: {result}"))

            # Remove failed players from sync_clients immediately under the lock
            # so they are excluded from future write cycles. Only defer process
            # cleanup (_cleanup_after_removal) — this prevents fire-and-forget
            # remove_client calls from racing with a subsequent add_client when
            # a player is being moved between groups.
            for player, removal_reason in players_to_remove:
                if player in self.sync_clients:
                    self.sync_clients.remove(player)
                self.mass.create_task(self._cleanup_after_removal(player, reason=removal_reason))

            removed = {player for player, _ in players_to_remove}
            has_running_clients = any(x.renders_audio for x in sync_clients if x not in removed)
        for player in removed:
            if not player.renders_audio:
                # the music goes on without a member that is not heard, which is added once more
                self._rejoin_silent_member(player)
            else:
                self._lose_set_speaker(player, rejoin=True)
        return has_running_clients

    async def _write_chunk_to_player(self, airplay_player: AirPlayPlayer, chunk: bytes) -> None:
        """Write audio chunk to a player's ffmpeg process."""
        protocol_id = airplay_player.protocol_id
        # Drain any pending late-join skip first: a joiner anchored ahead of the
        # write head must drop that many leading bytes of the live feed so its
        # first delivered byte is the sample due at its anchor.
        if skip := self._client_skip_bytes.get(protocol_id, 0):
            if skip >= len(chunk):
                self._client_skip_bytes[protocol_id] = skip - len(chunk)
                return
            chunk = chunk[skip:]
            self._client_skip_bytes[protocol_id] = 0
        if ffmpeg := self._player_ffmpeg.get(protocol_id):
            if ffmpeg.closed:
                return
            # all members are fed under one lock, so one that is not heard must not hold it long
            timeout = (
                AIRPLAY_SILENT_MEMBER_TIMEOUT
                if protocol_id in self._started_silent_members
                else 35.0
            )
            await asyncio.wait_for(ffmpeg.write(chunk), timeout=timeout)

    def _replacement_expected(self) -> bool:
        """
        Return whether the queue is loading a stream that takes this session over.

        A seek or a next-track starts the new stream while the old one is still
        playing: the queue rotates its stream session at the beginning of that,
        well before the play_media carrying it arrives, and the flow stream it
        supersedes ends as soon as it notices. A flow that ends on its own -
        the queue played out, the source failed, or it broke off to be restarted
        once the player reports idle - leaves the queue between transitions.
        """
        queue_id = self.media.source_id
        session_id = get_media_session_id(self.media)
        if not queue_id or not session_id:
            return False
        queue_data = self.mass.player_queues.queue_data_or_none(queue_id)
        return (
            queue_data is not None
            and queue_data.transitioning
            and queue_data.session_id != session_id
        )

    async def _retire_player_ffmpeg(
        self, airplay_player: AirPlayPlayer, *, end_of_stream: bool
    ) -> None:
        """
        Retire a member's ffmpeg now that the source feeding it has ended.

        :param airplay_player: The member whose ffmpeg is retired.
        :param end_of_stream: Whether this ends the stream for good. The audio the
            ffmpeg still holds is then handed over and the binary's stdin closed
            behind it, which makes it play out, report eof and exit. A session a
            replacement is coming for drops that audio instead: the flush the
            replacement opens with discards it anyway, and no byte may reach the
            binary between the old ffmpeg dying and that flush.
        """
        if not (ffmpeg := self._player_ffmpeg.pop(airplay_player.protocol_id, None)):
            return
        if not end_of_stream:
            await ffmpeg.kill()
            return
        await ffmpeg.write_eof()
        await ffmpeg.wait_with_timeout(30)
        if airplay_player.stream:
            await airplay_player.stream.write_audio_eof()

    async def _end_stream_if_no_replacement_lands(self) -> None:
        """
        Deliver a withheld end of stream once no replacement is coming for it.

        This runs on the audio streamer's own task, which every route that takes
        the session over - a warm replace, a park, a stop - cancels before it
        touches the members, so getting past the wait below means no replacement
        ever claimed the session. The queue is watched rather than a fixed time
        waited out: it clears its transition on any failure between rotating its
        stream session and the play_media that carries the replacement, which
        says one is never coming, while a slow load keeps it set and must not be
        cut short. Nothing else can end this stream - without the EOF the binary
        never plays out, never reports eof, and the player keeps reporting
        playback until the user commands something else.
        """
        deadline = time.monotonic() + AIRPLAY_REPLACEMENT_EOF_TIMEOUT
        while self._replacement_expected() and (left := deadline - time.monotonic()) > 0:
            await asyncio.sleep(min(AIRPLAY_REPLACEMENT_POLL_INTERVAL, left))
        self.prov.logger.warning(
            "No replacement stream claimed the AirPlay session of %s - %s; "
            "ending it so the player can report idle",
            self.media.source_id,
            f"it is still loading one after {AIRPLAY_REPLACEMENT_EOF_TIMEOUT:.0f}s"
            if self._replacement_expected()
            else "the queue ended that transition without one",
        )
        async with self._lock:
            # A member that joined while the EOF was withheld holds a fresh
            # ffmpeg, and that process owns its own handle on the same cli
            # stdin: closing only this end would leave the pipe open and the
            # binary still waiting on it.
            for player in self.sync_clients:
                if ffmpeg := self._player_ffmpeg.pop(player.protocol_id, None):
                    await ffmpeg.kill()
            await asyncio.gather(
                *[
                    stream.write_audio_eof()
                    for player in self.sync_clients
                    if (stream := player.stream) and stream.accepts_audio
                ],
                return_exceptions=True,
            )

    async def _member_start_step(
        self, airplay_player: AirPlayPlayer, step: str, awaitable: Coroutine[Any, Any, None]
    ) -> None:
        """
        Run one per-member step of a group start, naming the member if it fails.

        A group start fans its members out over a task group and a gather, both
        of which collapse into a single exception at the caller - so with five
        speakers connecting, nothing in the log says which one failed. That,
        with whatever reason its binary reported, is the whole diagnostic.

        :param airplay_player: The member the step belongs to.
        :param step: What the member was doing, for the failure message.
        :param awaitable: The step to run.
        """
        try:
            await awaitable
        except asyncio.CancelledError:
            raise
        except Exception as err:
            self.prov.logger.warning(
                "AirPlay group start: %s failed to %s: %s",
                airplay_player.display_name,
                step,
                err,
            )
            raise

    async def _start_client(self, airplay_player: AirPlayPlayer, use_shared_ptp: bool) -> None:
        """
        Connect a CLI process and start its ffmpeg for a single client.

        :param airplay_player: The player to start streaming to.
        :param use_shared_ptp: The session-wide shared-PTP decision applied to
            this member so the whole group shares one timing source.
        """
        # joining a session supersedes any pending automatic group re-join
        airplay_player.cancel_group_rejoin()
        airplay_player.release_foreign_mute_latch()
        # Held from the decision to displace whatever is published until the new
        # process is connected and published, so a Sendspin bridge start cannot
        # put a second cli process on the same receiver in between.
        async with airplay_player.stream_spawn_lock:
            if airplay_player.stream:
                # Stopped unconditionally, not just while it reads as running: a
                # stream stops reporting that the moment its own stop() starts,
                # while its process can still be on the receiver. stop() is
                # idempotent, so this joins a teardown already under way and
                # returns at once for one that finished.
                await airplay_player.stream.stop()
            stream_pcm_format = airplay_player.get_stream_pcm_format(self.pcm_format)
            stream = AirPlayStream(airplay_player, pcm_format=stream_pcm_format)
            airplay_player.stream = stream
            stream.session = self
            await stream.connect(use_shared_ptp)
            # Wiring the audio producer to the cli stdin belongs to the same
            # claim: a displacement landing between the connect and this would
            # leave an ffmpeg feeding a process that is already gone, with
            # nothing tracking it to clean up. Pass the stream we just published
            # rather than re-reading player.stream, so a displacement cannot swap
            # it under us while the ffmpeg is being wired.
            await self._start_player_ffmpeg(airplay_player, self.media, stream)

    def _anchor_start_unix_ms(self, *, warm: bool = False, ready_at_unix_ms: int = 0) -> int:
        """
        Return the shared audible-start instant for a readiness-confirmed start.

        :param warm: True for a warm re-start over live connections (seek/next/
            resume-from-park). Members on the splice timeline report a
            minimum warm lead — their queued audio plays out before the new
            content can begin — and the shared anchor must sit beyond the
            largest member value so every member splices at the same instant.
        :param ready_at_unix_ms: Latest instant at which a member's receiver
            clock becomes usable, as the binaries reported it. The anchor never
            lands before it. 0 when no member reported a projection, leaving the
            lead below as the whole anchor.
        """
        audible = self._audible_clients
        if len(audible) == 1:
            lead_ms = AIRPLAY_START_LEAD_MS
        elif warm:
            lead_ms = AIRPLAY_GROUP_START_LEAD_MS
        else:
            # Cold group start: cover the members' receiver-side clock
            # acquisition (see AIRPLAY_COLD_GROUP_START_LEAD_MS).
            lead_ms = AIRPLAY_COLD_GROUP_START_LEAD_MS
        anchor = int(cli_clock_time(self.mass.timeline_clock) * 1000) + lead_ms
        if ready_at_unix_ms:
            anchor = max(anchor, ready_at_unix_ms + AIRPLAY_CLOCK_READY_LEAD_MS)
        if not warm:
            return anchor
        # Splice-timeline members honor the commanded instant only when that
        # instant (plus their sync offset) lands beyond their queued audio.
        # A NEGATIVE sync offset moves a member's commanded instant earlier,
        # eating into the lead, so it must be added to that member's
        # requirement — otherwise the first round can never succeed for that
        # member and every group start pays a corrective round.
        member_requirement = 0
        for player in audible:
            stream = player.stream
            if stream is None or stream.warm_lead_ms <= 0:
                continue
            adjust_ms = player.sync_offset_ms
            member_requirement = max(member_requirement, stream.warm_lead_ms - min(0, adjust_ms))
        if member_requirement > 0:
            anchor = max(
                anchor,
                int(cli_clock_time(self.mass.timeline_clock) * 1000)
                + member_requirement
                + AIRPLAY_SPLICE_LEAD_MARGIN_MS,
            )
        for player in audible:
            stream = player.stream
            if stream is None or stream.flushed_head_unix_ms <= 0:
                continue
            adjust_ms = player.sync_offset_ms
            # The member's commanded instant is anchor + adjust; it must clear
            # the member's frozen head with margin for the command round-trip.
            anchor = max(
                anchor,
                stream.flushed_head_unix_ms - adjust_ms + AIRPLAY_SPLICE_LEAD_MARGIN_MS,
            )
        return anchor

    async def _wait_members_audio_present(self) -> None:
        """
        Wait until every member's binary reports the new audio flowing.

        A binary can only report audio once it has been handed some, and a seek
        may land seconds ahead of what the source has produced. So the feed is
        waited out first and the per-member budget below measures the binary
        alone; giving up on the source here would only restart the session into
        the very same wait.
        """
        await self._wait_feed_settled()
        members = [(p, p.stream) for p in self._audible_clients if p.stream]
        results = await asyncio.gather(*[stream.wait_audio_present() for _, stream in members])
        if all(results):
            return
        # Name the members that never reported audio: they are what has to be
        # looked at, and the group start below is abandoned for all of them.
        silent = [
            player.display_name
            for (player, _), present in zip(members, results, strict=True)
            if not present
        ]
        raise PlayerCommandFailed(f"audio feed was not confirmed by {', '.join(silent)}")

    async def _wait_feed_settled(self) -> None:
        """Wait for the source to hand over its first audio, or to end without any."""
        task = self._audio_source_task
        if task is None or self._feed_settled.is_set():
            return
        settled = asyncio.create_task(self._feed_settled.wait())
        try:
            # The streamer settles the event itself, but watching the task too
            # means a feed that never even starts cannot hold this open: a task
            # cancelled before its first step never runs the finally that
            # settles the event. The timeout is the backstop for a producer that
            # neither delivers nor gives up: this runs under the player lock,
            # where every route that could stop the session waits behind it.
            await asyncio.wait(
                {settled, task},
                timeout=AIRPLAY_FEED_START_TIMEOUT,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            settled.cancel()

    async def _wait_members_clock_ready(self) -> int:
        """
        Return the latest receiver-clock readiness any member reported.

        Members that report nothing contribute no instant to the maximum; the
        caller anchors those on its lead alone.

        :return: Unix epoch ms of the latest projection any member reported, or 0
            when there is nothing to wait for — a receiver on NTP timing, or one
            that never answered. The caller then anchors on its lead alone.
        """
        # A solo start waits for the projection too: a receiver that has not
        # seated its clock renders silence at an anchor it cannot honor, and
        # enough of them need that time (WiiM, Edifier) that no start may assume
        # otherwise. It costs a warm clock nothing - the binary reports it ready
        # with a past instant right after connect - and a cold one anchors just
        # past its own projection instead of being corrected there by the binary
        # afterwards: the same instant, planned rather than repaired, with the
        # readiness lead's slack on top.
        results = await asyncio.gather(
            *[
                p.stream.wait_clock_ready(timeout=AIRPLAY_CLOCK_READY_TIMEOUT_MS / 1000)
                for p in self._audible_clients
                if p.stream
            ]
        )
        ready_at_unix_ms = max(
            (at for readiness, at in results if readiness is ClockReadiness.PROJECTED), default=0
        )
        # A group start does not drop a member that stalled - the rest of the
        # group would still be started, and the member is already warned about
        # by name, with the ports to check, where the binary reported it. Say
        # which outcomes were seen so the anchor decision is readable.
        unprojected = [
            readiness for readiness, _ in results if readiness is not ClockReadiness.PROJECTED
        ]
        if unprojected:
            self.prov.logger.debug(
                "AirPlay start: %d of %d member(s) reported no receiver clock projection (%s); "
                "anchoring those on the start lead alone",
                len(unprojected),
                len(results),
                ", ".join(sorted({readiness.value for readiness in unprojected})),
            )
        return ready_at_unix_ms

    async def _seat_member_anchors(self, position_ms: int, start_unix_ms: int) -> int:
        """
        Have every member of a cold buffered group accept an anchor before its audio leaves.

        :param position_ms: Media position mapped to the first sample of the anchor.
        :param start_unix_ms: Shared audible-start instant planned for the group.
        :return: The shared audible-start instant to command next.
        """
        streams = [player.stream for player in self.sync_clients if player.stream]
        if len(streams) < 2 or not all(stream.buffered_route for stream in streams):
            return start_unix_ms
        # A receiver can refuse its anchor until its clock has locked onto ours,
        # which the binary retries for seconds. A re-START cannot give back audio
        # a member already sent, so anchor everyone far enough out that nothing
        # has left before the slowest member accepts, then command the real
        # instant, which the members' fresh clock readiness must still allow.
        seat_unix_ms = (
            int(cli_clock_time(self.mass.timeline_clock) * 1000) + AIRPLAY_ANCHOR_SEAT_LEAD_MS
        )
        async with asyncio.TaskGroup() as task_group:
            for stream in streams:
                task_group.create_task(stream.start(seat_unix_ms, position_ms))
        floor_unix_ms = (
            int(cli_clock_time(self.mass.timeline_clock) * 1000) + AIRPLAY_GROUP_START_LEAD_MS
        )
        if ready_at_unix_ms := await self._wait_members_clock_ready():
            floor_unix_ms = max(floor_unix_ms, ready_at_unix_ms + AIRPLAY_CLOCK_READY_LEAD_MS)
        return max(start_unix_ms, floor_unix_ms)

    async def _start_members(self, position_ms: int, start_unix_ms: int) -> None:
        """
        Anchor every member's playback at one shared audible instant.

        The binaries verify the instant: each ack carries the TRUE scheduled
        instant (an infeasible one is corrected forward, never silently
        misplaced). When any member was corrected, every member is re-STARTed
        at the largest reported instant so the group converges on one shared
        instant; the recorded session anchor is always the verified truth.
        A solo member is never re-STARTed: its corrected instant is simply
        adopted as the anchor, since there is no partner to converge with.

        :param position_ms: Media position mapped to the first sample of the anchor.
        :param start_unix_ms: Shared audible-start instant in unix epoch ms.
        """
        target_ms = start_unix_ms
        corrected_ms = start_unix_ms
        for _ in range(4):
            member_tasks: list[tuple[int, asyncio.Task[int]]] = []
            async with asyncio.TaskGroup() as task_group:
                for player in self._audible_clients:
                    stream = player.stream
                    if stream is None:
                        continue
                    adjust_ms = player.sync_offset_ms
                    member_tasks.append(
                        (
                            adjust_ms,
                            task_group.create_task(
                                stream.start(target_ms + adjust_ms, position_ms)
                            ),
                        )
                    )
            corrected_ms = target_ms
            for adjust_ms, task in member_tasks:
                corrected_ms = max(corrected_ms, task.result() - adjust_ms)
            if corrected_ms <= target_ms + 2:
                break
            if len(member_tasks) == 1:
                # A lone member has no partner to converge with and its binary
                # already scheduled the corrected instant exactly, so adopt that
                # as the anchor. Re-STARTing it only does damage: the command
                # re-bases reported position on the raw position_ms (start()
                # writes that base unconditionally), throwing away the
                # correction the anchor already folded into it, and if the
                # second ack times out the session records an instant the
                # binary never played on.
                target_ms = corrected_ms
                break
            self.prov.logger.warning(
                "AirPlay group start corrected: a member could not honor %d, "
                "re-anchoring all members at %d (+%d ms)",
                target_ms,
                corrected_ms,
                corrected_ms - target_ms,
            )
            # The corrected instant already carries the binary's own
            # command-latency slack; the extra margin covers fanning the
            # retry out to every member so the next round lands.
            target_ms = corrected_ms + AIRPLAY_SPLICE_LEAD_MARGIN_MS
        else:
            # No round landed, so the members sit on the instants they last
            # reported, not on the retry that was about to be commanded. The
            # anchor has to record where they actually are, or every later
            # joiner maps against a timeline the group never played.
            target_ms = corrected_ms
            self.prov.logger.error(
                "AirPlay group start did not converge after 4 rounds "
                "(members last reported %d) - they may be audibly out of sync; "
                "please report this with a debug log",
                target_ms,
            )
        self.start_unix_ms = target_ms
        self.start_time = target_ms / 1000
        # the only place a session (re)gains a live timeline, so any park ends here
        self.parked = False
        await self._start_silent_members(position_ms, target_ms)

    async def _flush_member(self, player: AirPlayPlayer) -> bool:
        """Flush one member's live stream in place and report the binary's ack."""
        stream = player.stream
        if stream is None:
            return False
        if not player.renders_audio:
            # the members that are heard are not kept waiting long by one that is not
            with suppress(TimeoutError):
                return await asyncio.wait_for(stream.flush(), AIRPLAY_SILENT_MEMBER_TIMEOUT)
            return False
        return await stream.flush()

    def _reset_member_shifts(self) -> None:
        """Zero every member's accumulated starvation shift for a fresh anchor."""
        self._member_shift_base.clear()
        for player in self.sync_clients:
            if player.stream is not None:
                player.stream.reset_reanchor_shift()

    def _member_shift(self, player: AirPlayPlayer) -> float:
        """
        Return how far a member plays behind the session anchor after starvation shifts.

        :param player: The member to look up.
        """
        shift = self._member_shift_base.get(player.protocol_id, 0.0)
        if player.stream is not None:
            shift += player.stream.cumulative_shift_seconds
        return shift

    def _check_member_shifts(self) -> None:
        """Restart the playback when the members came out of a stall on different timelines."""
        # a seek, next or stop re-anchors (or ends) the group anyway
        if (
            len(self.sync_clients) < 2
            or not self._session_is_live()
            or self._replacement_expected()
        ):
            return
        audible = self._audible_clients
        shifts = [(player.display_name, self._member_shift(player)) for player in audible]
        spread = max(shift for _, shift in shifts) - min(shift for _, shift in shifts)
        if spread * 1000 <= AIRPLAY_STALL_SHIFT_TOLERANCE_MS:
            return
        players = [owner.player for member in audible if (owner := member.owner) and owner.player]
        if not players:
            return
        now = time.monotonic()
        if now < self._resync_not_before:
            self.prov.logger.debug(
                "AirPlay group is out of sync after a stalled audio source; restarting it "
                "in %.0fs to avoid a restart loop",
                self._resync_not_before - now,
            )
            self.mass.call_later(
                self._resync_not_before - now,
                self._check_member_shifts,
                task_id=self._shift_check_task_id,
            )
            return
        self._resync_not_before = now + self._resync_holdoff
        self._resync_holdoff = min(self._resync_holdoff * 2, AIRPLAY_STALL_RESYNC_MAX_HOLDOFF)
        # the controller passes the restart on to whichever player owns the playback
        target = next((player for player in players if not player.synced_to), players[0])
        self.prov.logger.warning(
            "AirPlay group of %s is out of sync after its audio source stalled (shifted by: "
            "%s); restarting playback",
            target.name,
            ", ".join(f"{name} {shift * 1000:.0f} ms" for name, shift in shifts),
        )
        self.mass.create_task(
            self.mass.players.cmd_resume(target.player_id),
            task_id=f"airplay_stall_resync_{id(self)}",
        )

    @property
    def _audible_clients(self) -> list[AirPlayPlayer]:
        """Return the members whose audio is heard."""
        return [player for player in self.sync_clients if player.renders_audio]

    def _join_silent_members(self, joined: AirPlayPlayer | None = None) -> None:
        """
        Add the members that are not heard and were not added yet, after those they go with.

        :param joined: A member that just joined late: the one that is not heard and goes with
            it is added again, also when it was added before.
        """
        if joined and (owner := joined.owner) and (member := owner.silent_member):
            self._silent_member_joins.pop(member.protocol_id, None)
            self._speaker_joined_at[member.protocol_id] = time.monotonic()
        for player in self._audible_clients:
            owner = player.owner
            member = owner.silent_member if owner else None
            if (
                member is not None
                and member not in self.sync_clients
                and not self._silent_member_joins.get(member.protocol_id)
                and not (
                    (pending := self._silent_member_tasks.get(member.protocol_id))
                    and not pending.done()
                )
            ):
                self._schedule_silent_member_join(member)

    def _join_failed(self, airplay_player: AirPlayPlayer, *, returning: bool) -> None:
        """
        Handle a late joiner that could not join.

        :param airplay_player: The late joiner.
        :param returning: Whether it came back into the session its set plays in.
        """
        # a set plays on what is there, but a set that joins plays on all of its speakers
        if not returning:
            self._lose_set_speaker(airplay_player, rejoin=False)

    def _lose_set_speaker(self, airplay_player: AirPlayPlayer, *, rejoin: bool) -> None:
        """
        Take the set of a speaker that was lost along, as when the stream of a speaker dies.

        A set plays on all of its speakers or not at all, so it also fails as a whole when
        one of its speakers cannot join, as its start does.

        :param airplay_player: The member that was lost.
        :param rejoin: Whether the set may be lined up to join its group again.
        """
        # the Apple TV of a set never takes its set along; the HomePod that carries a pair is
        # one of its speakers
        owner = airplay_player.owner
        if not airplay_player.renders_audio or (
            (owner is None or owner is airplay_player) and not airplay_player.speakers
        ):
            return
        if (stream := airplay_player.stream) is not None and stream.session is self:
            stream.handle_stream_loss(rejoin=rejoin)

    def _admit_silent_member(self, airplay_player: AirPlayPlayer) -> bool:
        """
        Return whether a late joiner that is not heard may stay, and count it as started.

        It may not once its set left the session while it joined.

        :param airplay_player: The late joiner, whose START was acked.
        """
        if airplay_player.renders_audio:
            return True
        if not any(endpoint in self.sync_clients for endpoint in airplay_player.endpoints):
            return False
        self._started_silent_members.add(airplay_player.protocol_id)
        return True

    def _rejoin_silent_member(self, airplay_player: AirPlayPlayer) -> None:
        """
        Add a member that is not heard once more after it left, unless it was before.

        :param airplay_player: The member that left.
        """
        if self._silent_member_joins.get(airplay_player.protocol_id, 0) < 2:
            self._schedule_silent_member_join(
                airplay_player, delay=AIRPLAY_SILENT_MEMBER_REJOIN_DELAY
            )

    def _schedule_silent_member_join(self, airplay_player: AirPlayPlayer, delay: float = 0) -> None:
        """
        Add a member that is not heard in a task of its own, so nothing waits for it.

        :param airplay_player: The member to add.
        :param delay: Seconds to wait first.
        """
        protocol_id = airplay_player.protocol_id
        pending = self._silent_member_tasks.get(protocol_id)
        if pending and not pending.done() and pending is not asyncio.current_task():
            pending.cancel()
        self._silent_member_tasks[protocol_id] = self.mass.create_task(
            self._join_silent_member(airplay_player, delay)
        )

    async def _join_silent_member(self, airplay_player: AirPlayPlayer, delay: float) -> None:
        """
        Add a member that is not heard as a late joiner while the members it goes with play.

        :param airplay_player: The member to add.
        :param delay: Seconds to wait first.
        """
        await asyncio.sleep(delay)
        # It joins once all the members it goes with play: an Apple TV resets its session
        # when its SETUP precedes the one of a speaker (measured), and a late joiner is
        # anchored on the timeline they play.
        deadline = time.monotonic() + AIRPLAY_SILENT_MEMBER_JOIN_TIMEOUT
        while (
            not self._session_is_live()
            or not all(endpoint in self.sync_clients for endpoint in airplay_player.endpoints)
        ) and time.monotonic() < deadline:
            await asyncio.sleep(AIRPLAY_SILENT_MEMBER_JOIN_POLL_INTERVAL)
        if (
            not self._session_is_live()
            or airplay_player in self.sync_clients
            or airplay_player.silent_member is not airplay_player
            or not any(endpoint in self.sync_clients for endpoint in airplay_player.endpoints)
        ):
            return
        protocol_id = airplay_player.protocol_id
        self._silent_member_joins[protocol_id] = self._silent_member_joins.get(protocol_id, 0) + 1
        await self.add_client(airplay_player)
        if airplay_player not in self.sync_clients:
            self._rejoin_silent_member(airplay_player)

    async def _start_silent_members(self, position_ms: int, start_unix_ms: int) -> None:
        """
        Anchor the members that are not heard on the instant the others start at.

        A member that does not take its anchor leaves the session and is added once more; the
        others play on.

        :param position_ms: Media position mapped to the first sample of the anchor.
        :param start_unix_ms: Audible-start instant of the other members in unix epoch ms.
        """
        members = [
            (player, stream)
            for player in self.sync_clients
            if not player.renders_audio and (stream := player.stream)
        ]
        results = await asyncio.gather(
            *[
                asyncio.wait_for(
                    stream.start(start_unix_ms + player.sync_offset_ms, position_ms),
                    AIRPLAY_SILENT_MEMBER_TIMEOUT,
                )
                for player, stream in members
            ],
            return_exceptions=True,
        )
        for (player, _), result in zip(members, results, strict=True):
            if isinstance(result, BaseException):
                # each start it misses gives it a retry of its own
                self._silent_member_joins.pop(player.protocol_id, None)
                await self.remove_silent_member(
                    player, rejoin=True, reason=f"start failed: {result!r}"
                )
            else:
                self._started_silent_members.add(player.protocol_id)

    async def _command_silent_member(self, airplay_player: AirPlayPlayer, command: str) -> bool:
        """
        Send a command to a member that is not heard; False when it did not arrive.

        :param airplay_player: The member to send the command to.
        :param command: The command for its binary.
        """
        if (stream := airplay_player.stream) is None:
            return False
        try:
            return await asyncio.wait_for(
                stream.send_cli_command(command), AIRPLAY_SILENT_MEMBER_TIMEOUT
            )
        except OSError as err:  # a timeout is an OSError too
            self.prov.logger.debug(
                "Could not send %s to %s: %s", command, airplay_player.protocol_id, err
            )
            return False

    async def _start_player_ffmpeg(
        self, player: AirPlayPlayer, media: PlayerMedia, stream: AirPlayStream
    ) -> None:
        """
        Start the per-seek ffmpeg feeding a member's persistent cli stdin.

        Retires any ffmpeg still tracked for the player and wires a fresh one to
        the same cli stdin fd. Killing ffmpeg never closes cli stdin (MA holds
        the write end via its process transport), so the binary's stdin reader
        survives a warm seek.

        :param player: The member whose ffmpeg is (re)started.
        :param media: Media whose queue/session identify the output plan.
        :param stream: The published stream to wire the ffmpeg into, resolved by
            the caller so an await here cannot swap player.stream under us.
        """
        if ffmpeg := self._player_ffmpeg.pop(player.protocol_id, None):
            await ffmpeg.close()
        handoff_format = stream.pcm_format
        if player.renders_audio:
            filter_params = self.mass.streams.audio.get_player_output_plan(
                player.output_plan_protocol_id,
                input_format=self.pcm_format,
                output_format=get_final_output_format(handoff_format),
                handoff_format=handoff_format,
                queue_id=media.source_id,
                session_id=get_media_session_id(media),
            ).filter_params
        else:
            # what an Apple TV of a set is fed can not be heard, so it is fed silence
            filter_params = ["volume=0"]
        cli_proc = stream._cli_proc
        assert cli_proc
        assert cli_proc.proc
        assert cli_proc.proc.stdin
        stdin_transport = cli_proc.proc.stdin.transport
        audio_output: str | int = stdin_transport.get_extra_info("pipe").fileno()
        ffmpeg = FFMpeg(
            audio_input="-",
            input_format=self.pcm_format,
            output_format=handoff_format,
            filter_params=filter_params,
            audio_output=audio_output,
        )
        await ffmpeg.start()
        self._player_ffmpeg[player.protocol_id] = ffmpeg


def _first_music_assistant_error(err: BaseException) -> MusicAssistantError | None:
    """
    Return the first MusicAssistantError inside an error or (nested) exception group.

    :param err: The error to inspect.
    """
    if isinstance(err, MusicAssistantError):
        return err
    if isinstance(err, BaseExceptionGroup):
        for nested in err.exceptions:
            if (found := _first_music_assistant_error(nested)) is not None:
                return found
    return None
