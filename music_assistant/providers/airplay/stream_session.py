"""Unified AirPlay/RAOP stream session logic for AirPlay devices."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncGenerator
from contextlib import suppress
from typing import TYPE_CHECKING

from music_assistant.helpers.audio import get_player_filter_params
from music_assistant.helpers.ffmpeg import FFMpeg
from music_assistant.helpers.util import TaskManager, close_async_generator
from music_assistant.providers.airplay.helpers import get_ntp_timestamp

from .constants import StreamingProtocol
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
        input_format: AudioFormat,
        audio_source: AsyncGenerator[bytes, None],
    ) -> None:
        """Initialize AirPlayStreamSession.

        Args:
            airplay_provider: The AirPlay provider instance
            sync_clients: List of AirPlay players to stream to
            input_format: Audio format of the input stream
            audio_source: Async generator yielding audio chunks
        """
        assert sync_clients
        self.prov = airplay_provider
        self.mass = airplay_provider.mass
        self.input_format = input_format
        self.sync_clients = sync_clients
        self._audio_source = audio_source
        self._audio_source_task: asyncio.Task[None] | None = None
        self._player_ffmpeg: dict[str, FFMpeg] = {}
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        """Initialize stream session for all players."""
        # Get current NTP timestamp and calculate wait time
        start_ntp = get_ntp_timestamp()
        wait_start = 1750 + (250 * len(self.sync_clients))

        async def _start_client(airplay_player: AirPlayPlayer) -> None:
            """Start stream for a single client."""
            # Stop existing stream if running
            if airplay_player.stream and airplay_player.stream.running:
                await airplay_player.stream.stop()
            if ffmpeg := self._player_ffmpeg.pop(airplay_player.player_id, None):
                await ffmpeg.close()
                del ffmpeg

            # Create appropriate stream type based on protocol
            if airplay_player.protocol == StreamingProtocol.AIRPLAY2:
                airplay_player.stream = AirPlay2Stream(self, airplay_player)
            else:
                airplay_player.stream = RaopStream(self, airplay_player)

            # create optional FFMpeg instance per player if needed
            # this is used to do any optional DSP processing/filtering
            filter_params = get_player_filter_params(
                self.mass,
                airplay_player.player_id,
                self.input_format,
                airplay_player.stream.pcm_format,
            )
            if filter_params or self.input_format != airplay_player.stream.pcm_format:
                ffmpeg = FFMpeg(
                    audio_input="-",
                    input_format=self.input_format,
                    output_format=airplay_player.stream.pcm_format,
                    filter_params=filter_params,
                )
                await ffmpeg.start()
                self._player_ffmpeg[airplay_player.player_id] = ffmpeg

            await airplay_player.stream.start(start_ntp, wait_start)

        async with TaskManager(self.mass) as tm:
            for _airplay_player in self.sync_clients:
                tm.create_task(_start_client(_airplay_player))
        # Start audio source streamer task
        # this will read from the audio source and distribute to all players
        self._audio_source_task = asyncio.create_task(self._audio_streamer())

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
        if ffmpeg := self._player_ffmpeg.pop(airplay_player.player_id, None):
            await ffmpeg.close()
            del ffmpeg
        await airplay_player.stream.stop()
        airplay_player.stream = None
        # If this was the last client, stop the session
        if not self.sync_clients:
            await self.stop()
            return

    async def add_client(self, airplay_player: AirPlayPlayer) -> None:
        """Add a sync client to the session.

        TODO: Add the ability to add a new client to an existing session
        e.g. by counting the number of frames sent etc.

        Current implementation: restart the whole playback session when new client(s) join
        """
        sync_leader = self.sync_clients[0]
        if not sync_leader.stream or not sync_leader.stream.running:
            return

        await self.stop()  # We need to stop the current session to add a new client
        # This could potentially be called by multiple players at the exact same time
        # so we debounce the resync a bit here with a timer
        if sync_leader.current_media:
            self.mass.call_later(
                0.5,
                self.mass.players.cmd_resume(sync_leader.player_id),
                task_id=f"resync_session_{sync_leader.player_id}",
            )

    async def replace_stream(self, audio_source: AsyncGenerator[bytes, None]) -> None:
        """Replace the audio source of the stream."""
        # Cancel the current audio source task
        assert self._audio_source_task  # for type checker
        self._audio_source_task.cancel()
        with suppress(asyncio.CancelledError):
            await self._audio_source_task
        # Set new audio source and restart the stream
        self._audio_source = audio_source
        self._audio_source_task = asyncio.create_task(self._audio_streamer())
        # Restart the (player-specific) ffmpeg stream for all players
        # This is the easiest way to ensure the new audio source is used
        # as quickly as possible, without waiting for the buffers to be drained
        # It also allows changing the player settings such as DSP on the fly
        # for sync_client in self.sync_clients:
        #     if not sync_client.stream:
        #         continue  # guard
        #     sync_client.stream.start_ffmpeg_stream()

    async def _audio_streamer(self) -> None:
        """Stream audio to all players."""
        generator_exhausted = False
        _last_metadata: str | None = None
        try:
            async for chunk in self._audio_source:
                async with self._lock:
                    sync_clients = [x for x in self.sync_clients if x.stream and x.stream.running]
                    if not sync_clients:
                        return
                    await asyncio.gather(
                        *[self._write_chunk_to_player(x, chunk) for x in sync_clients if x.stream],
                        return_exceptions=True,
                    )
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
        """Write audio chunk to a specific player."""
        # if the player has an associated FFMpeg instance, use that first
        if ffmpeg := self._player_ffmpeg.get(airplay_player.player_id):
            await ffmpeg.write(chunk)
            chunk = await ffmpeg.read(len(chunk))
        assert airplay_player.stream
        await airplay_player.stream.write_chunk(chunk)

    async def _write_eof_to_player(self, airplay_player: AirPlayPlayer) -> None:
        """Write EOF to a specific player."""
        # cleanup any associated FFMpeg instance first
        if ffmpeg := self._player_ffmpeg.pop(airplay_player.player_id, None):
            await ffmpeg.write_eof()
            await ffmpeg.close()
        assert airplay_player.stream
        await airplay_player.stream.write_eof()

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
