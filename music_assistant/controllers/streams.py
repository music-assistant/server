"""Controller to stream audio to players."""
from __future__ import annotations

import asyncio
import gc
import urllib.parse
from time import time
from types import CoroutineType
from typing import TYPE_CHECKING, AsyncGenerator, Dict, Optional
from uuid import uuid4

from aiohttp import web

from music_assistant.helpers.audio import (
    check_audio_support,
    create_wave_header,
    crossfade_pcm_parts,
    fadein_pcm_part,
    get_chunksize,
    get_media_stream,
    get_preview_stream,
    get_stream_details,
    strip_silence,
)
from music_assistant.helpers.process import AsyncProcess
from music_assistant.models.enums import (
    ContentType,
    CrossFadeMode,
    EventType,
    MediaType,
    ProviderType,
)
from music_assistant.models.errors import MediaNotFoundError, QueueEmpty
from music_assistant.models.event import MassEvent
from music_assistant.models.player_queue import PlayerQueue
from music_assistant.models.queue_item import QueueItem

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


class StreamsController:
    """Controller to stream audio to players."""

    def __init__(self, mass: MusicAssistant):
        """Initialize instance."""
        self.mass = mass
        self.logger = mass.logger.getChild("stream")
        self._port = mass.config.stream_port
        self._ip = mass.config.stream_ip
        self.queue_streams: Dict[str, QueueStream] = {}

    @property
    def base_url(self) -> str:
        """Return the base url for the stream engine."""
        return f"http://{self._ip}:{self._port}"

    def get_stream_url(
        self,
        stream_id: str,
        content_type: ContentType = ContentType.FLAC,
    ) -> str:
        """Generate unique stream url for the PlayerQueue Stream."""
        ext = content_type.value
        return f"{self.base_url}/{stream_id}.{ext}"

    async def get_preview_url(self, provider: ProviderType, track_id: str) -> str:
        """Return url to short preview sample."""
        track = await self.mass.music.tracks.get_provider_item(track_id, provider)
        if preview := track.metadata.preview:
            return preview
        enc_track_id = urllib.parse.quote(track_id)
        return f"{self.base_url}/preview?provider_id={provider.value}&item_id={enc_track_id}"

    def get_silence_url(self, duration: int = 600) -> str:
        """Return url to silence."""
        return f"{self.base_url}/silence?duration={duration}"

    async def setup(self) -> None:
        """Async initialize of module."""
        app = web.Application()

        app.router.add_get("/preview", self.serve_preview)
        app.router.add_get("/silence", self.serve_silence)
        app.router.add_get("/{stream_id}.{format}", self.serve_queue_stream)

        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        # set host to None to bind to all addresses on both IPv4 and IPv6
        http_site = web.TCPSite(runner, host=None, port=self._port)
        await http_site.start()

        async def on_shutdown_event(*event: MassEvent):
            """Handle shutdown event."""
            await http_site.stop()
            await runner.cleanup()
            await app.shutdown()
            await app.cleanup()
            self.logger.debug("Streamserver exited.")

        self.mass.subscribe(on_shutdown_event, EventType.SHUTDOWN)

        ffmpeg_present, libsoxr_support = await check_audio_support(True)
        if not ffmpeg_present:
            self.logger.error(
                "FFmpeg binary not found on your system, playback will NOT work!."
            )
        elif not libsoxr_support:
            self.logger.warning(
                "FFmpeg version found without libsoxr support, "
                "highest quality audio not available. "
            )

        self.logger.info("Started stream server on port %s", self._port)

    @staticmethod
    async def serve_silence(request: web.Request):
        """Serve silence."""
        resp = web.StreamResponse(
            status=200, reason="OK", headers={"Content-Type": "audio/wav"}
        )
        await resp.prepare(request)
        duration = int(request.query.get("duration", 600))
        await resp.write(create_wave_header(duration=duration))
        for _ in range(0, duration):
            await resp.write(b"\0" * 1764000)
        return resp

    async def serve_preview(self, request: web.Request):
        """Serve short preview sample."""
        provider_id = request.query["provider_id"]
        item_id = urllib.parse.unquote(request.query["item_id"])
        resp = web.StreamResponse(
            status=200, reason="OK", headers={"Content-Type": "audio/mp3"}
        )
        await resp.prepare(request)
        async for chunk in get_preview_stream(self.mass, provider_id, item_id):
            await resp.write(chunk)
        return resp

    async def serve_queue_stream(self, request: web.Request):
        """Serve queue audio stream to a single player."""
        stream_id = request.match_info["stream_id"]
        queue_stream = self.queue_streams.get(stream_id)

        if queue_stream is None:
            self.logger.warning("Got stream request for unknown id: %s", stream_id)
            return web.Response(status=404)

        # prepare request, add some DLNA/UPNP compatible headers
        headers = {
            "Content-Type": f"audio/{queue_stream.output_format.value}",
            "transferMode.dlna.org": "Streaming",
            "Connection": "Close",
            "contentFeatures.dlna.org": "DLNA.ORG_OP=00;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=0d500000000000000000000000000000",
        }
        resp = web.StreamResponse(headers=headers)
        await resp.prepare(request)
        client_id = request.remote
        await queue_stream.subscribe(client_id, resp.write)

        return resp

    async def start_queue_stream(
        self,
        queue: PlayerQueue,
        expected_clients: int,
        start_index: int,
        seek_position: int,
        fade_in: bool,
        output_format: ContentType,
        is_alert: bool,
    ) -> QueueStream:
        """Start running a queue stream."""
        # cleanup stale previous queue tasks

        # generate unique stream url
        stream_id = uuid4().hex
        # determine the pcm details based on the first track we need to stream
        try:
            first_item = queue.items[start_index]
        except (IndexError, TypeError) as err:
            raise QueueEmpty() from err

        streamdetails = await get_stream_details(self.mass, first_item, queue.queue_id)

        # work out pcm details
        if is_alert:
            pcm_sample_rate = 41000
            pcm_bit_depth = 16
            pcm_channels = 2
            pcm_resample = True
        elif queue.settings.crossfade_mode == CrossFadeMode.ALWAYS:
            pcm_sample_rate = min(96000, queue.max_sample_rate)
            pcm_bit_depth = 24
            pcm_channels = 2
            pcm_resample = True
        elif streamdetails.sample_rate > queue.max_sample_rate:
            pcm_sample_rate = queue.max_sample_rate
            pcm_bit_depth = streamdetails.bit_depth
            pcm_channels = streamdetails.channels
            pcm_resample = True
        else:
            pcm_sample_rate = streamdetails.sample_rate
            pcm_bit_depth = streamdetails.bit_depth
            pcm_channels = streamdetails.channels
            pcm_resample = False

        self.queue_streams[stream_id] = stream = QueueStream(
            queue=queue,
            stream_id=stream_id,
            expected_clients=expected_clients,
            start_index=start_index,
            seek_position=seek_position,
            fade_in=fade_in,
            output_format=output_format,
            pcm_sample_rate=pcm_sample_rate,
            pcm_bit_depth=pcm_bit_depth,
            pcm_channels=pcm_channels,
            pcm_resample=pcm_resample,
            is_alert=is_alert,
            autostart=True,
        )
        self.mass.create_task(self.cleanup_stale)
        return stream

    def cleanup_stale(self) -> None:
        """Cleanup stale/done stream tasks."""
        stale = set()
        for stream_id, stream in self.queue_streams.items():
            if stream.done.is_set() and not stream.connected_clients:
                stale.add(stream_id)
        for stream_id in stale:
            self.queue_streams.pop(stream_id, None)


