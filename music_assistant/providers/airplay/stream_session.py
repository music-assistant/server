"""Unified AirPlay/RAOP stream session logic for AirPlay devices."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator, Coroutine
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import ContentType, PlaybackState
from music_assistant_models.errors import MusicAssistantError, PlayerCommandFailed

from music_assistant.constants import CONF_SYNC_ADJUST
from music_assistant.controllers.streams.audio_processing import get_media_session_id
from music_assistant.helpers.ffmpeg import FFMpeg

from .constants import (
    AIRPLAY_CLOCK_READY_LEAD_MS,
    AIRPLAY_CLOCK_READY_TIMEOUT_MS,
    AIRPLAY_COLD_GROUP_START_LEAD_MS,
    AIRPLAY_GROUP_START_LEAD_MS,
    AIRPLAY_LATE_JOIN_MIN_HEADROOM_MS,
    AIRPLAY_LATE_JOIN_RING_MARGIN_SECONDS,
    AIRPLAY_LATE_JOIN_RING_MAX_BYTES,
    AIRPLAY_LATE_JOIN_RING_MIN_SECONDS,
    AIRPLAY_SPLICE_LEAD_MARGIN_MS,
    AIRPLAY_START_LEAD_MS,
    ClockReadiness,
    StreamingProtocol,
)
from .helpers import get_final_output_format
from .stream import AirPlayStream

if TYPE_CHECKING:
    from music_assistant_models.media_items import AudioFormat

    from music_assistant.models.player import PlayerMedia

    from .player import AirPlayPlayer
    from .provider import AirPlayProvider

# What each readiness outcome means for the join anchor. They all end up
# anchoring on the join floor, so only the reason distinguishes a device that
# needs longer from one that will not play at all.
_CLOCK_READINESS_NOTES: dict[ClockReadiness, str] = {
    ClockReadiness.PROJECTED: "usable in {out:.2f}s; anchoring just past that",
    ClockReadiness.NOT_APPLICABLE: "runs on NTP timing, so there is none to wait for; "
    "anchoring on the join floor",
    ClockReadiness.STALLED: "never answered ours; anchoring on the join floor",
    ClockReadiness.UNREPORTED: "was not reported within {timeout:.1f}s (a slow device, or a "
    "binary too old to report it); anchoring on the join floor",
}


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

    @property
    def effective_start_time(self) -> float:
        """
        Return the session anchor adjusted for the reference member's clock shift.

        ``start_time`` is the wall-clock anchor set at the last group (re)start.
        A member's cliairplay can re-anchor its playout LATER after recovering
        from a PCM starvation, shifting the group's real timeline; that shift is
        tracked per member. The first sync client is taken as the reference and
        its accumulated shift is added, so a late joiner maps its first sample to
        where the group actually plays. Divergent per-member shifts make any
        single reference imperfect; the current first client is the best
        available choice and the reference transfers to the new first client when
        the old one is removed.
        """
        if not self.sync_clients:
            return self.start_time
        reference_stream = self.sync_clients[0].stream
        if reference_stream is None:
            return self.start_time
        return self.start_time + reference_stream.cumulative_shift_seconds

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
            await self._start_members(
                position_ms, self._anchor_start_unix_ms(ready_at_unix_ms=ready_at_unix_ms)
            )
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
        format and a connected stream on every member; anything else takes the
        cold path.
        """
        if {p.player_id for p in sync_clients} != {p.player_id for p in self.sync_clients}:
            return False
        if (
            pcm_format.sample_rate != self.pcm_format.sample_rate
            or pcm_format.bit_depth != self.pcm_format.bit_depth
        ):
            return False
        return all(
            p.stream is not None and p.stream.running and p.stream.connected
            for p in self.sync_clients
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
            for player in self.sync_clients:
                if ffmpeg := self._player_ffmpeg.pop(player.player_id, None):
                    await ffmpeg.kill()
            flushed = await asyncio.gather(
                *[self._flush_member(player) for player in self.sync_clients]
            )
            if not all(flushed):
                raise PlayerCommandFailed("warm flush was not acknowledged")
            for player in self.sync_clients:
                await self._start_player_ffmpeg(player, media)
            self.media = media
            # The stream position counter and the late-join prime buffer both
            # describe the OLD timeline; restart them before the new source pumps.
            self.seconds_streamed = 0
            self._pcm_total_fed = 0
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
        except asyncio.CancelledError:
            raise
        except Exception as err:
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
        Returns False when any member lacks a running, connected stream or its
        standby command cannot be delivered so the caller can fall back to a
        full stop.
        """
        if not all(
            p.stream is not None and p.stream.running and p.stream.connected
            for p in self.sync_clients
        ):
            return False
        if self._audio_source_task and not self._audio_source_task.done():
            self._audio_source_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._audio_source_task
        for player in self.sync_clients:
            if ffmpeg := self._player_ffmpeg.pop(player.player_id, None):
                await ffmpeg.kill()
            stream = player.stream
            assert stream
            try:
                command_delivered = await stream.send_cli_command("ACTION=STANDBY")
            except Exception as err:
                self.prov.logger.warning(
                    "Could not park AirPlay player %s: %s", player.player_id, err
                )
                return False
            if not command_delivered:
                self.prov.logger.warning(
                    "Could not park AirPlay player %s: standby command was not delivered",
                    player.player_id,
                )
                return False
            player.set_state_from_stream(state=PlaybackState.PAUSED, stream=stream)
        # a parked session has no live timeline; the resume re-anchors it
        self.seconds_streamed = 0
        self._pcm_total_fed = 0
        self._pcm_buffer.clear()
        self._client_skip_bytes.clear()
        self._reset_member_shifts()
        return True

    async def stop(self) -> None:
        """Stop playback and cleanup."""
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
            airplay_player.player_id,
            reason,
        )
        self._client_skip_bytes.pop(airplay_player.player_id, None)
        ffmpeg = self._player_ffmpeg.pop(airplay_player.player_id, None)
        # note that we use kill instead of graceful close here,
        # because otherwise it can take a very long time for the process to exit.
        if ffmpeg and not ffmpeg.closed:
            await ffmpeg.kill()
        if airplay_player.stream and airplay_player.stream.session == self:
            airplay_player.stream.reset_reanchor_shift()
            await airplay_player.stream.stop(force=True)

    async def add_client(self, airplay_player: AirPlayPlayer) -> None:  # noqa: PLR0915
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
        except Exception as err:
            self.prov.logger.warning(
                "Late joiner %s: failed to connect pipeline: %s",
                airplay_player.player_id,
                err,
            )
            await self.stop_client(airplay_player, reason="late joiner connection failed")
            return

        if readiness is ClockReadiness.STALLED:
            # The receiver never answered our clock, so it would render silence
            # for as long as it stayed in the group while every other signal
            # said it was playing. Better to leave it out: the group keeps
            # playing and the player stays idle, which is the visible truth.
            # The binary's own report names the device and the ports to check.
            self.prov.logger.warning(
                "Late joiner %s: not adding it to the group - its receiver never answered "
                "the server's PTP clock, so it would render silence",
                airplay_player.player_id,
            )
            await self.stop_client(airplay_player, reason="receiver clock stalled")
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
                        airplay_player.player_id,
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
                    lead = self.seconds_streamed - (time.time() - effective_start_time)
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
                            airplay_player.player_id,
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
                            airplay_player.player_id,
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

        now = time.time()
        self.prov.logger.debug(
            "Late joiner %s: receiver clock %s",
            airplay_player.player_id,
            _CLOCK_READINESS_NOTES.get(readiness, "").format(
                out=ready_at_unix_ms / 1000 - now,
                timeout=AIRPLAY_CLOCK_READY_TIMEOUT_MS / 1000,
            ),
        )
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
            sync_adjust = airplay_player.config.get_value(CONF_SYNC_ADJUST, 0)
            adjust_ms = sync_adjust if isinstance(sync_adjust, int) else 0
            position_ms = int(((self.media.elapsed_time or 0) + fed_pos_due) * 1000)

        try:
            # Anchor the joiner BEFORE feeding the prime: pre-START the binary
            # only buffers stdin into its bounded ring and sends nothing, so a
            # prime longer than that ring would wedge that write — and, through
            # the session lock, stall the whole group's feed. Anchored, the
            # binary drains the prime as it streams in. The START does not
            # re-anchor the session timeline (the group keeps playing). Its ack
            # is awaited WITHOUT the session lock: the binary holds that ack
            # until the receiver's clock readiness resolves, which would
            # otherwise starve every other member's feed for that whole wait.
            actual = await stream.start(start_unix_ms + adjust_ms, position_ms, join=True)
        except asyncio.CancelledError:
            await self.stop_client(airplay_player, reason="late joiner start cancelled")
            raise
        except Exception as err:
            self.prov.logger.warning(
                "Late joiner %s: failed to start/prime pipeline: %r",
                airplay_player.player_id,
                err,
            )
            await self.stop_client(airplay_player, reason="late joiner start/prime failed")
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
                # Map the content onto the instant the binary acked — its
                # verified truth — falling back to the commanded instant when no
                # ack arrived (older binary). The ring is re-snapshotted here, so
                # the mapping covers whatever the group was fed while the ack was
                # outstanding.
                acked_at = (actual - adjust_ms) / 1000 if actual is not None else requested_at
                start_at, fed_pos_due, prime, skip_bytes = map_to(acked_at, committed=True)
                self._client_skip_bytes[airplay_player.player_id] = skip_bytes
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
                    airplay_player.player_id,
                    len(prime) / pcm_sample_size,
                    skip_bytes / pcm_sample_size,
                    self.seconds_streamed,
                    fed_pos_due,
                    start_at - time.time(),
                    int(acked_at * 1000) - start_unix_ms,
                    min_headroom,
                    self.effective_start_time - self.start_time,
                    self.seconds_streamed - (time.time() - self.effective_start_time),
                    self._peak_lead_seconds,
                    self._pcm_buffer_max / pcm_sample_size,
                )

                if prime:
                    try:
                        await self._write_chunk_to_player(airplay_player, prime)
                    except Exception as err:
                        self.prov.logger.warning(
                            "Late joiner %s: failed to start/prime pipeline: %r",
                            airplay_player.player_id,
                            err,
                        )
                        await self.stop_client(
                            airplay_player, reason="late joiner start/prime failed"
                        )
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
            airplay_player.player_id,
            time.time() - now,
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
        now = time.time()
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
        """Return whether the session still has a running reference member (call under the lock)."""
        if not self.sync_clients:
            return False
        reference_stream = self.sync_clients[0].stream
        return reference_stream is not None and reference_stream.running

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
        # Re-check sync_clients under the lock to avoid racing with add_client.
        async with self._lock:
            should_stop = not self.sync_clients
        if should_stop:
            await self.stop()

    async def _audio_streamer(self, audio_source: AsyncGenerator[bytes]) -> None:
        """Stream audio to all players."""
        stream_error: BaseException | None = None
        try:
            async for chunk in audio_source:
                if not self.sync_clients:
                    break

                has_running_clients = await self._write_chunk_to_all_players(chunk)
                if not has_running_clients:
                    self.prov.logger.debug("No running clients remaining, stopping audio streamer")
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
            if stream_error:
                self.prov.logger.warning(
                    "Stream ended prematurely due to error - notifying players"
                )
        async with self._lock:
            await asyncio.gather(
                *[
                    self._write_eof_to_player(x)
                    for x in self.sync_clients
                    if x.stream and x.stream.running
                ],
                return_exceptions=True,
            )

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
                        player.player_id,
                    )
                    players_to_remove.append((player, "audio write timeout"))
                elif isinstance(result, Exception):
                    self.prov.logger.warning(
                        "Removing player %s from session due to write error: %s",
                        player.player_id,
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

            remaining_clients = len(sync_clients) - len(players_to_remove)
            return remaining_clients > 0

    async def _write_chunk_to_player(self, airplay_player: AirPlayPlayer, chunk: bytes) -> None:
        """Write audio chunk to a player's ffmpeg process."""
        player_id = airplay_player.player_id
        # Drain any pending late-join skip first: a joiner anchored ahead of the
        # write head must drop that many leading bytes of the live feed so its
        # first delivered byte is the sample due at its anchor.
        if skip := self._client_skip_bytes.get(player_id, 0):
            if skip >= len(chunk):
                self._client_skip_bytes[player_id] = skip - len(chunk)
                return
            chunk = chunk[skip:]
            self._client_skip_bytes[player_id] = 0
        if ffmpeg := self._player_ffmpeg.get(player_id):
            if ffmpeg.closed:
                return
            await asyncio.wait_for(ffmpeg.write(chunk), timeout=35.0)

    async def _write_eof_to_player(self, airplay_player: AirPlayPlayer) -> None:
        """Write EOF to a specific player."""
        if ffmpeg := self._player_ffmpeg.pop(airplay_player.player_id, None):
            await ffmpeg.write_eof()
            await ffmpeg.wait_with_timeout(30)
            if airplay_player.stream:
                await airplay_player.stream.write_audio_eof()

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
        # sync volume from parent player if needed
        airplay_player.sync_volume_level()
        if airplay_player.stream and airplay_player.stream.running:
            await airplay_player.stream.stop()
        stream_pcm_format = airplay_player.get_stream_pcm_format(self.pcm_format)
        airplay_player.stream = AirPlayStream(airplay_player, pcm_format=stream_pcm_format)
        airplay_player.stream.session = self
        await airplay_player.stream.connect(use_shared_ptp)
        await self._start_player_ffmpeg(airplay_player, self.media)

    def _anchor_start_unix_ms(self, *, warm: bool = False, ready_at_unix_ms: int = 0) -> int:
        """
        Return the shared audible-start instant for a readiness-confirmed start.

        :param warm: True for a warm re-start over live connections (seek/next/
            resume-from-park). Members on the Apple splice timeline report a
            minimum warm lead — their queued audio plays out before the new
            content can begin — and the shared anchor must sit beyond the
            largest member value so every member splices at the same instant.
        :param ready_at_unix_ms: Latest instant at which a member's receiver
            clock becomes usable, as the binaries reported it. The anchor never
            lands before it. 0 when no member reported a projection, leaving the
            lead below as the whole anchor.
        """
        if len(self.sync_clients) == 1:
            lead_ms = AIRPLAY_START_LEAD_MS
        elif warm:
            lead_ms = AIRPLAY_GROUP_START_LEAD_MS
        else:
            # Cold group start: cover the members' receiver-side clock
            # acquisition (see AIRPLAY_COLD_GROUP_START_LEAD_MS).
            lead_ms = AIRPLAY_COLD_GROUP_START_LEAD_MS
        anchor = int(time.time() * 1000) + lead_ms
        if ready_at_unix_ms:
            anchor = max(anchor, ready_at_unix_ms + AIRPLAY_CLOCK_READY_LEAD_MS)
        if not warm:
            return anchor
        # Splice-timeline members honor the commanded instant only when that
        # instant (plus their sync_adjust) lands beyond their queued audio.
        # A NEGATIVE sync_adjust moves a member's commanded instant earlier,
        # eating into the lead, so it must be added to that member's
        # requirement — otherwise the first round can never succeed for that
        # member and every group start pays a corrective round.
        member_requirement = 0
        for player in self.sync_clients:
            stream = player.stream
            if stream is None or stream.warm_lead_ms <= 0:
                continue
            sync_adjust = player.config.get_value(CONF_SYNC_ADJUST, 0)
            adjust_ms = sync_adjust if isinstance(sync_adjust, int) else 0
            member_requirement = max(member_requirement, stream.warm_lead_ms - min(0, adjust_ms))
        if member_requirement > 0:
            anchor = max(
                anchor,
                int(time.time() * 1000) + member_requirement + AIRPLAY_SPLICE_LEAD_MARGIN_MS,
            )
        for player in self.sync_clients:
            stream = player.stream
            if stream is None or stream.flushed_head_unix_ms <= 0:
                continue
            sync_adjust = player.config.get_value(CONF_SYNC_ADJUST, 0)
            adjust_ms = sync_adjust if isinstance(sync_adjust, int) else 0
            # The member's commanded instant is anchor + adjust; it must clear
            # the member's frozen head with margin for the command round-trip.
            anchor = max(
                anchor,
                stream.flushed_head_unix_ms - adjust_ms + AIRPLAY_SPLICE_LEAD_MARGIN_MS,
            )
        return anchor

    async def _wait_members_audio_present(self) -> None:
        """Wait until every member's binary reports the new audio flowing."""
        members = [(p, p.stream) for p in self.sync_clients if p.stream]
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

    async def _wait_members_clock_ready(self) -> int:
        """
        Return the latest receiver-clock readiness any member reported.

        Members that report nothing are not represented in the result; the
        caller anchors those on its lead alone.

        :return: Unix epoch ms of the latest projection any member reported, or 0
            when there is nothing to wait for — a solo start, a receiver on NTP
            timing, one that never answered, or a binary too old to report. The
            caller then anchors on its lead alone.
        """
        if len(self.sync_clients) == 1:
            # A lone receiver seats a fresh session by itself within ~20 ms and
            # has no partner to be late against, so waiting only delays it. The
            # instant matters once members have to agree on one.
            return 0
        results = await asyncio.gather(
            *[
                p.stream.wait_clock_ready(timeout=AIRPLAY_CLOCK_READY_TIMEOUT_MS / 1000)
                for p in self.sync_clients
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

    async def _start_members(self, position_ms: int, start_unix_ms: int) -> None:
        """
        Anchor every member's playback at one shared audible instant.

        The binaries verify the instant: each ack carries the TRUE scheduled
        instant (an infeasible one is corrected forward, never silently
        misplaced). When any member was corrected, every member is re-STARTed
        at the largest reported instant so the group converges on one shared
        instant; the recorded session anchor is always the verified truth.

        :param position_ms: Media position mapped to the first sample of the anchor.
        :param start_unix_ms: Shared audible-start instant in unix epoch ms.
        """
        target_ms = start_unix_ms
        corrected_ms = start_unix_ms
        for _ in range(4):
            member_tasks: list[tuple[int, asyncio.Task[int | None]]] = []
            async with asyncio.TaskGroup() as task_group:
                for player in self.sync_clients:
                    stream = player.stream
                    if stream is None:
                        continue
                    sync_adjust = player.config.get_value(CONF_SYNC_ADJUST, 0)
                    adjust_ms = sync_adjust if isinstance(sync_adjust, int) else 0
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
                actual = task.result()
                if actual is None:
                    continue  # older binary without a started ack
                corrected_ms = max(corrected_ms, actual - adjust_ms)
            if corrected_ms <= target_ms + 2:
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

    async def _flush_member(self, player: AirPlayPlayer) -> bool:
        """Flush one member's live stream in place and report the binary's ack."""
        stream = player.stream
        if stream is None:
            return False
        return await stream.flush()

    def _reset_member_shifts(self) -> None:
        """Zero every member's accumulated starvation shift for a fresh anchor."""
        for player in self.sync_clients:
            if player.stream is not None:
                player.stream.reset_reanchor_shift()

    async def _start_player_ffmpeg(self, player: AirPlayPlayer, media: PlayerMedia) -> None:
        """
        Start the per-seek ffmpeg feeding a member's persistent cli stdin.

        Retires any ffmpeg still tracked for the player and wires a fresh one to
        the same cli stdin fd. Killing ffmpeg never closes cli stdin (MA holds
        the write end via its process transport), so the binary's stdin reader
        survives a warm seek.

        :param player: The member whose ffmpeg is (re)started.
        :param media: Media whose queue/session identify the output plan.
        """
        if ffmpeg := self._player_ffmpeg.pop(player.player_id, None):
            await ffmpeg.close()
        stream = player.stream
        assert stream
        handoff_format = stream.pcm_format
        output_plan = self.mass.streams.audio.get_player_output_plan(
            player.player_id,
            input_format=self.pcm_format,
            output_format=get_final_output_format(handoff_format),
            handoff_format=handoff_format,
            queue_id=media.source_id,
            session_id=get_media_session_id(media),
        )
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
            filter_params=output_plan.filter_params,
            audio_output=audio_output,
        )
        await ffmpeg.start()
        self._player_ffmpeg[player.player_id] = ffmpeg


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
