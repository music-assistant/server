"""Model for a (multisubscriber) Audio Queue."""
from __future__ import annotations

import asyncio
from types import CoroutineType
from typing import TYPE_CHECKING, AsyncGenerator, Dict, Optional

from music_assistant.helpers.audio import (
    crossfade_pcm_parts,
    fadein_pcm_part,
    get_chunksize,
    get_media_stream,
    get_sox_args_for_pcm_stream,
    get_stream_details,
    strip_silence,
)
from music_assistant.helpers.process import AsyncProcess
from music_assistant.models.enums import ContentType, CrossFadeMode, MediaType
from music_assistant.models.errors import MediaNotFoundError
from music_assistant.models.queue_item import QueueItem

if TYPE_CHECKING:
    from .player_queue import PlayerQueue


class QueueStream:
    """Model for a (multisubscriber) Audio Queue stream."""

    def __init__(
        self,
        queue: PlayerQueue,
        stream_id: str,
        expected_clients: set,
        start_index: int,
        seek_position: int,
        fade_in: bool,
        output_format: ContentType,
        pcm_sample_rate: int,
        pcm_bit_depth: int,
        pcm_channels: int = 2,
        pcm_floating_point: bool = False,
        pcm_resample: bool = False,
    ):
        """Init QueueStreamJob instance."""
        self.queue = queue
        self.stream_id = stream_id
        self.expected_clients = expected_clients
        self.start_index = start_index
        self.seek_position = seek_position
        self.fade_in = fade_in
        self.output_format = output_format
        self.pcm_sample_rate = pcm_sample_rate
        self.pcm_bit_depth = pcm_bit_depth
        self.pcm_channels = pcm_channels
        self.pcm_floating_point = pcm_floating_point
        self.pcm_resample = pcm_resample

        self.mass = queue.mass
        self.logger = self.queue.logger.getChild("stream")
        self.expected_clients = expected_clients
        self.connected_clients: Dict[str, CoroutineType[bytes]] = {}
        self._runner_task = self.mass.create_task(self._queue_stream_runner())
        self.done = asyncio.Event()

    async def subscribe(self, client_id: str, callback: CoroutineType[bytes]) -> None:
        """Subscribe callback and wait for completion."""
        assert client_id in self.expected_clients, "Unexpected client connected"
        assert client_id not in self.connected_clients, "Client is already connected"
        self.connected_clients[client_id] = callback
        self.logger.debug("client connected: %s", client_id)
        try:
            await self.done.wait()
        finally:
            self.connected_clients.pop(client_id)
            self.logger.debug("client disconnected: %s", client_id)
            if len(self.connected_clients) == 0:
                # no more clients, schedule cleanup
                self.mass.create_task(self.close())

    async def _wait_for_clients(self) -> None:
        """Wait until all expected clients connected."""
        self.logger.debug("wait for clients...")
        # wait max 2 seconds for all client(s) to connect to have a
        # more or less coordinated start
        count = 0
        while len(self.connected_clients) < len(self.expected_clients):
            if count > 200:
                break
            await asyncio.sleep(0.01)
        self.logger.debug("%s clients connected", len(self.connected_clients))

    async def close(self) -> None:
        """Cleanup stream Job when finished."""
        self.logger.debug("Finishing stream job %s", self.stream_id)
        self.done.set()
        if not self._runner_task.done():
            self._runner_task.cancel()
        self.mass.streams.queue_streams.pop(self.stream_id)
        self.logger.debug("Stream job %s cleaned up", self.stream_id)

    async def _queue_stream_runner(self) -> None:
        """Distribute audio chunks over connected client queues."""
        sox_args = await get_sox_args_for_pcm_stream(
            self.pcm_sample_rate,
            self.pcm_bit_depth,
            self.pcm_channels,
            output_format=self.output_format,
        )
        # get the raw pcm bytes from the queue stream and on the fly encode to wanted format
        # send the compressed/endoded stream to the client(s).
        async with AsyncProcess(sox_args, True) as sox_proc:

            async def writer():
                """Task that sends the raw pcm audio to the sox/ffmpeg process."""
                async for audio_chunk in self._get_queue_stream():
                    if sox_proc.closed:
                        return
                    await sox_proc.write(audio_chunk)
                # write eof when last packet is received
                sox_proc.write_eof()

            sox_proc.attach_task(writer())

            # Read bytes from final output and put chunk on child callback.
            await self._wait_for_clients()
            chunk_size = get_chunksize(self.output_format)
            async for chunk in sox_proc.iterate_chunks(chunk_size):
                await asyncio.gather(
                    *[x(chunk) for x in self.connected_clients.values()],
                    return_exceptions=False,
                )
                if len(self.connected_clients) == 0:
                    break

        # set event that we're finished
        self.done.set()

    async def _get_queue_stream(
        self,
    ) -> AsyncGenerator[None, bytes]:
        """Stream the PlayerQueue's tracks as constant feed of PCM raw audio."""
        bytes_written_total = 0
        last_fadeout_data = b""
        queue_index = None
        track_count = 0
        prev_track: Optional[QueueItem] = None
        prev_last_update = self.queue.last_update

        pcm_fmt = ContentType.from_bit_depth(self.pcm_bit_depth)
        self.logger.info(
            "Starting Queue audio stream for Queue %s (PCM format: %s - sample rate: %s)",
            self.queue.player.name,
            pcm_fmt.value,
            self.pcm_sample_rate,
        )

        # stream queue tracks one by one
        while True:
            # get the (next) track in queue
            track_count += 1
            if track_count == 1:
                queue_index = self.start_index
                seek_position = self.seek_position
                fade_in = self.fade_in
            else:
                queue_index = await self.queue.queue_stream_next(queue_index)
                seek_position = 0
                fade_in = 0
            queue_track = self.queue.get_item(queue_index)
            if not queue_track:
                self.logger.debug(
                    "Abort Queue stream %s: no (more) tracks in queue",
                    self.queue.queue_id,
                )
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

            # checksum on queue changes
            if track_count > 1 and self.queue.last_update != prev_last_update:
                await self.queue.queue_stream_signal_next()
                self.logger.debug(
                    "Abort queue stream %s due to checksum mismatch",
                    self.queue.player.name,
                )
                break

            # check the PCM samplerate/bitrate
            if not self.pcm_resample and streamdetails.bit_depth > self.pcm_bit_depth:
                await self.queue.queue_stream_signal_next()
                self.logger.debug(
                    "Abort queue stream %s due to bit depth mismatch",
                    self.queue.player.name,
                )
                break
            if (
                not self.pcm_resample
                and streamdetails.sample_rate > self.pcm_sample_rate
                and streamdetails.sample_rate <= self.queue.max_sample_rate
            ):
                self.logger.debug(
                    "Abort queue stream %s due to sample rate mismatch",
                    self.queue.player.name,
                )
                await self.queue.queue_stream_signal_next()
                break

            # check crossfade ability
            use_crossfade = self.queue.settings.crossfade_mode != CrossFadeMode.DISABLED
            if (
                prev_track is not None
                and prev_track.media_type == MediaType.TRACK
                and queue_track.media_type == MediaType.TRACK
            ):
                prev_item = await self.mass.music.get_item_by_uri(prev_track.uri)
                new_item = await self.mass.music.get_item_by_uri(queue_track.uri)
                if (
                    prev_item.album is not None
                    and new_item.album is not None
                    and prev_item.album == new_item.album
                ):
                    use_crossfade = False
            prev_track = queue_track

            sample_size = int(
                self.pcm_sample_rate * (self.pcm_bit_depth / 8) * self.pcm_channels
            )  # 1 second
            buffer_size = sample_size * (self.queue.settings.crossfade_duration or 2)
            # force small buffer for radio to prevent too much lag at start
            if queue_track.media_type != MediaType.TRACK:
                use_crossfade = False
                buffer_size = sample_size

            self.logger.info(
                "Start Streaming queue track: %s (%s) for queue %s",
                queue_track.uri,
                queue_track.name,
                self.queue.player.name,
            )
            queue_track.streamdetails.seconds_skipped = seek_position
            fade_in_part = b""
            cur_chunk = 0
            prev_chunk = None
            bytes_written = 0
            # handle incoming audio chunks
            async for is_last_chunk, chunk in get_media_stream(
                self.mass,
                streamdetails,
                pcm_fmt,
                resample=self.pcm_sample_rate,
                chunk_size=buffer_size,
                seek_position=seek_position,
            ):
                cur_chunk += 1

                # HANDLE FIRST PART OF TRACK
                if not chunk and bytes_written == 0 and is_last_chunk:
                    # stream error: got empy first chunk ?!
                    self.logger.warning("Stream error on %s", queue_track.uri)
                elif cur_chunk == 1 and last_fadeout_data:
                    prev_chunk = chunk
                    del chunk
                elif cur_chunk == 1 and fade_in:
                    # fadein first chunk
                    fadein_first_part = await fadein_pcm_part(
                        chunk, fade_in, pcm_fmt, self.pcm_sample_rate
                    )
                    yield fadein_first_part
                    bytes_written += len(fadein_first_part)
                    del chunk
                    del fadein_first_part
                elif cur_chunk <= 2 and not last_fadeout_data:
                    # no fadeout_part available so just pass it to the output directly
                    yield chunk
                    bytes_written += len(chunk)
                    del chunk
                # HANDLE CROSSFADE OF PREVIOUS TRACK FADE_OUT AND THIS TRACK FADE_IN
                elif cur_chunk == 2 and last_fadeout_data:
                    # combine the first 2 chunks and strip off silence
                    first_part = await strip_silence(
                        prev_chunk + chunk, pcm_fmt, self.pcm_sample_rate
                    )
                    if len(first_part) < buffer_size:
                        # part is too short after the strip action?!
                        # so we just use the full first part
                        first_part = prev_chunk + chunk
                    fade_in_part = first_part[:buffer_size]
                    remaining_bytes = first_part[buffer_size:]
                    del first_part
                    # do crossfade
                    crossfade_part = await crossfade_pcm_parts(
                        fade_in_part,
                        last_fadeout_data,
                        self.queue.settings.crossfade_duration,
                        pcm_fmt,
                        self.pcm_sample_rate,
                    )
                    # send crossfade_part
                    yield crossfade_part
                    bytes_written += len(crossfade_part)
                    del crossfade_part
                    del fade_in_part
                    last_fadeout_data = b""
                    # also write the leftover bytes from the strip action
                    yield remaining_bytes
                    bytes_written += len(remaining_bytes)
                    del remaining_bytes
                    del chunk
                    prev_chunk = None  # needed to prevent this chunk being sent again
                # HANDLE LAST PART OF TRACK
                elif prev_chunk and is_last_chunk:
                    # last chunk received so create the last_part
                    # with the previous chunk and this chunk
                    # and strip off silence
                    last_part = await strip_silence(
                        prev_chunk + chunk, pcm_fmt, self.pcm_sample_rate, True
                    )
                    if len(last_part) < buffer_size:
                        # part is too short after the strip action
                        # so we just use the entire original data
                        last_part = prev_chunk + chunk
                    if not use_crossfade or len(last_part) < buffer_size:
                        # crossfading is not enabled or not enough data,
                        # so just pass the (stripped) audio data
                        if use_crossfade:
                            self.logger.warning(
                                "Not enough data for crossfade: %s", len(last_part)
                            )
                        yield last_part
                        bytes_written += len(last_part)
                        del last_part
                        del chunk
                    else:
                        # handle crossfading support
                        # store fade section to be picked up for next track
                        last_fadeout_data = last_part[-buffer_size:]
                        remaining_bytes = last_part[:-buffer_size]
                        # write remaining bytes
                        if remaining_bytes:
                            yield remaining_bytes
                            bytes_written += len(remaining_bytes)
                        del last_part
                        del remaining_bytes
                        del chunk
                # MIDDLE PARTS OF TRACK
                else:
                    # middle part of the track
                    # keep previous chunk in memory so we have enough
                    # samples to perform the crossfade
                    if prev_chunk:
                        yield prev_chunk
                        bytes_written += len(prev_chunk)
                        prev_chunk = chunk
                    else:
                        prev_chunk = chunk
                    del chunk
                # allow clients to only buffer max ~60 seconds ahead
                queue_track.streamdetails.seconds_streamed = bytes_written / sample_size
                seconds_buffered = (bytes_written_total + bytes_written) / sample_size
                seconds_needed = self.queue.player.elapsed_time + 10
                diff = seconds_buffered - seconds_needed
                track_time = queue_track.duration or 0
                if track_time > 10 and diff > 1:
                    await asyncio.sleep(diff)
            # end of the track reached
            bytes_written_total += bytes_written
            self.logger.debug(
                "Finished Streaming queue track: %s (%s) on queue %s",
                queue_track.uri,
                queue_track.name,
                self.queue.player.name,
            )
        # end of queue reached, pass last fadeout bits to final output
        yield last_fadeout_data
        # END OF QUEUE STREAM
        self.logger.info("Queue stream for Queue %s finished.", self.queue.player.name)
