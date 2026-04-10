"""Unified AirPlay/RAOP stream session logic for AirPlay devices."""

from __future__ import annotations

import asyncio
import time
from collections import deque
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
        self._chunk_available = asyncio.Condition(self._lock)
        self.start_ntp: int = 0
        self.start_time: float = 0.0
        self.wait_start: float = 0.0
        self.seconds_streamed: float = 0
        # Ring buffer for late joiners: stores (chunk_data, seconds_offset) tuples.
        # Chunk sizes vary (~100ms-1s depending on the source), so use a generous
        # maxlen to keep enough audio history for late joiners.
        self._chunk_buffer: deque[tuple[bytes, float]] = deque(maxlen=100)

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

        The late joiner will:
        1. Wait (if needed) for the ring buffer to have audio at the target position
        2. Collect buffered chunks and calculate correct NTP start time
        3. Start the stream and immediately feed buffered audio into the pipeline
        4. Wait for device connection outside the lock (data buffers in the pipe)
        5. Join the real-time stream in sync with other players
        """
        if not self.sync_clients:
            return
        first_client = self.sync_clients[0]
        if not first_client.stream or not first_client.stream.running:
            return

        async with self._chunk_available:
            buffered_chunks = self._collect_buffered_chunks(airplay_player)

            # If no usable chunks at the target position (stream just started or
            # buffer too short), wait for the audio streamer to produce enough data.
            # Without this, the NTP start time would target a future position while
            # the actual audio fed starts from an earlier position — putting the
            # late joiner seconds behind the other players.
            if (
                not buffered_chunks
                and self._audio_source_task
                and not self._audio_source_task.done()
            ):
                wait_start_seconds = airplay_player.wait_start / 1000
                self.prov.logger.debug(
                    "Late joiner %s: waiting for audio at target position "
                    "(stream position=%.2fs, need position >= %.2fs)",
                    airplay_player.player_id,
                    self.seconds_streamed,
                    (time.time() + wait_start_seconds) - self.start_time,
                )
                try:
                    await asyncio.wait_for(
                        self._chunk_available.wait_for(
                            lambda: bool(self._collect_buffered_chunks(airplay_player))
                        ),
                        timeout=wait_start_seconds + 5.0,
                    )
                    # Re-collect with updated buffer and timing
                    buffered_chunks = self._collect_buffered_chunks(airplay_player)
                except TimeoutError:
                    self.prov.logger.warning(
                        "Late joiner %s: timed out waiting for audio data (stream position=%.2fs)",
                        airplay_player.player_id,
                        self.seconds_streamed,
                    )
                    return

            if not buffered_chunks:
                self.prov.logger.warning(
                    "Late joiner %s: no usable buffered audio available, "
                    "cannot join in sync (stream position=%.2fs)",
                    airplay_player.player_id,
                    self.seconds_streamed,
                )
                return

            now = time.time()
            first_chunk_position = buffered_chunks[0][1]
            start_at = self.start_time + first_chunk_position
            # Sanity check: start_at must not be in the past for the device
            wait_start_seconds = airplay_player.wait_start / 1000
            min_start_at = now + wait_start_seconds
            start_at = max(start_at, min_start_at)
            buffer_duration = self.seconds_streamed - first_chunk_position
            buffered_bytes = sum(len(chunk) for chunk, _ in buffered_chunks)

            self.prov.logger.debug(
                "Late joiner %s: sending %.2fs of buffered audio (%d bytes, %d chunks), "
                "stream position=%.2fs, start_at is %.2fs from now",
                airplay_player.player_id,
                buffer_duration,
                buffered_bytes,
                len(buffered_chunks),
                self.seconds_streamed,
                start_at - now,
            )

            start_ntp = unix_time_to_ntp(start_at)

            if airplay_player not in self.sync_clients:
                self.sync_clients.append(airplay_player)

            await self._start_client(airplay_player, start_ntp)

            # Feed buffered chunks immediately - data will buffer in the pipe
            # while the device connection is being established
            await self._feed_buffered_chunks(airplay_player, buffered_chunks)

        # Wait for device connection OUTSIDE the lock so the audio streamer
        # continues feeding real-time chunks to all players (including this one)
        if airplay_player.stream:
            try:
                await airplay_player.stream.wait_for_connection()
                elapsed = time.time() - now
                self.prov.logger.debug(
                    "Late joiner %s: device connected after %.2fs",
                    airplay_player.player_id,
                    elapsed,
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
        pcm_sample_size = self.pcm_format.pcm_sample_size
        stream_error: BaseException | None = None
        try:
            async for chunk in audio_source:
                if not self.sync_clients:
                    break

                # Split large chunks (e.g. crossfade segments) into 1-second sub-chunks
                # to prevent write timeouts and keep the late-joiner buffer accurate.
                for offset in range(0, len(chunk), pcm_sample_size):
                    sub_chunk = chunk[offset : offset + pcm_sample_size]
                    if not self.sync_clients:
                        break
                    has_running_clients = await self._write_chunk_to_all_players(sub_chunk)
                    if not has_running_clients:
                        self.prov.logger.debug(
                            "No running clients remaining, stopping audio streamer"
                        )
                        break
                    self.seconds_streamed += len(sub_chunk) / pcm_sample_size
                    # Yield to the event loop to prevent blocking warnings
                    # when writes complete synchronously (pipe buffers not full)
                    await asyncio.sleep(0)
                else:
                    continue
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

            # Add chunk to ring buffer for late joiners (before seconds_streamed is updated)
            chunk_position = self.seconds_streamed
            self._chunk_buffer.append((chunk, chunk_position))
            # Notify late joiners waiting for buffered data
            self._chunk_available.notify_all()

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

    async def _feed_buffered_chunks(
        self,
        airplay_player: AirPlayPlayer,
        buffered_chunks: list[tuple[bytes, float]],
    ) -> None:
        """Feed buffered chunks to a late joiner to prime the ffmpeg pipeline.

        :param airplay_player: The late joiner player.
        :param buffered_chunks: List of (chunk_data, position) tuples to send.
        """
        try:
            for chunk, _position in buffered_chunks:
                await self._write_chunk_to_player(airplay_player, chunk)
        except Exception as err:
            self.prov.logger.warning(
                "Failed to feed buffered chunks to late joiner %s: %s",
                airplay_player.player_id,
                err,
            )
            # Remove the client if feeding buffered chunks fails
            self.mass.create_task(self.remove_client(airplay_player))

    def _collect_buffered_chunks(self, airplay_player: AirPlayPlayer) -> list[tuple[bytes, float]]:
        """Collect usable buffered chunks for a late joiner.

        Filters the ring buffer to chunks whose wall-clock time satisfies
        the device's minimum start constraint, trimming the first chunk
        if needed for sample-accurate alignment.

        :param airplay_player: The late joiner player.
        :return: List of (chunk_data, position) tuples to send.
        """
        now = time.time()
        wait_start_seconds = airplay_player.wait_start / 1000
        min_start_at = now + wait_start_seconds
        min_position = min_start_at - self.start_time

        all_buffered = list(self._chunk_buffer)
        buffered_chunks = [(chunk, pos) for chunk, pos in all_buffered if pos >= min_position]

        # Trim the first chunk so byte 0 aligns exactly with min_position.
        if not buffered_chunks and all_buffered:
            pcm_sample_size = self.pcm_format.pcm_sample_size
            bytes_per_sample = pcm_sample_size // self.pcm_format.sample_rate
            for i, (chunk, pos) in enumerate(all_buffered):
                chunk_duration = len(chunk) / pcm_sample_size
                if pos < min_position < pos + chunk_duration:
                    trim_seconds = min_position - pos
                    trim_bytes = int(trim_seconds * pcm_sample_size)
                    trim_bytes = (trim_bytes // bytes_per_sample) * bytes_per_sample
                    trimmed = chunk[trim_bytes:]
                    if trimmed:
                        buffered_chunks.append((trimmed, min_position))
                    buffered_chunks.extend(all_buffered[i + 1 :])
                    break

        return buffered_chunks

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
