"""Unified AirPlay/RAOP stream session logic for AirPlay devices."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator
from contextlib import suppress
from typing import TYPE_CHECKING

from music_assistant.constants import CONF_SYNC_ADJUST
from music_assistant.helpers.audio import get_player_filter_params
from music_assistant.helpers.ffmpeg import FFMpeg
from music_assistant.helpers.util import TaskManager
from music_assistant.providers.airplay.helpers import ntp_to_unix_time, unix_time_to_ntp

from .constants import (
    AIRPLAY2_CONNECT_TIME_MS,
    AIRPLAY_OUTPUT_BUFFER_DURATION_MS,
    AIRPLAY_PRELOAD_SECONDS,
    AIRPLAY_PROCESS_SPAWN_TIME_MS,
    CONF_ENABLE_LATE_JOIN,
    ENABLE_LATE_JOIN_DEFAULT,
    StreamingProtocol,
)
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
        self._audio_source_task: asyncio.Task[None] | None = None
        self._player_ffmpeg: dict[str, FFMpeg] = {}
        self._lock = asyncio.Lock()
        self.start_ntp: int = 0
        self.start_time: float = 0.0
        self.wait_start: float = 0.0  # in seconds
        self.seconds_streamed: float = 0  # Total seconds sent to session
        # because we reuse an existing stream session for new play_media requests,
        # we need to track when the last stream was started
        self.last_stream_started: float = 0.0
        self._clients_ready = asyncio.Event()
        self._first_chunk_received = asyncio.Event()

    async def start(self, audio_source: AsyncGenerator[bytes, None]) -> None:
        """Initialize stream session for all players."""
        self.prov.logger.debug(
            "Starting stream session with %d clients",
            len(self.sync_clients),
        )
        # Prepare all clients
        # this will create the stream objects, named pipes and start ffmpeg
        async with TaskManager(self.mass) as tm:
            for _airplay_player in self.sync_clients:
                tm.create_task(self._prepare_client(_airplay_player))

        # Start audio source streamer task
        # this will read from the audio source and distribute
        # to all player-specific ffmpeg processes
        # we start this task early because some streams (especially radio)
        # may need more time to buffer - this way we ensure we have audio ready
        # when the players should start playing
        self._audio_source_task = asyncio.create_task(self._audio_streamer(audio_source))
        # wait until the first chunk is received before starting clients
        await self._first_chunk_received.wait()

        # Start all clients
        # Get current NTP timestamp and calculate wait time
        cur_time = time.time()
        # AirPlay2 clients need around 2500ms to establish connection and start playback
        # The also have a fixed 2000ms output buffer. We will not be able to respect the
        # ntpstart time unless we cater for all these time delays.
        # RAOP clients need less due to less RTSP exchanges and different packet buffer
        # handling
        # Plus we need to cater for process spawn and initialisation time
        wait_start = (
            AIRPLAY2_CONNECT_TIME_MS
            + AIRPLAY_OUTPUT_BUFFER_DURATION_MS
            + AIRPLAY_PROCESS_SPAWN_TIME_MS
            + (250 * len(self.sync_clients))
        )  # in milliseconds
        wait_start_seconds = wait_start / 1000
        self.wait_start = wait_start_seconds  # in seconds
        self.start_time = cur_time + wait_start_seconds
        self.last_stream_started = self.start_time
        self.start_ntp = unix_time_to_ntp(self.start_time)

        async with TaskManager(self.mass) as tm:
            for _airplay_player in self.sync_clients:
                tm.create_task(self._start_client(_airplay_player, self.start_ntp))

        # All clients started
        self._clients_ready.set()

    async def stop(self) -> None:
        """Stop playback and cleanup."""
        await asyncio.gather(
            *[self.remove_client(x) for x in self.sync_clients],
            return_exceptions=True,
        )
        if self._audio_source_task and not self._audio_source_task.done():
            self._audio_source_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._audio_source_task

    async def remove_client(self, airplay_player: AirPlayPlayer) -> None:
        """Remove a sync client from the session."""
        async with self._lock:
            if airplay_player not in self.sync_clients:
                return
            self.sync_clients.remove(airplay_player)
        if airplay_player.stream and airplay_player.stream.session == self:
            await airplay_player.stream.stop()
        if ffmpeg := self._player_ffmpeg.pop(airplay_player.player_id, None):
            await ffmpeg.close()
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

        # Prepare the new client for streaming
        await self._prepare_client(airplay_player)

        # Snapshot seconds_streamed inside lock to prevent race conditions
        # Keep lock held during stream.start() to ensure player doesn't miss any chunks
        async with self._lock:
            # Calculate skip_seconds based on how many chunks have been sent
            skip_seconds = self.seconds_streamed
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

            await self._start_client(airplay_player, start_ntp)

    async def replace_stream(self, audio_source: AsyncGenerator[bytes, None]) -> None:
        """Replace the audio source of the stream."""
        self._first_chunk_received.clear()
        new_audio_source_task = asyncio.create_task(self._audio_streamer(audio_source))
        await self._first_chunk_received.wait()
        async with self._lock:
            # Cancel the current audio source task
            assert self._audio_source_task  # for type checker
            old_audio_source_task = self._audio_source_task
            old_audio_source_task.cancel()
            self._audio_source_task = new_audio_source_task
        self.last_stream_started = time.time() + self.wait_start
        for sync_client in self.sync_clients:
            sync_client.set_state_from_stream(state=None, elapsed_time=0)
        # ensure we cleanly wait for the old audio source task to finish
        with suppress(asyncio.CancelledError):
            await old_audio_source_task

    async def _audio_streamer(self, audio_source: AsyncGenerator[bytes, None]) -> None:
        """Stream audio to all players."""
        pcm_sample_size = self.pcm_format.pcm_sample_size
        stream_start_time = time.time()
        first_chunk_received = False
        # each chunk is exactly one second of audio data based on the pcm format.
        async for chunk in audio_source:
            if first_chunk_received is False:
                first_chunk_received = True
                self.prov.logger.debug(
                    "First audio chunk received after %.3fs",
                    time.time() - stream_start_time,
                )
                self._first_chunk_received.set()
            # Wait until all clients are ready
            await self._clients_ready.wait()
            # Send chunk to all players
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

                # Remove failed/timed-out players from sync group
                for player in players_to_remove:
                    self.mass.create_task(self.remove_client(player))

                # Update chunk counter (each chunk is exactly one second of audio)
                chunk_seconds = len(chunk) / pcm_sample_size
                self.seconds_streamed += chunk_seconds

        # Entire stream consumed: send EOF
        self.prov.logger.debug("Audio source stream exhausted")
        async with self._lock:
            await asyncio.gather(
                *[
                    self._write_eof_to_player(x)
                    for x in self.sync_clients
                    if x.stream and x.stream.running
                ],
                return_exceptions=True,
            )

    async def _write_chunk_to_player(self, airplay_player: AirPlayPlayer, chunk: bytes) -> None:
        """
        Write audio chunk to a specific player.

        each chunk is (in general) one second of audio data based on the pcm format.
        For late joiners, compensates for chunks sent between join time and actual chunk delivery.
        Blocks (async) until the data has been written.
        """
        player_id = airplay_player.player_id
        # we write the chunk to the player's ffmpeg process which
        # applies any player-specific filters (e.g. volume, dsp, etc)
        # and outputs in the correct format for the player stream
        # to the named pipe associated with the player's stream
        if ffmpeg := self._player_ffmpeg.get(player_id):
            if ffmpeg.closed:
                return
            # Use a 35 second timeout - if the write takes longer, the player
            # has stopped reading data and we're in a deadlock situation
            # 35 seconds is a little bit above out pause timeout (30s) to allow for some margin
            await asyncio.wait_for(ffmpeg.write(chunk), timeout=35.0)

    async def _write_eof_to_player(self, airplay_player: AirPlayPlayer) -> None:
        """Write EOF to a specific player."""
        # cleanup any associated FFMpeg instance first
        if ffmpeg := self._player_ffmpeg.pop(airplay_player.player_id, None):
            await ffmpeg.write_eof()
            await ffmpeg.wait_with_timeout(30)
            del ffmpeg

    async def _prepare_client(self, airplay_player: AirPlayPlayer) -> None:
        """Prepare stream for a single client."""
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
        # start the (player-specific) ffmpeg process
        # note that ffmpeg will open the named pipe for writing
        await self._start_client_ffmpeg(airplay_player)
        await asyncio.sleep(0.05)  # allow ffmpeg to open the pipe properly

    async def _start_client(self, airplay_player: AirPlayPlayer, start_ntp: int) -> None:
        """Start stream for a single client."""
        sync_adjust = airplay_player.config.get_value(CONF_SYNC_ADJUST, 0)
        assert isinstance(sync_adjust, int)
        if sync_adjust != 0:
            # apply sync adjustment
            start_ntp += sync_adjust * 1000  # sync_adjust is in seconds, NTP in milliseconds
            start_ntp = unix_time_to_ntp(ntp_to_unix_time(start_ntp) + (sync_adjust / 1000))
        # start the stream
        assert airplay_player.stream  # for type checker
        await airplay_player.stream.start(start_ntp)

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
            extra_input_args=[
                "-y",
                "-readrate",
                "1",
                "-readrate_initial_burst",
                f"{AIRPLAY_PRELOAD_SECONDS}",
            ],
        )
        await ffmpeg.start()
        self._player_ffmpeg[airplay_player.player_id] = ffmpeg
