"""Controller to stream audio to players."""
from __future__ import annotations

import asyncio
import gc
import urllib.parse
from time import time
from types import CoroutineType
from typing import TYPE_CHECKING, AsyncGenerator, Dict, Optional, Tuple
from uuid import uuid4

from aiohttp import web

from music_assistant.constants import FALLBACK_DURATION, SILENCE_FILE
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
from music_assistant.models.enums import (
    ContentType,
    CrossFadeMode,
    EventType,
    MediaType,
    MetadataMode,
    ProviderType,
)
from music_assistant.models.errors import MediaNotFoundError, QueueEmpty
from music_assistant.models.event import MassEvent
from music_assistant.models.player_queue import PlayerQueue
from music_assistant.models.queue_item import QueueItem

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

ICY_CHUNKSIZE = 8192


class StreamsController:
    """Controller to stream audio to players."""

    def __init__(self, mass: MusicAssistant):
        """Initialize instance."""
        self.mass = mass
        self.logger = mass.logger.getChild("stream")
        self._port = mass.config.stream_port
        self._ip = mass.config.stream_ip
        self.queue_streams: Dict[str, QueueStream] = {}
        self.announcements: Dict[str, Tuple[str]] = {}

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

    def get_announcement_url(
        self,
        queue_id: str,
        urls: Tuple[str],
        content_type: ContentType,
    ) -> str:
        """Get url to announcement stream."""
        self.announcements[queue_id] = urls
        ext = content_type.value
        return f"{self.base_url}/announce/{queue_id}.{ext}"

    def get_control_url(self, player_id: str, cmd: str) -> str:
        """Get url to special control stream."""
        return f"{self.base_url}/control/{player_id}/{cmd}.mp3"

    async def get_preview_url(self, provider: ProviderType, track_id: str) -> str:
        """Return url to short preview sample."""
        track = await self.mass.music.tracks.get_provider_item(track_id, provider)
        if preview := track.metadata.preview:
            return preview
        enc_track_id = urllib.parse.quote(track_id)
        return f"{self.base_url}/preview?provider_id={provider.value}&item_id={enc_track_id}"

    async def setup(self) -> None:
        """Async initialize of module."""
        app = web.Application()

        app.router.add_get("/preview", self.serve_preview)
        app.router.add_get("/announce/{queue_id}.{fmt}", self.serve_announcement)
        app.router.add_get("/control/{player_id}/{cmd}.mp3", self.serve_control)
        app.router.add_get("/{stream_id}.{fmt}", self.serve_queue_stream)

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

    async def serve_announcement(self, request: web.Request):
        """Serve announcement broadcast."""
        queue_id = request.match_info["queue_id"]
        fmt = ContentType.try_parse(request.match_info["fmt"])
        urls = self.announcements[queue_id]

        ffmpeg_args = ["ffmpeg", "-hide_banner", "-loglevel", "quiet"]
        for url in urls:
            ffmpeg_args += ["-i", url]
        if len(urls) > 1:
            ffmpeg_args += [
                "-filter_complex",
                f"[0:a][1:a]concat=n={len(urls)}:v=0:a=1",
            ]
        ffmpeg_args += ["-f", fmt.value, "-"]

        async with AsyncProcess(ffmpeg_args) as ffmpeg_proc:
            output, _ = await ffmpeg_proc.communicate()

        return web.Response(body=output, headers={"Content-Type": f"audio/{fmt.value}"})

    async def serve_control(self, request: web.Request):
        """Serve special control stream."""
        self.logger.debug(
            "Got %s request to %s from %s\nheaders: %s\n",
            request.method,
            request.path,
            request.remote,
            request.headers,
        )
        player_id = request.match_info["player_id"]
        cmd = request.match_info["cmd"]

        player = self.mass.players.get_player(player_id)
        if not player:
            return web.Response(status=404)

        queue = player.active_queue

        if queue and queue.stream:
            # handle next (ignore if signal_next active)
            if cmd == "next" and not queue.stream.signal_next:
                self.mass.create_task(queue.stream.queue.next())
            # handle previous
            elif cmd == "previous":
                self.mass.create_task(queue.stream.queue.previous())

        # always respond with silence just to prevent errors
        return web.FileResponse(SILENCE_FILE)

    async def serve_queue_stream(self, request: web.Request):
        """Serve queue audio stream to a single player."""
        self.logger.debug(
            "Got %s request to %s from %s\nheaders: %s\n",
            request.method,
            request.path,
            request.remote,
            request.headers,
        )
        client_id = request.query.get("player_id", request.remote)
        stream_id = request.match_info["stream_id"]
        queue_stream = self.queue_streams.get(stream_id)

        # try to recover from the situation where the player itself requests
        # a stream that is already done
        if queue_stream is None or queue_stream.done.is_set():
            self.logger.warning(
                "Got stream request for unknown or finished stream: %s",
                stream_id,
            )
            return web.FileResponse(SILENCE_FILE)

        # handle a second connection for the same player
        # this probably means a client which does multiple GET requests (e.g. Kodi, Vlc)
        if client_id in queue_stream.connected_clients:
            self.logger.warning(
                "Simultanuous connections detected from %s, playback may be disturbed!",
                client_id,
            )
            client_id += uuid4().hex
        elif queue_stream.all_clients_connected.is_set():
            self.logger.warning(
                "Got stream request for already running stream: %s, playback may be disturbed!",
                stream_id,
            )

        # prepare request, add some DLNA/UPNP compatible headers
        headers = {
            "Content-Type": f"audio/{queue_stream.output_format.value}",
            "transferMode.dlna.org": "Streaming",
            "contentFeatures.dlna.org": "DLNA.ORG_OP=00;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=0d500000000000000000000000000000",
            "Cache-Control": "no-cache",
        }

        # ICY-metadata headers depend on settings
        metadata_mode = queue_stream.queue.settings.metadata_mode
        if metadata_mode != MetadataMode.DISABLED:
            headers["icy-name"] = "Music Assistant"
            headers["icy-pub"] = "1"
            headers["icy-metaint"] = str(queue_stream.output_chunksize)

        resp = web.StreamResponse(headers=headers)
        try:
            await resp.prepare(request)
        except ConnectionResetError:
            return resp

        if request.method != "GET":
            # do not start stream on HEAD request
            return resp

        enable_icy = request.headers.get("Icy-MetaData", "") == "1"

        # regular streaming - each chunk is sent to the callback here
        # this chunk is already encoded to the requested audio format of choice.
        # optional ICY metadata can be sent to the client if it supports that
        async def audio_callback(chunk: bytes) -> None:
            """Call when a new audio chunk arrives."""
            # write audio
            await resp.write(chunk)

            # ICY metadata support
            if not enable_icy:
                return

            # if icy metadata is enabled, send the icy metadata after the chunk
            item_in_buf = queue_stream.queue.get_item(queue_stream.index_in_buffer)
            if item_in_buf and item_in_buf.name:
                title = item_in_buf.name
                if item_in_buf.image and not item_in_buf.image.is_file:
                    image = item_in_buf.media_item.image.url
                else:
                    image = ""
            else:
                title = "Music Assistant"
                image = ""
            metadata = f"StreamTitle='{title}';StreamUrl='&picture={image}';".encode()
            while len(metadata) % 16 != 0:
                metadata += b"\x00"
            length = len(metadata)
            length_b = chr(int(length / 16)).encode()
            await resp.write(length_b + metadata)

        await queue_stream.subscribe(client_id, audio_callback)
        await resp.write_eof()

        return resp

    async def start_queue_stream(
        self,
        queue: PlayerQueue,
        start_index: int,
        seek_position: int,
        fade_in: bool,
        output_format: ContentType,
    ) -> QueueStream:
        """Start running a queue stream."""
        # generate unique stream url
        stream_id = uuid4().hex
        # determine the pcm details based on the first track we need to stream
        try:
            first_item = queue.items[start_index]
        except (IndexError, TypeError) as err:
            raise QueueEmpty() from err

        streamdetails = await get_stream_details(self.mass, first_item, queue.queue_id)

        # work out pcm details
        if queue.settings.crossfade_mode == CrossFadeMode.ALWAYS:
            pcm_sample_rate = min(96000, queue.settings.max_sample_rate)
            pcm_bit_depth = 24
            pcm_channels = 2
            allow_resample = True
        elif streamdetails.sample_rate > queue.settings.max_sample_rate:
            pcm_sample_rate = queue.settings.max_sample_rate
            pcm_bit_depth = streamdetails.bit_depth
            pcm_channels = streamdetails.channels
            allow_resample = True
        else:
            pcm_sample_rate = streamdetails.sample_rate
            pcm_bit_depth = streamdetails.bit_depth
            pcm_channels = streamdetails.channels
            allow_resample = False

        self.queue_streams[stream_id] = stream = QueueStream(
            queue=queue,
            stream_id=stream_id,
            start_index=start_index,
            seek_position=seek_position,
            fade_in=fade_in,
            output_format=output_format,
            pcm_sample_rate=pcm_sample_rate,
            pcm_bit_depth=pcm_bit_depth,
            pcm_channels=pcm_channels,
            allow_resample=allow_resample,
        )
        # cleanup stale previous queue tasks
        asyncio.create_task(self.cleanup_stale())
        return stream

    async def cleanup_stale(self) -> None:
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
        start_index: int,
        seek_position: int,
        fade_in: bool,
        output_format: ContentType,
        pcm_sample_rate: int,
        pcm_bit_depth: int,
        pcm_channels: int = 2,
        pcm_floating_point: bool = False,
        allow_resample: bool = False,
    ):
        """Init QueueStreamJob instance."""
        self.queue = queue
        self.stream_id = stream_id
        self.start_index = start_index
        self.seek_position = seek_position
        self.fade_in = fade_in
        self.output_format = output_format
        self.pcm_sample_rate = pcm_sample_rate
        self.pcm_bit_depth = pcm_bit_depth
        self.pcm_channels = pcm_channels
        self.pcm_floating_point = pcm_floating_point
        self.allow_resample = allow_resample
        self.url = queue.mass.streams.get_stream_url(stream_id, output_format)

        self.mass = queue.mass
        self.logger = self.queue.logger.getChild("stream")
        self.expected_clients = 1
        self.connected_clients: Dict[str, CoroutineType[bytes]] = {}
        self.total_seconds_streamed = 0
        self.streaming_started = 0
        self.done = asyncio.Event()
        self.all_clients_connected = asyncio.Event()
        self.index_in_buffer = start_index
        self.signal_next: Optional[int] = None
        self._runner_task: Optional[asyncio.Task] = None
        if queue.settings.metadata_mode == MetadataMode.LEGACY:
            # use the legacy/recommended metaint size of 8192 bytes
            self.output_chunksize = ICY_CHUNKSIZE
        else:
            self.output_chunksize = get_chunksize(
                output_format, pcm_sample_rate, pcm_bit_depth
            )
        self.sample_size_per_second = get_chunksize(
            ContentType.from_bit_depth(pcm_bit_depth, pcm_floating_point),
            pcm_sample_rate,
            pcm_bit_depth,
            pcm_channels,
        )
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
        assert not self.done.is_set(), "Stream task is already finished"
        # assert client_id not in self.connected_clients, "Client already connected"

        self.connected_clients[client_id] = callback
        self.logger.debug("client connected: %s", client_id)
        if len(self.connected_clients) == self.expected_clients:
            self.all_clients_connected.set()
        try:
            await self.done.wait()
        finally:
            self.logger.debug("client disconnected: %s", client_id)
            self.connected_clients.pop(client_id, None)
            await self._check_stop()

    async def _queue_stream_runner(self) -> None:
        """Distribute audio chunks over connected client(s)."""
        # collect ffmpeg args
        input_format = ContentType.from_bit_depth(
            self.pcm_bit_depth, self.pcm_floating_point
        )
        ffmpeg_args = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "quiet",
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
            # add metadata
            "-metadata",
            "title=Streaming from Music Assistant",
        ]
        # fade-in if needed
        if self.fade_in:
            ffmpeg_args += ["-af", "afade=t=in:st=0:d=5"]

        ffmpeg_args += [
            # output args
            "-f",
            self.output_format.value,
            "-compression_level",
            "0",
            "-",
        ]
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
        self,
    ) -> AsyncGenerator[None, bytes]:
        """Stream the PlayerQueue's tracks as constant feed of PCM raw audio."""
        last_fadeout_part = b""
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
            else:
                next_index = self.queue.get_next_index(queue_index)
                # break here if next index does not match (e.g. when repeat enabled)!
                if next_index <= queue_index:
                    self.signal_next = next_index
                    break
                queue_index = next_index
                seek_position = 0
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
            if not self.allow_resample and streamdetails.bit_depth > self.pcm_bit_depth:
                self.signal_next = queue_index
                self.logger.debug(
                    "Abort queue stream %s due to bit depth mismatch",
                    self.queue.player.name,
                )
                break
            if (
                not self.allow_resample
                and streamdetails.sample_rate > self.pcm_sample_rate
                and streamdetails.sample_rate <= self.queue.settings.max_sample_rate
            ):
                self.logger.debug(
                    "Abort queue stream %s due to sample rate mismatch",
                    self.queue.player.name,
                )
                self.signal_next = queue_index
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
            ):
                prev_item = await self.mass.music.get_item_by_uri(prev_track.uri)
                new_item = await self.mass.music.get_item_by_uri(queue_track.uri)
                if (
                    prev_item.album is not None
                    and new_item.album is not None
                    and prev_item.album == new_item.album
                ):
                    self.logger.debug("Skipping crossfade: Tracks are from same album")
                    use_crossfade = False
            prev_track = queue_track

            self.logger.info(
                "Start Streaming queue track: %s (%s) for queue %s - crossfade: %s",
                queue_track.uri,
                queue_track.name,
                self.queue.player.name,
                use_crossfade,
            )

            # set some basic vars
            crossfade_duration = self.queue.settings.crossfade_duration
            crossfade_size = int(self.sample_size_per_second * crossfade_duration)
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
            # handle incoming audio chunks
            async for chunk in get_media_stream(
                self.mass,
                streamdetails,
                pcm_fmt=pcm_fmt,
                sample_rate=self.pcm_sample_rate,
                channels=self.pcm_channels,
                seek_position=seek_position,
                chunk_size=self.sample_size_per_second,
            ):

                chunk_num += 1
                seconds_in_buffer = len(buffer) / self.sample_size_per_second

                ####  HANDLE FIRST PART OF TRACK

                # buffer full for crossfade
                if last_fadeout_part and (seconds_in_buffer >= buffer_duration):
                    # strip silence of start
                    first_part = await strip_silence(
                        buffer + chunk, pcm_fmt, self.pcm_sample_rate
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
                self.logger.warning("Stream error on %s", queue_track.uri)
                queue_track.streamdetails.seconds_streamed = 0
                continue

            # try to make a rough assumption of how many seconds is buffered ahead by the player(s)
            player_buffered = (
                self.total_seconds_streamed - self.queue.player.elapsed_time or 0
            )
            seconds_in_buffer = len(buffer) / self.sample_size_per_second
            # log warning if received seconds are a lot less than expected
            if (stream_duration - chunk_num) > 20:
                self.logger.warning(
                    "Unexpected number of chunks received for track: %s/%s",
                    chunk_num,
                    stream_duration,
                )
            self.logger.debug(
                "end of track reached - chunk_num: %s - crossfade_buffer: %s - stream_duration: %s - player_buffer: %s",
                chunk_num,
                seconds_in_buffer,
                stream_duration,
                player_buffered,
            )

            if buffer:
                # strip silence from end of audio
                last_part = await strip_silence(
                    buffer, pcm_fmt, self.pcm_sample_rate, reverse=True
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
                bytes_written / self.sample_size_per_second
            )
            self.logger.debug(
                "Finished Streaming queue track: %s (%s) on queue %s",
                queue_track.uri,
                queue_track.name,
                self.queue.player.name,
            )
        # end of queue reached, pass last fadeout bits to final output
        if last_fadeout_part:
            yield last_fadeout_part
        # END OF QUEUE STREAM
        self.logger.debug("Queue stream for Queue %s finished.", self.queue.player.name)

    async def _check_stop(self) -> bool:
        """Schedule stop of queue stream."""
        # Stop this queue stream when no clients (re)connected within 5 seconds
        for _ in range(0, 10):
            if len(self.connected_clients) > 0:
                return False
            await asyncio.sleep(0.5)
        asyncio.create_task(self.stop())
        return True
