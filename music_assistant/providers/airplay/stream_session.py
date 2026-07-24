"""Unified AirPlay/RAOP stream session logic for AirPlay devices."""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncGenerator
from contextlib import suppress
from typing import TYPE_CHECKING

from music_assistant_models.enums import PlaybackState
from music_assistant_models.errors import PlayerCommandFailed

from music_assistant.constants import CONF_SYNC_ADJUST
from music_assistant.controllers.streams.audio_processing import get_media_session_id
from music_assistant.helpers.ffmpeg import FFMpeg

from .constants import AIRPLAY_LATE_JOIN_MIN_HEADROOM_MS, StreamingProtocol
from .helpers import get_final_output_format
from .stream import AirPlayStream

if TYPE_CHECKING:
    from music_assistant_models.media_items import AudioFormat

    from music_assistant.models.player import PlayerMedia

    from .player import AirPlayPlayer
    from .provider import AirPlayProvider


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
        self.wait_start: float = 0.0
        self.seconds_streamed: float = 0
        # Timing source for the whole session, decided once in start() and applied
        # identically to every native AirPlay 2 member (and any late joiner) so a
        # sync group can never mix shared-PTP and NTP members.
        self.use_shared_ptp: bool = False
        self._shared_ptp_resolved = False
        # A lone native AirPlay 2 player negotiates the clock honestly and may
        # yield the timeline to a master-or-mute receiver (Samsung); groups
        # keep the sender-owned shared timeline.
        self._ptp_follow = False
        self._ptp_degraded_warning_logged = False
        # Raw PCM ring buffer for late joiners. When a late joiner arrives we
        # send this buffer to prime its pipeline so it starts playing quickly
        # instead of waiting for the full pipeline to fill from scratch.
        # Must cover how far the feed runs ahead of the audible position
        # (the binary's ring cap of latency+2s plus the ~2s receiver buffer)
        # with enough slack left to prime the joiner.
        self._pcm_buffer = bytearray()
        self._pcm_buffer_max = pcm_format.pcm_sample_size * 10  # 10 seconds

    async def start(self, audio_source: AsyncGenerator[bytes]) -> None:
        """Initialize stream session for all players."""
        ap2_members = sum(1 for p in self.sync_clients if p.protocol == StreamingProtocol.AIRPLAY2)
        self._ptp_follow = ap2_members == 1 and len(self.sync_clients) == 1
        if ap2_members and not self._ptp_follow:
            # Resolve the timing source before calculating the audible anchor so
            # a bounded daemon-readiness wait cannot consume the setup lead.
            self.use_shared_ptp = await self._resolve_shared_ptp(ap2_members)
            self._shared_ptp_resolved = True
        self.wait_start = max(p.wait_start for p in self.sync_clients) / 1000
        position_ms = int((self.media.elapsed_time or 0) * 1000)
        generations = {player.player_id: 0 for player in self.sync_clients}
        try:
            async with asyncio.TaskGroup() as task_group:
                for player in self.sync_clients:
                    task_group.create_task(self._start_client(player, self.use_shared_ptp))
            await asyncio.gather(
                *[p.stream.wait_for_connection() for p in self.sync_clients if p.stream]
            )
            async with asyncio.TaskGroup() as task_group:
                for player in self.sync_clients:
                    stream = player.stream
                    assert stream
                    task_group.create_task(stream.prepare_generation(0, "-", position_ms))
            self._audio_source_task = asyncio.create_task(self._audio_streamer(audio_source))
            primed = await asyncio.gather(
                *[p.stream.wait_generation_primed(0) for p in self.sync_clients if p.stream]
            )
            if not all(primed):
                raise PlayerCommandFailed("generation prime timeout")
            await self._commit_generations(generations, position_ms)
        except asyncio.CancelledError:
            await self.stop()
            raise
        except Exception as err:
            # playback failed to start, cleanup
            await self.stop()
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

        Each member stages the new source as its next generation on a fresh
        pipe while the old audio keeps playing; once every member reports
        primed, one shared START commits the switch (warm flush + re-anchor)
        a few hundred milliseconds later. Returns False when anything fails,
        so the caller can fall back to the cold path.
        """
        position_ms = int((media.elapsed_time or 0) * 1000)
        generations: dict[str, int] = {}
        fifos: dict[str, str] = {}
        try:
            # stage the new generation on every member (old audio unaffected)
            for player in self.sync_clients:
                stream = player.stream
                assert stream
                gen = stream.next_generation()
                fifo = f"/tmp/ma-gen-{player.player_id}-{gen}.pcm"  # noqa: S108
                with suppress(FileNotFoundError):
                    await asyncio.to_thread(os.unlink, fifo)
                await asyncio.to_thread(os.mkfifo, fifo)
                generations[player.player_id] = gen
                fifos[player.player_id] = fifo
                await stream.prepare_generation(gen, fifo, position_ms)

            # swap the source: retire the old flow + per-player DSP ffmpegs,
            # spawn fresh ones writing into the generation pipes
            if self._audio_source_task and not self._audio_source_task.done():
                self._audio_source_task.cancel()
                with suppress(asyncio.CancelledError):
                    await self._audio_source_task
            for player in self.sync_clients:
                await self._start_generation_ffmpeg(player, fifos[player.player_id], media)
            self.media = media
            # The stream position counter and the late-join prime buffer both
            # describe the OLD generation's timeline; restart them before the
            # new source starts pumping.
            self.seconds_streamed = 0
            self._pcm_buffer.clear()
            self._audio_source_task = asyncio.create_task(self._audio_streamer(audio_source))

            primed = await asyncio.gather(
                *[
                    p.stream.wait_generation_primed(generations[p.player_id])
                    for p in self.sync_clients
                    if p.stream
                ]
            )
            if not all(primed):
                raise PlayerCommandFailed("generation prime timeout")
            await self._commit_generations(generations, position_ms)
        except Exception as err:
            self.prov.logger.warning(
                "Warm replacement failed (%r); falling back to a cold restart", err
            )
            return False
        finally:
            for fifo in fifos.values():
                # Unblock any writer-open still pending on a failed pipe by
                # briefly opening the read side, then remove the pipe node.
                with suppress(OSError):
                    os.close(os.open(fifo, os.O_RDONLY | os.O_NONBLOCK))
                with suppress(OSError):
                    await asyncio.to_thread(os.unlink, fifo)
        return True

    async def standby(self) -> bool:
        """
        Park the session: stall every member but keep the connections alive.

        The next play_media (resume or seek) commits a new generation over the
        live connections — the same coordinated warm restart as seek/next.
        Returns False when any member lacks a running, connected stream so the
        caller can fall back to a full stop.
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
        self._pcm_buffer.clear()
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
        ffmpeg = self._player_ffmpeg.pop(airplay_player.player_id, None)
        # note that we use kill instead of graceful close here,
        # because otherwise it can take a very long time for the process to exit.
        if ffmpeg and not ffmpeg.closed:
            await ffmpeg.kill()
        if airplay_player.stream and airplay_player.stream.session == self:
            await airplay_player.stream.stop(force=True)

    async def add_client(self, airplay_player: AirPlayPlayer) -> None:  # noqa: PLR0915
        """
        Add a sync client to the session as a late joiner.

        Uses the PCM ring buffer to prime the late joiner's pipeline so it
        starts playing quickly. Timing-source readiness is resolved before the
        session lock; buffer calculations and writes stay under the lock so
        ``seconds_streamed`` and the buffer remain consistent.

        Devices generally cannot honour a start anchor that is in the
        past — they just play whatever the pipe gives them, trailing the
        group by the deficit. To stay in sync we therefore push the late
        joiner's ``start_at`` into the future (at least ``wait_start`` ahead
        of now) and trim the corresponding amount from the head of the
        buffered PCM, so the first sample we send maps to the correct future
        stream position.

        1. Connect the new client without starting playback.
        2. Snapshot the ring buffer and map its first byte to its stream
           position; if the
           resulting start anchor is in the past, shift it forward to
           ``now + min_headroom`` and trim that many seconds from the buffer
           head so timing stays aligned with the rest of the group.
        3. Prepare generation 0, write the (possibly trimmed) buffer, and add
           the client so the live stream continues priming it.
        4. Start generation 0 only after the client reports primed.
        """
        if self._ptp_follow:
            # A group needs the sender-owned timeline; the solo session
            # yielded its clock to the receiver, so restart as a group.
            await self._regroup_from_follow()
            return
        await self._resolve_late_joiner_ptp(airplay_player)
        async with self._lock:
            if not self.sync_clients:
                return
            first_client = self.sync_clients[0]
            if not first_client.stream or not first_client.stream.running:
                return
        try:
            await self._start_client(airplay_player, self.use_shared_ptp)
            stream = airplay_player.stream
            assert stream
            await stream.wait_for_connection()
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

        async with self._lock:
            if not self.sync_clients:
                await self.stop_client(airplay_player, reason="session ended during late join")
                return
            first_client = self.sync_clients[0]
            if not first_client.stream or not first_client.stream.running:
                await self.stop_client(airplay_player, reason="session ended during late join")
                return
            now = time.time()
            pcm_sample_size = self.pcm_format.pcm_sample_size
            frame_size = (self.pcm_format.bit_depth // 8) * self.pcm_format.channels
            # Snapshot the buffer and calculate its duration
            buffered_pcm = bytes(self._pcm_buffer)
            buffer_seconds = len(buffered_pcm) / pcm_sample_size
            # The first byte in the buffer corresponds to this stream position
            first_byte_pos = self.seconds_streamed - buffer_seconds
            first_sample_pos = first_byte_pos
            start_at = self.start_time + first_byte_pos

            # The audio we hand to ffmpeg → cliairplay must be bit-aligned with
            # ``start_at``: the first sample sent should be the one that should
            # play at ``start_at``. cliairplay buffers ``wait_start`` seconds of
            # audio before starting playback, so we keep ``start_at`` at least
            # that far in the future (using the larger of the session's
            # existing wait_start, the late joiner's own and the late-join
            # floor, which is more conservative than the plain session lead).
            min_headroom = max(
                self.wait_start,
                airplay_player.wait_start / 1000,
                AIRPLAY_LATE_JOIN_MIN_HEADROOM_MS / 1000,
            )
            target_start_at = now + min_headroom
            trim_seconds = 0.0
            if start_at < target_start_at:
                # Shift start_at forward so the device has time to prep, and
                # trim the buffer head by the same amount so the first sample
                # we send is the one that should play at the new start_at.
                trim_seconds = target_start_at - start_at
                trim_bytes = int(trim_seconds * pcm_sample_size)
                trim_bytes -= trim_bytes % frame_size
                if trim_bytes >= len(buffered_pcm):
                    # Nothing left of the buffer after the trim. Drop it and
                    # let the audio_streamer feed the late joiner from the
                    # next chunk. Set start_at to the current live position;
                    # the clamp below may still move it later to preserve
                    # the required headroom.
                    buffered_pcm = b""
                    first_sample_pos = self.seconds_streamed
                    start_at = self.start_time + self.seconds_streamed
                else:
                    buffered_pcm = buffered_pcm[trim_bytes:]
                    # Advance start_at by exactly the trimmed duration so the
                    # NTP anchor matches the first sample we actually send.
                    trimmed_seconds = trim_bytes / pcm_sample_size
                    first_sample_pos += trimmed_seconds
                    start_at += trimmed_seconds
                # Make sure start_at still leaves enough lead time for slow
                # connections / very-far-behind joiners.
                start_at = max(start_at, target_start_at)

            start_unix_ms = int(start_at * 1000)
            position_ms = int(((self.media.elapsed_time or 0) + first_sample_pos) * 1000)

            self.prov.logger.debug(
                "Late joiner %s: sending %.2fs of buffered audio, "
                "stream_pos=%.2fs, first_byte_pos=%.2fs, "
                "start_at is %.2fs from now (min_headroom=%.2fs, trimmed=%.2fs)",
                airplay_player.player_id,
                len(buffered_pcm) / pcm_sample_size,
                self.seconds_streamed,
                first_byte_pos,
                start_at - now,
                min_headroom,
                trim_seconds,
            )

            try:
                await stream.prepare_generation(0, "-", position_ms)
                if buffered_pcm:
                    await self._write_chunk_to_player(airplay_player, buffered_pcm)
            except asyncio.CancelledError:
                await self.stop_client(airplay_player, reason="late joiner prepare cancelled")
                raise
            except Exception as err:
                self.prov.logger.warning(
                    "Late joiner %s: failed to start/prime pipeline: %s",
                    airplay_player.player_id,
                    err,
                )
                await self.stop_client(airplay_player, reason="late joiner start/prime failed")
                return

            if airplay_player not in self.sync_clients:
                self.sync_clients.append(airplay_player)
            if airplay_player.protocol == StreamingProtocol.AIRPLAY2 and not self.use_shared_ptp:
                ap2_members = sum(
                    1
                    for player in self.sync_clients
                    if player.protocol == StreamingProtocol.AIRPLAY2
                )
                self._warn_degraded_shared_ptp(ap2_members)

        try:
            if not await stream.wait_generation_primed(0):
                raise PlayerCommandFailed("generation prime timeout")
            await self._commit_generations(
                {airplay_player.player_id: 0},
                position_ms,
                start_unix_ms,
                reanchor_session=False,
            )
        except asyncio.CancelledError:
            await self.remove_client(airplay_player, reason="late joiner prime cancelled")
            raise
        except Exception as err:
            self.prov.logger.warning(
                "Late joiner %s: failed to prime generation: %s",
                airplay_player.player_id,
                err,
            )
            await self.remove_client(airplay_player, reason="late joiner prime failed")
            return
        self.prov.logger.debug(
            "Late joiner %s: device primed after %.2fs",
            airplay_player.player_id,
            time.time() - now,
        )

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

    async def _regroup_from_follow(self) -> None:
        """
        Restart a yielded (receiver-clocked) solo session as a proper group.

        The late joiner is already in the leader's group_members, so the
        queue's resume rebuilds playback as a sender-clocked group including
        it. The resume runs as a task because the caller holds the leader's
        lock (resume ends in play_media, which takes the same lock).
        """
        queue_id = self.media.source_id
        await self.stop()
        if queue_id:
            self.mass.create_task(self.mass.player_queues.resume(queue_id, fade_in=False))

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
            self.seconds_streamed += len(chunk) / self.pcm_format.pcm_sample_size
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

    async def _start_client(self, airplay_player: AirPlayPlayer, use_shared_ptp: bool) -> None:
        """
        Connect a CLI process and start ffmpeg for a single client.

        :param airplay_player: The player to start streaming to.
        :param use_shared_ptp: The session-wide shared-PTP decision applied to
            this member so the whole group shares one timing source.
        """
        # sync volume from parent player if needed
        airplay_player.sync_volume_level()
        if airplay_player.stream and airplay_player.stream.running:
            await airplay_player.stream.stop()
        stream_pcm_format = airplay_player.get_stream_pcm_format(self.pcm_format)
        airplay_player.stream = AirPlayStream(airplay_player, pcm_format=stream_pcm_format)
        airplay_player.stream.session = self
        await airplay_player.stream.start(use_shared_ptp, ptp_follow=self._ptp_follow)
        # Start ffmpeg to feed audio to CLI stdin
        if ffmpeg := self._player_ffmpeg.pop(airplay_player.player_id, None):
            await ffmpeg.close()
        handoff_format = airplay_player.stream.pcm_format
        output_plan = self.mass.streams.audio.get_player_output_plan(
            airplay_player.player_id,
            input_format=self.pcm_format,
            output_format=get_final_output_format(handoff_format, airplay_player),
            handoff_format=handoff_format,
            queue_id=self.media.source_id,
            session_id=get_media_session_id(self.media),
        )
        cli_proc = airplay_player.stream._cli_proc
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
        self._player_ffmpeg[airplay_player.player_id] = ffmpeg

    async def _commit_generations(
        self,
        generations: dict[str, int],
        position_ms: int,
        start_unix_ms: int | None = None,
        *,
        reanchor_session: bool = True,
    ) -> None:
        """
        Start prepared member generations at one audible instant.

        :param generations: Generation number keyed by player ID.
        :param position_ms: Media position represented by each generation's first sample.
        :param start_unix_ms: Optional audible-start instant in unix epoch milliseconds.
        :param reanchor_session: Whether this generation becomes the session timeline anchor.
        """
        if start_unix_ms is None:
            start_lead_ms = 400 if len(generations) == 1 else 750
            start_unix_ms = int(time.time() * 1000) + start_lead_ms
        async with asyncio.TaskGroup() as task_group:
            for player in self.sync_clients:
                generation = generations.get(player.player_id)
                if generation is None:
                    continue
                member_start = start_unix_ms
                sync_adjust = player.config.get_value(CONF_SYNC_ADJUST, 0)
                if isinstance(sync_adjust, int):
                    member_start += sync_adjust
                stream = player.stream
                assert stream
                task_group.create_task(
                    stream.start_generation(generation, position_ms, member_start)
                )
        if reanchor_session:
            self.start_unix_ms = start_unix_ms
            self.start_time = start_unix_ms / 1000

    async def _start_generation_ffmpeg(
        self, player: AirPlayPlayer, fifo: str, media: PlayerMedia
    ) -> None:
        """
        Start the per-player DSP ffmpeg for a staged generation.

        Retires the player's previous ffmpeg and spawns a fresh one writing
        into the generation's pipe. The pipe's write side is opened here and
        handed to ffmpeg as an inherited descriptor (like the stdin case in
        ``_start_client``) because ffmpeg refuses to open an existing path.
        The open pairs with the read side the binary opened at PREPARE.
        """
        if ffmpeg := self._player_ffmpeg.pop(player.player_id, None):
            # Kill, not close: a graceful close waits for the old ffmpeg to
            # drain its buffered audio into the binary at playback speed
            # (seconds), and that audio is superseded anyway. The binary keeps
            # playing the old generation from its own ring until START lands.
            await ffmpeg.kill()
        stream = player.stream
        assert stream
        handoff_format = stream.pcm_format
        output_plan = self.mass.streams.audio.get_player_output_plan(
            player.player_id,
            input_format=self.pcm_format,
            output_format=get_final_output_format(handoff_format, player),
            handoff_format=handoff_format,
            queue_id=media.source_id,
            session_id=get_media_session_id(media),
        )
        fifo_fd = await asyncio.wait_for(asyncio.to_thread(os.open, fifo, os.O_WRONLY), timeout=5)
        try:
            ffmpeg = FFMpeg(
                audio_input="-",
                input_format=self.pcm_format,
                output_format=handoff_format,
                filter_params=output_plan.filter_params,
                audio_output=fifo_fd,
            )
            await ffmpeg.start()
        finally:
            # the ffmpeg child inherited the descriptor; drop our copy so the
            # pipe sees EOF when ffmpeg exits
            os.close(fifo_fd)
        self._player_ffmpeg[player.player_id] = ffmpeg