class QueueStream:
    """Representation of a (multisubscriber) Audio Queue stream."""

    def __init__(
        self,
        queue: PlayerQueue,
        stream_id: str,
        expected_clients: int,
        start_index: int,
        seek_position: int,
        fade_in: bool,
        output_format: ContentType,
        pcm_sample_rate: int,
        pcm_bit_depth: int,
        pcm_channels: int = 2,
        pcm_floating_point: bool = False,
        pcm_resample: bool = False,
        is_alert: bool = False,
        autostart: bool = False,
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
        self.is_alert = is_alert
        self.url = queue.mass.streams.get_stream_url(stream_id, output_format)

        self.mass = queue.mass
        self.logger = self.queue.logger.getChild("stream")
        self.expected_clients = expected_clients
        self.connected_clients: Dict[str, CoroutineType[bytes]] = {}
        self.seconds_streamed = 0
        self.streaming_started = 0
        self.done = asyncio.Event()
        self.all_clients_connected = asyncio.Event()
        self.index_in_buffer = start_index
        self.signal_next: bool = False
        self._runner_task: Optional[asyncio.Task] = None
        if autostart:
            self.mass.create_task(self.start())

    async def start(self) -> None:
        """Start running queue stream."""
        self._runner_task = self.mass.create_task(self._queue_stream_runner())

    async def stop(self) -> None:
        """Stop running queue stream and cleanup."""
        self.done.set()
        if self._runner_task and not self._runner_task.done():
            self._runner_task.cancel()
            # allow some time to cleanup
            await asyncio.sleep(2)

        self._runner_task = None
        self.connected_clients = {}

        # run garbage collection manually due to the high number of
        # processed bytes blocks
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, gc.collect)
        self.logger.debug("Stream job %s cleaned up", self.stream_id)

    async def subscribe(self, client_id: str, callback: CoroutineType[bytes]) -> None:
        """Subscribe callback and wait for completion."""
        assert client_id not in self.connected_clients, "Client is already connected"
        assert not self.done.is_set(), "Stream task is already finished"
        self.connected_clients[client_id] = callback
        self.logger.debug("client connected: %s", client_id)
        if len(self.connected_clients) == self.expected_clients:
            self.all_clients_connected.set()
        try:
            await self.done.wait()
        finally:
            self.connected_clients.pop(client_id, None)
            self.logger.debug("client disconnected: %s", client_id)
            if len(self.connected_clients) == 0:
                # no more clients, perform cleanup
                await self.stop()

    async def _queue_stream_runner(self) -> None:
        """Distribute audio chunks over connected client queues."""
        # collect ffmpeg args
        input_format = ContentType.from_bit_depth(
            self.pcm_bit_depth, self.pcm_floating_point
        )
        ffmpeg_args = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ignore_unknown",
            # pcm input args
            "-f",
            input_format.value,
            "-ac",
            str(self.pcm_channels),
            "-ar",
            str(self.pcm_sample_rate),
            "-i",
            "-",
            # output args
            "-f",
            self.output_format.value,
            "-compression_level",
            "0",
            "-",
        ]
        # get the raw pcm bytes from the queue stream and on the fly encode to wanted format
        # send the compressed/encoded stream to the client(s).
        chunk_size = get_chunksize(self.output_format)
        sample_size = int(
            self.pcm_sample_rate * (self.pcm_bit_depth / 8) * self.pcm_channels
        )
        async with AsyncProcess(ffmpeg_args, True, chunk_size) as ffmpeg_proc:

            async def writer():
                """Task that sends the raw pcm audio to the ffmpeg process."""
                async for audio_chunk in self._get_queue_stream():
                    await ffmpeg_proc.write(audio_chunk)
                    self.seconds_streamed += len(audio_chunk) / sample_size
                    del audio_chunk
                    # allow clients to only buffer max ~30 seconds ahead
                    seconds_allowed = int(time() - self.streaming_started)
                    diff = self.seconds_streamed - seconds_allowed
                    if diff > 30:
                        self.logger.debug(
                            "Player is buffering %s seconds ahead, slowing it down a bit",
                            diff,
                        )
                        await asyncio.sleep(10)
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
            async for chunk in ffmpeg_proc.iterate_chunks():
                if len(self.connected_clients) == 0:
                    # no more clients
                    self.done.set()
                    self.logger.debug("Abort: all clients diconnected.")
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

                del chunk

            # complete queue streamed
            if self.signal_next:
                # the queue stream was aborted (e.g. because of sample rate mismatch)
                # tell the queue to load the next track (restart stream) as soon
                # as the player finished playing and returns to idle
                self.queue.signal_next = True

        # all queue data has been streamed. Either because the queue is exhausted
        # or we need to restart the stream due to decoder/sample rate mismatch
        # set event that this stream task is finished
        # if the stream is restarted by the queue manager afterwards is controlled
        # by the `signal_next` bool above.
        self.done.set()

    async def _get_queue_stream(
        self,
    ) -> AsyncGenerator[None, bytes]:
        """Stream the PlayerQueue's tracks as constant feed of PCM raw audio."""
        last_fadeout_data = b""
        queue_index = None
        track_count = 0
        prev_track: Optional[QueueItem] = None

        pcm_fmt = ContentType.from_bit_depth(self.pcm_bit_depth)
        self.logger.debug(
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
                queue_index = self.queue.get_next_index(queue_index)
                seek_position = 0
                fade_in = False
            self.index_in_buffer = queue_index
            # send signal that we've loaded a new track into the buffer
            self.queue.signal_update()
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

            # check the PCM samplerate/bitrate
            if not self.pcm_resample and streamdetails.bit_depth > self.pcm_bit_depth:
                self.signal_next = True
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
                self.signal_next = True
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
            prev_chunk = b""
            bytes_written = 0
            # handle incoming audio chunks
            async for is_last_chunk, chunk in get_media_stream(
                self.mass,
                streamdetails,
                pcm_fmt=pcm_fmt,
                sample_rate=self.pcm_sample_rate,
                channels=self.pcm_channels,
                chunk_size=buffer_size,
                seek_position=seek_position,
            ):
                cur_chunk += 1

                # HANDLE FIRST PART OF TRACK
                if len(chunk) == 0 and bytes_written == 0 and is_last_chunk:
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
                    prev_chunk = b""  # needed to prevent this chunk being sent again
                # HANDLE LAST PART OF TRACK
                elif prev_chunk and is_last_chunk:
                    # last chunk received so create the last_part
                    # with the previous chunk and this chunk
                    # and strip off silence
                    last_part = await strip_silence(
                        prev_chunk + chunk, pcm_fmt, self.pcm_sample_rate, reverse=True
                    )
                    if len(last_part) < buffer_size:
                        # part is too short after the strip action
                        # so we just use the entire original data
                        last_part = prev_chunk + chunk
                    if not use_crossfade or len(last_part) < buffer_size:
                        if use_crossfade:
                            self.logger.debug("not enough data for crossfade")
                        # crossfading is not enabled or not enough data,
                        # so just pass the (stripped) audio data
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
                elif is_last_chunk:
                    # there is only one chunk (e.g. alert sound)
                    yield chunk
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
            # end of the track reached
            queue_track.streamdetails.seconds_streamed = bytes_written / sample_size
            self.logger.debug(
                "Finished Streaming queue track: %s (%s) on queue %s",
                queue_track.uri,
                queue_track.name,
                self.queue.player.name,
            )
        # end of queue reached, pass last fadeout bits to final output
        yield last_fadeout_data
        del last_fadeout_data
        # END OF QUEUE STREAM
        self.logger.debug("Queue stream for Queue %s finished.", self.queue.player.name)
