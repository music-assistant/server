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
        self._player_ffmpeg: dict[str, FFMpeg] = {}
        self._lock = asyncio.Lock()
        self.start_ntp: int = 0
        self.start_time: float = 0.0
        self.wait_start: float = 0.0
        self.seconds_streamed: float = 0
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
        await asyncio.gather(*[self._start_client(p, self.start_ntp) for p in self.sync_clients])
        self._audio_source_task = asyncio.create_task(self._audio_streamer(audio_source))
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
        await self.stop_client(airplay_player)
        airplay_player.set_state_from_stream(PlaybackState.IDLE)
        # If this was the last client, stop the session
        if not self.sync_clients:
            await self.stop()
            return

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
        starts playing quickly.  All work happens under the lock to ensure
        ``seconds_streamed`` and the buffer are consistent.

        1. Snapshot the ring buffer and calculate how many seconds it holds.
        2. NTP = ``start_time + (seconds_streamed - buffer_seconds)`` — the
           first byte in the buffer maps to that stream position.
        3. Start ffmpeg+CLI, write the buffer into ffmpeg (primes the pipe
           while cliraop is still connecting), then add to sync_clients so
           the audio streamer continues seamlessly from where the buffer ends.
        """
        async with self._lock:
            if not self.sync_clients:
                return
            first_client = self.sync_clients[0]
            if not first_client.stream or not first_client.stream.running:
                return
            now = time.time()
            pcm_sample_size = self.pcm_format.pcm_sample_size
            # Snapshot the buffer and calculate its duration
            buffered_pcm = bytes(self._pcm_buffer)
            buffer_seconds = len(buffered_pcm) / pcm_sample_size
            # The first byte in the buffer corresponds to this stream position
            first_byte_pos = self.seconds_streamed - buffer_seconds
            start_at = self.start_time + first_byte_pos

            # If start_at is in the past (or too close to now), prepend silence
            # to prime the pipeline while cliraop connects. The device will use
            # NTP sync to discard past-due silence and start playing at the
            # correct position — the silence just keeps the pipe fed.
            min_headroom = 1.0
            if start_at < now + min_headroom:
                deficit = (now + min_headroom) - start_at
                silence_bytes = int(deficit * pcm_sample_size)
                frame_size = (self.pcm_format.bit_depth // 8) * self.pcm_format.channels
                silence_bytes -= silence_bytes % frame_size
                if silence_bytes > 0:
                    buffered_pcm = b"\x00" * silence_bytes + buffered_pcm

            start_ntp = unix_time_to_ntp(start_at)

            self.prov.logger.debug(
                "Late joiner %s: sending %.2fs of buffered audio, "
                "stream_pos=%.2fs, first_byte_pos=%.2fs, start_at is %.2fs from now",
                airplay_player.player_id,
                len(buffered_pcm) / pcm_sample_size,
                self.seconds_streamed,
                first_byte_pos,
                start_at - now,
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
                self.mass.create_task(self.remove_client(airplay_player))

    async def _audio_streamer(self, audio_source: AsyncGenerator[bytes, None]) -> None:
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

            for player in players_to_remove:
                self.mass.create_task(self.remove_client(player))

            # Return False if all clients were removed (or scheduled for removal)
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
