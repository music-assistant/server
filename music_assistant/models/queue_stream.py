"""Representation of a (multisubscriber) Audio stream attached to a PlayerQueue."""
from __future__ import annotations

import asyncio
import gc
import os
import urllib.parse
from contextlib import asynccontextmanager
from time import time
from types import CoroutineType
from typing import TYPE_CHECKING, AsyncGenerator, Dict, List, Optional, Tuple
from uuid import uuid4

from aiohttp import web

from music_assistant.constants import (
    BASE_URL_OVERRIDE_ENVNAME,
    FALLBACK_DURATION,
    SILENCE_FILE,
)
from music_assistant.helpers.audio import (
    check_audio_support,
    crossfade_pcm_parts,
    get_chunksize,
    get_media_stream,
    get_preview_stream,
    get_stream_details,
    strip_silence,
)
from music_assistant.helpers.process import AsyncProcess
from music_assistant.helpers.util import empty_queue
from music_assistant.models.enums import (
    ContentType,
    CrossFadeMode,
    EventType,
    MediaType,
    MetadataMode,
    PlayerType,
    ProviderType,
    StreamState,
)
from music_assistant.models.errors import (
    MediaNotFoundError,
    PlayerUnavailableError,
    QueueEmpty,
)

if TYPE_CHECKING:
    from music_assistant.models.player import Player
    from music_assistant.models.player_queue import PlayerQueue
    from music_assistant.models.queue_item import QueueItem


