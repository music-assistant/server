"""Unified AirPlay/RAOP stream session logic for AirPlay devices."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncGenerator
from contextlib import suppress
from typing import TYPE_CHECKING

from music_assistant_models.enums import PlaybackState

from music_assistant.helpers.audio import get_player_filter_params
from music_assistant.helpers.ffmpeg import FFMpeg
from music_assistant.helpers.util import TaskManager, close_async_generator
from music_assistant.providers.airplay.helpers import unix_time_to_ntp

from .constants import CONF_ENABLE_LATE_JOIN, ENABLE_LATE_JOIN_DEFAULT, StreamingProtocol
from .protocols.airplay2 import AirPlay2Stream
from .protocols.raop import RaopStream

if TYPE_CHECKING:
    from music_assistant_models.media_items import AudioFormat
    from music_assistant_models.player import PlayerMedia

    from .player import AirPlayPlayer
    from .provider import AirPlayProvider


class AirPlayStreamSession:
    """Stream session (RAOP or AirPlay2) to one or more players."""

    def __init__(
        self,
        airplay_provider: AirPlayProvider,
        sync_clients: list[AirPlayPlayer],
        pcm_format: AudioFormat,
        audio_source: AsyncGenerator[bytes, None],
    ) -> None:
        """Initialize AirPlayStreamSession.

        Args:
            airplay_provider: The AirPlay provider instance
            sync_clients: List of AirPlay players to stream to
            pcm_format: PCM format of the input stream
            audio_source: Async generator yielding audio chunks
        """
        assert sync_clients
        self.prov = airplay_provider
        self.mass = airplay_provider.mass
        self.pcm_format = pcm_format
        self.sync_clients = sync_clients
        self._audio_source = audio_source
        self._audio_source_task: asyncio.Task[None] | None = None
        self._player_ffmpeg: dict[str, FFMpeg] = {}
        self._lock = asyncio.Lock()
        self.start_ntp: int = 0
        self.start_time: float = 0.0
        self.chunks_streamed: int = 0  # Total chunks sent to session (each chunk = 1 second)
        # because we reuse an existing stream session for new play_media requests,
        # we need to track when the last stream was started
        self.last_stream_started: float = 0.0
        self._clients_ready_event = asyncio.Event()

    async def start(self) -> None:
        """Initialize stream session for all players."""
        # Get current NTP timestamp and calculate wait time
        cur_time = time.time()
        wait_start = 1750 + (250 * len(self.sync_clients))  # in milliseconds
        wait_start_seconds = wait_start / 1000
        self.wait_start = wait_start_seconds  # in seconds
        self.start_time = cur_time + wait_start_seconds
        self.start_ntp = unix_time_to_ntp(self.start_time)
        self.prov.logger.debug(
            "Starting stream session with %d clients",
            len(self.sync_clients),
        )
        # Start audio source streamer task
        # this will read from the audio source and distribute to all players
        # we start this task early so it can buffer audio while players are starting
        self._audio_source_task = asyncio.create_task(self._audio_streamer())

        async def _start_client(airplay_player: AirPlayPlayer) -> None:
            """Start stream for a single client."""
            # Stop existing stream if running
            if airplay_player.stream and airplay_player.stream.running:
                await airplay_player.stream.stop()

            # Create appropriate stream type based on protocol
            if airplay_player.protocol == StreamingProtocol.AIRPLAY2:
                airplay_player.stream = AirPlay2Stream(airplay_player)
            else:
                airplay_player.stream = RaopStream(airplay_player)

            # Link stream session to player stream
            airplay_player.stream.session = self
            # create the named pipes
            await airplay_player.stream.audio_pipe.create()
            await airplay_player.stream.commands_pipe.create()
            # start the stream
            await airplay_player.stream.start(self.start_ntp)
            # start the (player-specific) ffmpeg process
            # note that ffmpeg will open the named pipe for writing
            await self._start_client_ffmpeg(airplay_player)
            # open the command pipe for writing
            await airplay_player.stream.commands_pipe.open()
            # repeat sending the volume level to the player because some players seem
            # to ignore it the first time
            # https://github.com/music-assistant/support/issues/3330
            await airplay_player.stream.send_cli_command(f"VOLUME={airplay_player.volume_level}\n")

        async with TaskManager(self.mass) as tm:
            for _airplay_player in self.sync_clients:
                tm.create_task(_start_client(_airplay_player))
        # All clients started
        self._clients_ready_event.set()

    async def stop(self) -> None:
        """Stop playback and cleanup."""
        if self._audio_source_task and not self._audio_source_task.done():
            self._audio_source_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._audio_source_task
        await asyncio.gather(
            *[self.remove_client(x) for x in self.sync_clients],
            return_exceptions=True,
        )

    async def remove_client(self, airplay_player: AirPlayPlayer) -> None:
        """Remove a sync client from the session."""
        if airplay_player not in self.sync_clients:
            return
        assert airplay_player.stream
        assert airplay_player.stream.session == self
        async with self._lock:
            self.sync_clients.remove(airplay_player)
        await airplay_player.stream.stop()
        if ffmpeg := self._player_ffmpeg.pop(airplay_player.player_id, None):
            await ffmpeg.close()
            del ffmpeg
        airplay_player.stream = None
        # If this was the last client, stop the session
        if not self.sync_clients:
            await self.stop()
            return

    async def add_client(self, airplay_player: AirPlayPlayer) -> None:
        """Add a sync client to the session as a late joiner.

        The late joiner will:
        1. Start playing at a compensated NTP timestamp (start_ntp + offset)
        2. Receive silence calculated dynamically based on how much audio has been sent
        3. Then receive real audio chunks in sync with other players
        """
        sync_leader = self.sync_clients[0]
        if not sync_leader.stream or not sync_leader.stream.running:
            return

        allow_late_join = self.prov.config.get_value(
            CONF_ENABLE_LATE_JOIN, ENABLE_LATE_JOIN_DEFAULT
        )
        if not allow_late_join:
            # Late joining is not allowed - restart the session for all players
            await self.stop()  # we need to stop the current session to add a new client
            # this could potentially be called by multiple players at the exact same time
            # so we debounce the resync a bit here with a timer
            if sync_leader.current_media:
                self.mass.call_later(
                    0.5,
                    self.mass.players.cmd_resume(sync_leader.player_id),
                    task_id=f"resync_session_{sync_leader.player_id}",
                )
            return

        # Stop existing stream if the player is already streaming
        # should not happen, but guard just in case
        if airplay_player.stream and airplay_player.stream.running:
            await airplay_player.stream.stop()

        # Create appropriate stream type based on protocol
        if airplay_player.protocol == StreamingProtocol.AIRPLAY2:
            airplay_player.stream = AirPlay2Stream(airplay_player)
        else:
            airplay_player.stream = RaopStream(airplay_player)

        # Link stream session to player stream
        airplay_player.stream.session = self

        # Snapshot chunks_streamed inside lock to prevent race conditions
        # Keep lock held during stream.start() to ensure player doesn't miss any chunks
        async with self._lock:
            # (re)start the player specific ffmpeg process
            await self._start_client_ffmpeg(airplay_player)

            # Calculate skip_seconds based on how many chunks have been sent
            skip_seconds = self.chunks_streamed
            # Start the stream at compensated NTP timestamp
            start_at = self.start_time + skip_seconds
            start_ntp = unix_time_to_ntp(start_at)
            self.prov.logger.debug(
                "Adding late joiner %s to session, playback starts %.3fs from now",
                airplay_player.player_id,
                start_at - time.time(),
            )
            # Add player to sync clients list
            if airplay_player not in self.sync_clients:
                self.sync_clients.append(airplay_player)

            await airplay_player.stream.start(start_ntp)

    async def replace_stream(self, audio_source: AsyncGenerator[bytes, None]) -> None:
        """Replace the audio source of the stream."""
        # Cancel the current audio source task
        assert self._audio_source_task  # for type checker
        self._audio_source_task.cancel()
        with suppress(asyncio.CancelledError):
            await self._audio_source_task
        # Restart the (player-specific) ffmpeg stream for all players
        # This is the easiest way to ensure the new audio source is used
        # as quickly as possible, without waiting for the buffers to be drained
        # It also allows changing the player settings such as DSP on the fly
        async with self._lock, TaskManager(self.mass) as tm:
            for sync_client in self.sync_clients:
                if not sync_client.stream:
                    continue  # guard
                tm.create_task(self._start_client_ffmpeg(sync_client))
        # Set new audio source and restart the stream
        self._audio_source = audio_source
        self._audio_source_task = asyncio.create_task(self._audio_streamer())
        self.last_stream_started = time.time()
        for sync_client in self.sync_clients:
            sync_client.set_state_from_stream(state=None, elapsed_time=0)

    async def _audio_streamer(self) -> None:  # noqa: PLR0915
        """Stream audio to all players."""
        generator_exhausted = False
        _last_metadata: str | None = None
        chunk_size = self.pcm_format.pcm_sample_size
        stream_start_time = time.time()
        first_chunk_received = False
        try:
            # each chunk is exactly one second of audio data based on the pcm format.
            async for chunk in self._audio_source:
                if len(chunk) != chunk_size:
                    self.prov.logger.warning(
                        "Audio source yielded chunk of unexpected size %d (expected %d), "
                        "this may lead to desync issues",
                        len(chunk),
                        chunk_size,
                    )
                if first_chunk_received is False:
                    first_chunk_received = True
                    self.prov.logger.debug(
                        "First audio chunk received after %.3fs, "
                        "which is %.3fs before scheduled start time",
                        time.time() - stream_start_time,
                        time.time() - self.start_time,
                    )
                    # wait until the clients are ready to receive audio
                    await asyncio.wait_for(self._clients_ready_event.wait(), timeout=10)
                async with self._lock:
                    sync_clients = [x for x in self.sync_clients if x.stream and x.stream.running]
                    if not sync_clients:
                        self.prov.logger.debug(
                            "Audio streamer exiting: No running clients left in session"
                        )
                        return

                    # Write chunk to all players
                    write_tasks = [
                        self._write_chunk_to_player(x, chunk) for x in sync_clients if x.stream
                    ]
                    results = await asyncio.gather(*write_tasks, return_exceptions=True)

                    # Check for write errors or timeouts
                    players_to_remove: list[AirPlayPlayer] = []
                    for i, result in enumerate(results):
                        if i >= len(sync_clients):
                            continue
                        player = sync_clients[i]

                        if isinstance(result, asyncio.TimeoutError):
                            self.prov.logger.error(
                                "TIMEOUT writing chunk %d to player %s - REMOVING from sync group!",
                                self.chunks_streamed,
                                player.player_id,
                            )
                            players_to_remove.append(player)
                        elif isinstance(result, Exception):
                            self.prov.logger.error(
                                (
                                    "Error writing chunk %d to player %s: %s - "
                                    "REMOVING from sync group!"
                                ),
                                self.chunks_streamed,
                                player.player_id,
                                result,
                            )
                            players_to_remove.append(player)

                    # Remove failed/timed-out players from sync group
                    for player in players_to_remove:
                        if player in self.sync_clients:
                            self.sync_clients.remove(player)
                            self.prov.logger.warning(
                                "Player %s removed from sync group due to write failure/timeout",
                                player.player_id,
                            )
                            # Stop the player's stream
                            if player.stream:
                                self.mass.create_task(player.stream.stop())

                    # Update chunk counter (each chunk is exactly one second of audio)
                    self.chunks_streamed += 1

                # send metadata if changed
                # do this in a separate task to not disturb audio streaming
                # NOTE: we should probably move this out of the audio stream task into it's own task
                if (
                    self.sync_clients
                    and (_leader := self.sync_clients[0])
                    and (_leader.corrected_elapsed_time or 0) > 2
                    and (metadata := _leader.current_media) is not None
                ):
                    now = time.time()
                    metadata_checksum = f"{metadata.uri}.{metadata.title}.{metadata.image_url}"
                    progress = metadata.corrected_elapsed_time or 0
                    if _last_metadata != metadata_checksum:
                        _last_metadata = metadata_checksum
                        prev_progress_report = now
                        self.mass.create_task(self._send_metadata(progress, metadata))
                    # send the progress report every 5 seconds
                    elif now - prev_progress_report >= 5:
                        prev_progress_report = now
                        self.mass.create_task(self._send_metadata(progress, None))
            # Entire stream consumed: send EOF
            generator_exhausted = True
            async with self._lock:
                await asyncio.gather(
                    *[
                        self._write_eof_to_player(x)
                        for x in self.sync_clients
                        if x.stream and x.stream.running
                    ],
                    return_exceptions=True,
                )
        except Exception as err:
            logger = self.prov.logger
            logger.error(
                "Stream error: %s",
                str(err) or err.__class__.__name__,
                exc_info=err if logger.isEnabledFor(logging.DEBUG) else None,
            )
            raise
        finally:
            if not generator_exhausted:
                await close_async_generator(self._audio_source)

    async def _write_chunk_to_player(self, airplay_player: AirPlayPlayer, chunk: bytes) -> None:
        """
        Write audio chunk to a specific player.

        each chunk is exactly one second of audio data based on the pcm format.
        For late joiners, compensates for chunks sent between join time and actual chunk delivery.
        Blocks (async) until the data has been written.
        """
        write_start = time.time()
        chunk_number = self.chunks_streamed + 1
        player_id = airplay_player.player_id

        # don't write a chunk if we're paused
        while airplay_player.playback_state == PlaybackState.PAUSED:
            await asyncio.sleep(0.1)

        # we write the chunk to the player's ffmpeg process which
        # applies any player-specific filters (e.g. volume, dsp, etc)
        # and outputs in the correct format for the player stream
        # to the named pipe associated with the player's stream
        if ffmpeg := self._player_ffmpeg.get(player_id):
            await ffmpeg.write(chunk)

        stream_write_start = time.time()
        stream_write_elapsed = time.time() - stream_write_start
        total_elapsed = time.time() - write_start
        # Log only truly abnormal writes (>5s indicates a real stall)
        # Can take up to ~4s if player's latency buffer is being drained
        if total_elapsed > 5.0:
            self.prov.logger.error(
                "!!! STALLED WRITE: Player %s chunk %d took %.3fs total (stream write: %.3fs)",
                player_id,
                chunk_number,
                total_elapsed,
                stream_write_elapsed,
            )

    async def _write_eof_to_player(self, airplay_player: AirPlayPlayer) -> None:
        """Write EOF to a specific player."""
        # cleanup any associated FFMpeg instance first
        if ffmpeg := self._player_ffmpeg.pop(airplay_player.player_id, None):
            await ffmpeg.write_eof()
            await ffmpeg.close()

    async def _send_metadata(self, progress: int | None, metadata: PlayerMedia | None) -> None:
        """Send metadata to all players."""
        async with self._lock:
            await asyncio.gather(
                *[
                    x.stream.send_metadata(progress, metadata)
                    for x in self.sync_clients
                    if x.stream and x.stream.running
                ],
                return_exceptions=True,
            )

    async def _start_client_ffmpeg(self, airplay_player: AirPlayPlayer) -> None:
        """Start or restart the player's ffmpeg stream."""
        # Clean up any existing FFmpeg instance for this player
        if ffmpeg := self._player_ffmpeg.pop(airplay_player.player_id, None):
            await ffmpeg.close()
            del ffmpeg
        assert airplay_player.stream  # for type checker
        # Create the FFMpeg instance per player which accepts our PCM audio
        # applies any player-specific filters (e.g. volume, dsp, etc)
        # and outputs in the correct format for the player stream
        # to the named pipe associated with the player's stream
        filter_params = get_player_filter_params(
            self.mass,
            airplay_player.player_id,
            self.pcm_format,
            airplay_player.stream.pcm_format,
        )
        ffmpeg = FFMpeg(
            audio_input="-",
            input_format=self.pcm_format,
            output_format=airplay_player.stream.pcm_format,
            filter_params=filter_params,
            audio_output=airplay_player.stream.audio_pipe.path,
            extra_input_args=["-y"],
        )
        await ffmpeg.start()
        self._player_ffmpeg[airplay_player.player_id] = ffmpeg
