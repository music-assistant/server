"""Unified AirPlay/RAOP stream session logic for AirPlay devices."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator
from contextlib import suppress
from typing import TYPE_CHECKING

from music_assistant_models.enums import PlaybackState
from music_assistant_models.errors import PlayerCommandFailed

from music_assistant.constants import CONF_SYNC_ADJUST
from music_assistant.helpers.ffmpeg import FFMpeg

from .constants import StreamingProtocol
from .helpers import get_final_output_format, ntp_to_unix_time, unix_time_to_ntp
from .protocols.airplay2 import AirPlay2Stream
from .protocols.raop import RaopStream

if TYPE_CHECKING:
    from music_assistant_models.media_items import AudioFormat

    from .player import AirPlayPlayer
    from .provider import AirPlayProvider


class AirPlayStreamSession:
    """Stream session (RAOP or AirPlay2) to one or more players."""

    def __init__(
        self,
        airplay_provider: AirPlayProvider,
        sync_clients: list[AirPlayPlayer],
        pcm_format: AudioFormat,
    ) -> None:
        """Initialize AirPlayStreamSession.

        :param airplay_provider: The AirPlay provider instance.
        :param sync_clients: List of AirPlay players to stream to.
        :param pcm_format: PCM format of the input stream.
        """
        assert sync_clients
        self.prov = airplay_provider
        self.mass = airplay_provider.mass
        self.pcm_format = pcm_format
        self.sync_clients = sync_clients
        self._audio_source_task: asyncio.Task[None] | None = None
        self._stall_watchdog_task: asyncio.Task[None] | None = None
        self._player_ffmpeg: dict[str, FFMpeg] = {}
        self._lock = asyncio.Lock()
        self.start_ntp: int = 0
        self.start_time: float = 0.0
        self.wait_start: float = 0.0
        self.seconds_streamed: float = 0
        # Track when each client was added (player_id -> timestamp)
        self._client_added_time: dict[str, float] = {}
        # Raw PCM ring buffer for late joiners.  Capped at ~5 seconds.
        # When a late joiner arrives we send this buffer to prime its
        # pipeline so it starts playing quickly instead of waiting for
        # the full pipeline to fill from scratch.
        self._pcm_buffer = bytearray()
        self._pcm_buffer_max = pcm_format.pcm_sample_size * 5  # 5 seconds

    async def start(self, audio_source: AsyncGenerator[bytes, None]) -> None:
        """Initialize stream session for all players."""
        cur_time = time.time()
        wait_start = max(p.wait_start for p in self.sync_clients)
        wait_start_seconds = wait_start / 1000
        self.wait_start = wait_start_seconds
        self.start_time = cur_time + wait_start_seconds
        self.start_ntp = unix_time_to_ntp(self.start_time)
        now = time.time()
        for p in self.sync_clients:
            self._client_added_time[p.player_id] = now
        await asyncio.gather(*[self._start_client(p, self.start_ntp) for p in self.sync_clients])
        self._audio_source_task = asyncio.create_task(self._audio_streamer(audio_source))
        self._stall_watchdog_task = asyncio.create_task(self._stall_watchdog())
        try:
            await asyncio.gather(
                *[p.stream.wait_for_connection() for p in self.sync_clients if p.stream]
            )
        except Exception:
            # playback failed to start, cleanup
            await self.stop()
            raise PlayerCommandFailed("Playback failed to start")

    async def stop(self) -> None:
        """Stop playback and cleanup."""
        if self._stall_watchdog_task and not self._stall_watchdog_task.done():
            self._stall_watchdog_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._stall_watchdog_task
        if self._audio_source_task and not self._audio_source_task.done():
            self._audio_source_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._audio_source_task
        await asyncio.gather(
            *[self.remove_client(x) for x in self.sync_clients],
        )

    async def remove_client(self, airplay_player: AirPlayPlayer) -> None:
        """Remove a sync client from the session."""
        async with self._lock:
            if airplay_player not in self.sync_clients:
                return
            self.sync_clients.remove(airplay_player)
        self._client_added_time.pop(airplay_player.player_id, None)
        await self._cleanup_after_removal(airplay_player)

    async def _cleanup_after_removal(self, airplay_player: AirPlayPlayer) -> None:
        """Clean up processes and state after a client has been removed from sync_clients.

        :param airplay_player: The player whose processes should be stopped.
        """
        stream = airplay_player.stream
        await self.stop_client(airplay_player)
        # Only set IDLE if the player's stream still belongs to this session,
        # otherwise a re-add to a new session may have already set a new state.
        if stream is not None:
            airplay_player.set_state_from_stream(PlaybackState.IDLE, stream=stream)
        # Re-check sync_clients under the lock to avoid racing with add_client.
        async with self._lock:
            should_stop = not self.sync_clients
        if should_stop:
            await self.stop()

    async def stop_client(self, airplay_player: AirPlayPlayer) -> None:
        """
        Stop a client's stream and ffmpeg.

        :param airplay_player: The player to stop.
        :param force: If True, kill CLI process immediately.
        """
        ffmpeg = self._player_ffmpeg.pop(airplay_player.player_id, None)
        # note that we use kill instead of graceful close here,
        # because otherwise it can take a very long time for the process to exit.
        if ffmpeg and not ffmpeg.closed:
            await ffmpeg.kill()
        if airplay_player.stream and airplay_player.stream.session == self:
            await airplay_player.stream.stop(force=True)

    async def add_client(self, airplay_player: AirPlayPlayer) -> None:
        """Add a sync client to the session as a late joiner.

        Uses the PCM ring buffer to prime the late joiner's pipeline so it
        starts playing quickly. All work happens under the lock to ensure
        ``seconds_streamed`` and the buffer are consistent.

        Devices generally cannot honour an NTP start anchor that is in the
        past — they just play whatever the pipe gives them, trailing the
        group by the deficit. To stay in sync we therefore push the late
        joiner's ``start_at`` into the future (at least ``wait_start`` ahead
        of now) and trim the corresponding amount from the head of the
        buffered PCM, so the first sample we send maps to the correct future
        stream position.

        1. Snapshot the ring buffer and calculate how many seconds it holds.
        2. Map the buffer's first byte to its stream position; if the
           resulting NTP start is in the past, shift it forward to
           ``now + min_headroom`` and trim that many seconds from the buffer
           head so timing stays aligned with the rest of the group.
        3. Start ffmpeg+CLI, write the (possibly trimmed) buffer into ffmpeg
           to prime the pipe while cliraop is still connecting, then add to
           sync_clients so the audio streamer continues seamlessly.
        """
        async with self._lock:
            if not self.sync_clients:
                return
            first_client = self.sync_clients[0]
            if not first_client.stream or not first_client.stream.running:
                return
            now = time.time()
            pcm_sample_size = self.pcm_format.pcm_sample_size
            frame_size = (self.pcm_format.bit_depth // 8) * self.pcm_format.channels
            # Snapshot the buffer and calculate its duration
            buffered_pcm = bytes(self._pcm_buffer)
            buffer_seconds = len(buffered_pcm) / pcm_sample_size
            # The first byte in the buffer corresponds to this stream position
            first_byte_pos = self.seconds_streamed - buffer_seconds
            start_at = self.start_time + first_byte_pos

            # The audio we hand to ffmpeg → cliraop must be bit-aligned with
            # ``start_at``: the first sample sent should be the one that should
            # play at ``start_at``. cliraop buffers ``wait_start`` seconds of
            # audio before starting playback, so we keep ``start_at`` at least
            # that far in the future (using the larger of the session's
            # existing wait_start and the late joiner's own).
            min_headroom = max(self.wait_start, airplay_player.wait_start / 1000)
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
                    start_at = self.start_time + self.seconds_streamed
                else:
                    buffered_pcm = buffered_pcm[trim_bytes:]
                    # Advance start_at by exactly the trimmed duration so the
                    # NTP anchor matches the first sample we actually send.
                    start_at += trim_bytes / pcm_sample_size
                # Make sure start_at still leaves enough lead time for slow
                # connections / very-far-behind joiners.
                start_at = max(start_at, target_start_at)

            start_ntp = unix_time_to_ntp(start_at)

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

            # Start ffmpeg+CLI and immediately write the buffered PCM.
            # ffmpeg accepts data on stdin right away; it queues in the pipe
            # while cliraop is still connecting to the device.
            try:
                await self._start_client(airplay_player, start_ntp)
                if buffered_pcm:
                    await self._write_chunk_to_player(airplay_player, buffered_pcm)
            except Exception as err:
                self.prov.logger.warning(
                    "Late joiner %s: failed to start/prime pipeline: %s",
                    airplay_player.player_id,
                    err,
                )
                await self.stop_client(airplay_player)
                return

            # Now add to sync_clients — the audio streamer's next chunk
            # continues exactly from seconds_streamed where the buffer ended.
            if airplay_player not in self.sync_clients:
                self.sync_clients.append(airplay_player)
                self._client_added_time[airplay_player.player_id] = time.time()

        # Wait for device connection outside the lock.
        if airplay_player.stream:
            try:
                await airplay_player.stream.wait_for_connection()
                self.prov.logger.debug(
                    "Late joiner %s: device connected after %.2fs",
                    airplay_player.player_id,
                    time.time() - now,
                )
            except TimeoutError:
                self.prov.logger.warning(
                    "Late joiner %s: device connection timed out after %.2fs",
                    airplay_player.player_id,
                    time.time() - now,
                )
                await self.remove_client(airplay_player)

    async def _audio_streamer(self, audio_source: AsyncGenerator[bytes, None]) -> None:
        """Stream audio to all players."""
        stream_error: BaseException | None = None
        # Track inter-chunk timing to detect slow audio sources.
        # Flow streams yield ~1s chunks; gaps > threshold indicate source stalls.
        chunk_gap_threshold = 5.0
        last_chunk_time = time.time()
        source_slow_logged = False
        try:
            async for chunk in audio_source:
                if not self.sync_clients:
                    break

                now = time.time()
                chunk_gap = now - last_chunk_time
                last_chunk_time = now

                has_running_clients = await self._write_chunk_to_all_players(chunk)
                if not has_running_clients:
                    self.prov.logger.debug("No running clients remaining, stopping audio streamer")
                    break

                # Detect slow audio source (e.g. smart fades processing stall)
                if self.seconds_streamed > 30 and chunk_gap > chunk_gap_threshold:
                    self.prov.logger.warning(
                        "Slow audio source: %.1fs gap between chunks "
                        "(expected ~1s, streamed=%.1fs)",
                        chunk_gap,
                        self.seconds_streamed,
                    )
                    source_slow_logged = True
                elif source_slow_logged and chunk_gap < 2.0:
                    self.prov.logger.debug("Audio source delivery recovered")
                    source_slow_logged = False
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
        """Write a chunk to all connected players.

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
            players_to_remove: list[AirPlayPlayer] = []
            for i, result in enumerate(results):
                if i >= len(sync_clients):
                    continue
                player = sync_clients[i]

                if isinstance(result, TimeoutError):
                    self.prov.logger.warning(
                        "Removing player %s from session: stopped reading data (write timeout)",
                        player.player_id,
                    )
                    players_to_remove.append(player)
                elif isinstance(result, Exception):
                    self.prov.logger.warning(
                        "Removing player %s from session due to write error: %s",
                        player.player_id,
                        result,
                    )
                    players_to_remove.append(player)

            # Remove failed players from sync_clients immediately under the lock
            # so they are excluded from future write cycles. Only defer process
            # cleanup (_cleanup_after_removal) — this prevents fire-and-forget
            # remove_client calls from racing with a subsequent add_client when
            # a player is being moved between groups.
            for player in players_to_remove:
                if player in self.sync_clients:
                    self.sync_clients.remove(player)
                self._client_added_time.pop(player.player_id, None)
                self.mass.create_task(self._cleanup_after_removal(player))

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

    async def _stall_watchdog(self) -> None:
        """
        Monitor sync clients for stalled CLI processes and kill stuck pipelines.

        Supplements the per-chunk write timeout with proactive detection based on
        CLI stderr activity. Runs as a background task alongside the audio streamer.
        """
        stall_threshold = 30.0
        grace_period = 45.0
        check_interval = 5.0
        clients_removing: set[str] = set()
        try:
            while self.sync_clients:
                await asyncio.sleep(check_interval)
                now = time.time()
                for client in list(self.sync_clients):
                    if client.player_id in clients_removing:
                        continue
                    if not client.stream or not client.stream.running:
                        continue
                    added_time = self._client_added_time.get(client.player_id, now)
                    if now - added_time < grace_period:
                        continue
                    last_activity = client.stream.last_stderr_activity
                    if not last_activity:
                        continue
                    stall_duration = now - last_activity
                    if stall_duration > stall_threshold:
                        cli_alive = (
                            client.stream._cli_proc is not None
                            and not client.stream._cli_proc.closed
                        )
                        self.prov.logger.warning(
                            "Stall detected for player %s: no stderr activity for %.1fs "
                            "(cli_process_alive=%s, stream_running=%s). "
                            "Killing pipeline to unblock audio streamer.",
                            client.player_id,
                            stall_duration,
                            cli_alive,
                            client.stream.running,
                        )
                        clients_removing.add(client.player_id)
                        ffmpeg = self._player_ffmpeg.get(client.player_id)
                        if ffmpeg and not ffmpeg.closed:
                            await ffmpeg.kill()
                        await self.remove_client(client)
        except asyncio.CancelledError:
            pass

    async def _start_client(self, airplay_player: AirPlayPlayer, start_ntp: int) -> None:
        """Start CLI process and ffmpeg for a single client."""
        # sync volume from parent player if needed
        airplay_player.sync_volume_level()
        if airplay_player.stream and airplay_player.stream.running:
            await airplay_player.stream.stop()
        if airplay_player.protocol == StreamingProtocol.AIRPLAY2:
            airplay_player.stream = AirPlay2Stream(airplay_player)
        else:
            airplay_player.stream = RaopStream(airplay_player)
        airplay_player.stream.session = self
        sync_adjust = airplay_player.config.get_value(CONF_SYNC_ADJUST, 0)
        assert isinstance(sync_adjust, int)
        if sync_adjust != 0:
            start_ntp = unix_time_to_ntp(ntp_to_unix_time(start_ntp) + (sync_adjust / 1000))
        await airplay_player.stream.start(start_ntp)
        # Start ffmpeg to feed audio to CLI stdin
        if ffmpeg := self._player_ffmpeg.pop(airplay_player.player_id, None):
            await ffmpeg.close()
        filter_params = self.mass.streams.audio.get_player_filter_params(
            airplay_player.player_id,
            input_format=self.pcm_format,
            output_format=get_final_output_format(airplay_player.stream.pcm_format, airplay_player),
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
            output_format=airplay_player.stream.pcm_format,
            filter_params=filter_params,
            audio_output=audio_output,
        )
        await ffmpeg.start()
        self._player_ffmpeg[airplay_player.player_id] = ffmpeg