class QueueStream:
    """Representation of a (multisubscriber) Audio Queue stream."""

    def __init__(self, queue: PlayerQueue):
        """Init QueueStream instance."""
        self.queue = queue
        # state details
        self.state: StreamState = StreamState.IDLE
        self.start_index = 0
        self.index_in_buffer = 0
        self.fade_in = False
        # pcm details
        self.pcm_sample_rate = 44100
        self.pcm_bit_depth = 16
        self.pcm_channels = 2
        self.pcm_floating_point = False
        self.allow_resample = False
        # helpers
        self.logger = self.queue.player.logger.getChild("stream")
        self.mass = self.queue.mass
        # private/internal attributes
        self._clients: Dict[str, asyncio.Queue] = {}
        self.signal_next: Optional[int] = None
        self._runner_task: Optional[asyncio.Task] = None

    @property
    def pcm_format(self) -> ContentType:
        """Return PCM Format (as ContentType) from PCM details."""
        return ContentType.from_bit_depth(self.pcm_bit_depth, self.pcm_floating_point)

    async def start(self, start_index: int, seek_position: int, fade_in: bool) -> None:
        """Start the QueueStream."""
        # make sure that existing streamjob is stopped
        await self.stop()

        self.start_index = start_index
        self.index_in_buffer = start_index
        self.fade_in = fade_in
        self.signal_next = None
        self.state = StreamState.PENDING_START

        # determine the pcm details based on the first track we need to stream
        try:
            first_item = self.queue.items[start_index]
        except (IndexError, TypeError) as err:
            raise QueueEmpty() from err

        streamdetails = await get_stream_details(
            self.mass, first_item, self.queue.queue_id
        )
        max_sample_rate = self.queue.player.settings.max_sample_rate
        if self.queue.settings.crossfade_mode == CrossFadeMode.ALWAYS:
            self.pcm_sample_rate = min(96000, max_sample_rate)
            self.pcm_bit_depth = 24
            self.pcm_channels = 2
            self.allow_resample = True
        elif streamdetails.sample_rate > max_sample_rate:
            self.pcm_sample_rate = max_sample_rate
            self.pcm_bit_depth = streamdetails.bit_depth
            self.pcm_channels = streamdetails.channels
            self.allow_resample = True
        else:
            self.pcm_sample_rate = streamdetails.sample_rate
            self.pcm_bit_depth = streamdetails.bit_depth
            self.pcm_channels = streamdetails.channels
            self.allow_resample = False

        # for each expected client connection we create a queue as buffer (max 10 seconds)
        if self.queue.player.type == PlayerType.UNIVERSAL_GROUP:
            for child_player in self.queue.player.get_child_players(
                ignore_sync_childs=True
            ):
                self._clients[child_player.player_id] = asyncio.Queue(10)
        else:
            self._clients[self.queue.player.player_id] = asyncio.Queue(10)

        # start the runner task in the background which fills the client(s) buffer with pcm chunks
        self._runner_task = self.mass.create_task(
            self._queue_stream_runner(seek_position)
        )

    async def stop(self) -> None:
        """Stop running queue stream and cleanup."""
        if (
            not self._runner_task
            and not self._clients
            and self.state == StreamState.IDLE
        ):
            # return early if we're already completely idle
            return
        self.logger.debug("Stop requested")
        self.state = StreamState.PENDING_STOP
        # stop runner task if needed
        if self._runner_task and not self._runner_task.done():
            self._runner_task.cancel()
            try:
                await self._runner_task
            except asyncio.CancelledError:
                pass
        # flush buffers
        for client_buf in list(self._clients.values()):
            empty_queue(client_buf)

        self._runner_task = None
        self._clients = {}
        self.state = StreamState.IDLE
        self.logger.debug("Queue stream stopped and cleaned up")

    @asynccontextmanager
    async def subscribe_client(self, client_id: str) -> asyncio.Queue:
        """Subscribe client to queue stream."""
        assert client_id not in self._clients
        self.logger.debug("client connected: %s", client_id)
        try:
            self._clients[client_id] = client_buffer = asyncio.Queue(10)
            yield client_buffer
        finally:
            self.logger.debug("client disconnected: %s", client_id)
            self._clients.pop(client_id, None)
            await self._check_stop()

    async def _queue_stream_runner(self, seek_position: int, fade_in: bool) -> None:
        """Distribute audio chunks over connected client(s)."""

        received_chunks = 0
        fade_in_buffer = b""

        async for audio_chunk in self._get_queue_stream(seek_position):
            received_chunks += 1

            # if fade_in is requested, we collect the first 5 chunks (= 5 seconds)
            if fade_in and received_chunks < 5:
                fade_in_buffer += audio_chunk
                continue
            if fade_in and received_chunks == 5:
                # TODO: perform fade-in
                audio_chunk = fade_in_buffer + audio_chunk
                del fade_in_buffer

            # send the audio chunk to client(s)
            await asyncio.wait(
                *(x.put(audio_chunk) for x in list(self._clients.values())), timeout=10
            )

        # get the raw pcm bytes from the queue stream and on-the-fly encode to wanted format
        # send the compressed/encoded stream to the client(s).
        async with AsyncProcess(ffmpeg_args, True) as ffmpeg_proc:

            async def writer():
                """Task that sends the raw pcm audio to the ffmpeg process."""
                async for audio_chunk in self._get_queue_stream():
                    await ffmpeg_proc.write(audio_chunk)
                    self.total_seconds_streamed += (
                        len(audio_chunk) / self.sample_size_per_second
                    )
                # write eof when last packet is received
                ffmpeg_proc.write_eof()

            ffmpeg_proc.attach_task(writer())

            # wait max 10 seconds for all client(s) to connect
            try:
                await asyncio.wait_for(self.all_clients_connected.wait(), 10)
            except asyncio.exceptions.TimeoutError:
                self.logger.warning(
                    "Abort: client(s) did not connect within 10 seconds."
                )
                self.done.set()
                return
            self.logger.debug("%s clients connected", len(self.connected_clients))
            self.streaming_started = time()

            # Read bytes from final output and send chunk to child callback.
            chunk_num = 0
            async for chunk in ffmpeg_proc.iter_chunked(self.output_chunksize):
                chunk_num += 1

                if len(self.connected_clients) == 0:
                    # no more clients
                    if await self._check_stop():
                        return
                for client_id in set(self.connected_clients.keys()):
                    try:
                        callback = self.connected_clients[client_id]
                        await callback(chunk)
                    except (
                        ConnectionResetError,
                        KeyError,
                        BrokenPipeError,
                    ):
                        self.connected_clients.pop(client_id, None)

        # all queue data has been streamed. Either because the queue is exhausted
        # or we need to restart the stream due to decoder/sample rate mismatch
        # set event that this stream task is finished
        # if the stream is restarted by the queue manager afterwards is controlled
        # by the `signal_next` bool above.
        self.done.set()

    async def _get_queue_stream(
        self, seek_position: int
    ) -> AsyncGenerator[None, bytes]:
        """Return the PlayerQueue's tracks as constant feed of PCM raw audio."""
        last_fadeout_part = b""
        queue_index = None
        track_count = 0
        prev_track: Optional[QueueItem] = None

        self.logger.debug(
            "Starting Queue audio stream (PCM format: %s - sample rate: %s)",
            self.pcm_format,
            self.pcm_sample_rate,
        )

        # stream queue tracks one by one
        while True:
            # get the (next) track in queue
            track_count += 1
            if track_count == 1:
                queue_index = self.start_index
            else:
                next_index = self.queue.get_next_index(queue_index)
                queue_index = next_index
            queue_track = self.queue.get_item(queue_index)
            if not queue_track:
                self.logger.debug("Abort Queue stream: no (more) tracks in queue")
                break

            # get streamdetails
            try:
                streamdetails = await get_stream_details(
                    self.mass, queue_track, self.queue.queue_id
                )
            except MediaNotFoundError as err:
                self.logger.warning(
                    "Skip track %s due to missing streamdetails",
                    queue_track.name,
                    exc_info=err,
                )
                continue

            # check the PCM samplerate/bitrate
            if not self.allow_resample and (
                streamdetails.bit_depth > self.pcm_bit_depth
                or streamdetails.sample_rate > self.pcm_sample_rate
            ):
                self.signal_next = queue_index
                self.logger.debug(
                    "Abort queue stream due to sample rate/bit depth mismatch"
                )
                break

            # check crossfade ability
            use_crossfade = (
                self.queue.settings.crossfade_mode != CrossFadeMode.DISABLED
                and self.queue.settings.crossfade_duration > 0
            )
            # do not crossfade tracks of same album
            if (
                use_crossfade
                and self.queue.settings.crossfade_mode != CrossFadeMode.ALWAYS
                and prev_track
                and prev_track.media_type == MediaType.TRACK
                and queue_track.media_type == MediaType.TRACK
                and prev_track.media_item.album is not None
                and queue_track.media_item.album is not None
                and prev_track.media_item.album == queue_track.media_item.album
            ):
                self.logger.debug("Skipping crossfade: Tracks are from same album")
                use_crossfade = False
            prev_track = queue_track

            # all checks passed - start streaming the track
            self.logger.info(
                "Start Streaming queue track: %s (%s) - crossfade: %s",
                streamdetails.uri,
                queue_track.name,
                use_crossfade,
            )

            # set some basic vars
            sample_size_per_second = int(
                self.pcm_sample_rate * (self.pcm_bit_depth / 8) * self.pcm_channels
            )
            crossfade_duration = self.queue.settings.crossfade_duration
            crossfade_size = int(sample_size_per_second * crossfade_duration)
            queue_track.streamdetails.seconds_skipped = seek_position
            # predict total size to expect for this track from duration
            stream_duration = (
                queue_track.duration or FALLBACK_DURATION
            ) - seek_position
            # buffer_duration has some overhead to account for padded silence
            buffer_duration = (crossfade_duration + 4) if use_crossfade else 4
            # send signal that we've loaded a new track into the buffer
            self.index_in_buffer = queue_index
            self.queue.signal_update()

            buffer = b""
            bytes_written = 0
            chunk_num = 0
            # process incoming audio chunks
            # each chunk is 1 second of pcm audio
            async for chunk in get_media_stream(
                self.mass,
                streamdetails,
                pcm_fmt=self.pcm_format,
                sample_rate=self.pcm_sample_rate,
                channels=self.pcm_channels,
                seek_position=seek_position,
                chunk_size=sample_size_per_second,
            ):

                chunk_num += 1
                seconds_in_buffer = len(buffer) / sample_size_per_second

                ####  HANDLE FIRST PART OF TRACK

                # buffer full for crossfade
                if last_fadeout_part and (seconds_in_buffer >= buffer_duration):
                    # strip silence of start
                    first_part = await strip_silence(
                        buffer + chunk,
                        self.pcm_format,
                        self.pcm_sample_rate,
                    )
                    # perform crossfade
                    fadein_part = first_part[:crossfade_size]
                    remaining_bytes = first_part[crossfade_size:]
                    crossfade_part = await crossfade_pcm_parts(
                        fadein_part,
                        last_fadeout_part,
                        self.pcm_bit_depth,
                        self.pcm_sample_rate,
                    )
                    # send crossfade_part
                    yield crossfade_part
                    bytes_written += len(crossfade_part)
                    # also write the leftover bytes from the strip action
                    if remaining_bytes:
                        yield remaining_bytes
                        bytes_written += len(remaining_bytes)

                    # clear vars
                    last_fadeout_part = b""
                    buffer = b""
                    continue

                # first part of track and we need to crossfade: fill buffer
                if last_fadeout_part:
                    buffer += chunk
                    continue

                # last part of track: fill buffer
                if buffer or (chunk_num >= (stream_duration - buffer_duration)):
                    buffer += chunk
                    continue

                # all other: middle of track or no crossfade action, just yield the audio
                yield chunk
                bytes_written += len(chunk)
                continue

            #### HANDLE END OF TRACK

            if bytes_written == 0:
                # stream error: got empy first chunk ?!
                self.logger.warning("Stream error on %s", streamdetails.uri)
                queue_track.streamdetails.seconds_streamed = 0
                continue

            seconds_in_buffer = len(buffer) / sample_size_per_second
            # log warning if received seconds are a lot less than expected
            if (stream_duration - chunk_num) > 20:
                self.logger.warning(
                    "Unexpected number of chunks received for track %s: (%s out of %s)",
                    streamdetails.uri,
                    chunk_num,
                    stream_duration,
                )
            self.logger.debug(
                "End of track reached - chunk_num: %s - buf_size: %s - duration: %s",
                chunk_num,
                seconds_in_buffer,
                stream_duration,
            )

            if buffer:
                # strip silence from end of audio
                last_part = await strip_silence(
                    buffer,
                    self.pcm_format,
                    self.pcm_sample_rate,
                    reverse=True,
                )
                if use_crossfade:
                    # if crossfade is enabled, save fadeout part to pickup for next track
                    if len(last_part) < crossfade_size <= len(buffer):
                        # the chunk length is too short after stripping silence, only use first part
                        last_fadeout_part = buffer[:crossfade_size]
                    elif use_crossfade and len(last_part) > crossfade_size:
                        # yield remaining bytes from strip action,
                        # we only need the crossfade_size part
                        last_fadeout_part = last_part[-crossfade_size:]
                        remaining_bytes = last_part[:-crossfade_size]
                        yield remaining_bytes
                        bytes_written += len(remaining_bytes)
                    elif use_crossfade:
                        last_fadeout_part = last_part
                else:
                    # no crossfade enabled, just yield the stripped audio data
                    yield last_part
                    bytes_written += len(last_part)

            # end of the track reached - store accurate duration
            buffer = b""
            queue_track.streamdetails.seconds_streamed = (
                bytes_written / sample_size_per_second
            )
            self.logger.debug(
                "Finished Streaming queue track: %s (%s)",
                queue_track.streamdetails.uri,
                queue_track.name,
            )
        # end of queue reached, pass last fadeout bits to final output
        if last_fadeout_part:
            yield last_fadeout_part
        # END OF QUEUE STREAM
        self.logger.debug("Queue stream finished.", self.queue.player.name)

    async def _check_stop(self) -> bool:
        """Schedule stop of queue stream."""
        # Stop this queue stream when no clients (re)connected within 5 seconds
        for _ in range(0, 10):
            if len(self.connected_clients) > 0:
                return False
            await asyncio.sleep(0.5)
        asyncio.create_task(self.stop())
        return True
