"""Controller to stream audio to players."""
from __future__ import annotations

import asyncio
import urllib.parse
from asyncio import Task
from time import time
from typing import TYPE_CHECKING, AsyncGenerator, Dict, Optional, Set

from aiohttp import web

from music_assistant.helpers.audio import (
    check_audio_support,
    create_wave_header,
    crossfade_pcm_parts,
    get_media_stream,
    get_preview_stream,
    get_sox_args_for_pcm_stream,
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
from music_assistant.models.errors import (
    MediaNotFoundError,
    MusicAssistantError,
    QueueEmpty,
)
from music_assistant.models.event import MassEvent
from music_assistant.models.player_queue import PlayerQueue, QueueItem

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


class StreamController:
    """Controller to stream audio to players."""

    def __init__(self, mass: MusicAssistant):
        """Initialize instance."""
        self.mass = mass
        self.logger = mass.logger.getChild("stream")
        self._port = mass.config.stream_port
        self._ip = mass.config.stream_ip
        self._subscribers: Dict[str, Set[str]] = {}
        self._client_queues: Dict[str, Dict[str, asyncio.Queue]] = {}
        self._stream_tasks: Dict[str, Task] = {}
        self._time_started: Dict[str, float] = {}

    def get_stream_url(
        self,
        queue_id: str,
        child_player: Optional[str] = None,
        content_type: ContentType = ContentType.FLAC,
    ) -> str:
        """Return the full stream url for the PlayerQueue Stream."""
        ext = content_type.value
        if child_player:
            return f"http://{self._ip}:{self._port}/{queue_id}/{child_player}.{ext}"
        return f"http://{self._ip}:{self._port}/{queue_id}.{ext}"

    async def get_preview_url(self, provider: ProviderType, track_id: str) -> str:
        """Return url to short preview sample."""
        track = await self.mass.music.tracks.get_provider_item(track_id, provider)
        if preview := track.metadata.preview:
            return preview
        enc_track_id = urllib.parse.quote(track_id)
        return f"http://{self._ip}:{self._port}/preview?provider_id={provider.value}&item_id={enc_track_id}"

    async def setup(self) -> None:
        """Async initialize of module."""
        app = web.Application()

        app.router.add_get("/preview", self.serve_preview)
        app.router.add_get(
            "/{queue_id}/{player_id}.{format}",
            self.serve_multi_client_queue_stream,
        )
        app.router.add_get("/{queue_id}.{format}", self.serve_queue_stream)
        app.router.add_get("/{queue_id}", self.serve_queue_stream)

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
            self.logger.info("Streamserver exited.")

        self.mass.subscribe(on_shutdown_event, EventType.SHUTDOWN)

        sox_present, ffmpeg_present = await check_audio_support(True)
        if not ffmpeg_present and not sox_present:
            self.logger.error(
                "SoX or FFmpeg binary not found on your system, "
                "playback will NOT work!."
            )
        elif not ffmpeg_present:
            self.logger.warning(
                "The FFmpeg binary was not found on your system, "
                "you might experience issues with playback. "
                "Please install FFmpeg with your OS package manager.",
            )
        elif not sox_present:
            self.logger.warning(
                "The SoX binary was not found on your system, FFmpeg is used as fallback."
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
        async for _, chunk in get_preview_stream(self.mass, provider_id, item_id):
            await resp.write(chunk)
        return resp

    async def serve_queue_stream(self, request: web.Request):
        """Serve queue audio stream to a single player (encoded to fileformat of choice)."""
        queue_id = request.match_info["queue_id"]
        fmt = request.match_info.get("format", "flac")
        queue = self.mass.players.get_player_queue(queue_id)

        if queue is None:
            return web.Response(status=404)

        # prepare request
        try:
            start_streamdetails = await queue.queue_stream_prepare()
        except QueueEmpty:
            # send stop here to prevent the player from retrying over and over
            await queue.stop()
            # send some silence to allow the player to process the stop request
            result = create_wave_header(duration=10)
            result += b"\0" * 1764000
            return web.Response(status=200, body=result, content_type="audio/wav")

        resp = web.StreamResponse(
            status=200, reason="OK", headers={"Content-Type": f"audio/{fmt}"}
        )
        await resp.prepare(request)

        output_fmt = ContentType(fmt)
        # work out sample rate
        if queue.settings.crossfade_mode == CrossFadeMode.ALWAYS:
            sample_rate = min(96000, queue.max_sample_rate)
            bit_depth = 24
            channels = 2
            resample = True
        else:
            sample_rate = start_streamdetails.sample_rate
            bit_depth = start_streamdetails.bit_depth
            channels = start_streamdetails.channels
            resample = False
        sox_args = await get_sox_args_for_pcm_stream(
            sample_rate,
            bit_depth,
            channels,
            output_format=output_fmt,
        )
        # get the raw pcm bytes from the queue stream and on the fly encode to wanted format
        # send the compressed/encoded stream to the client.
        async with AsyncProcess(sox_args, True) as sox_proc:

            async def writer():
                # task that sends the raw pcm audio to the sox/ffmpeg process
                async for audio_chunk in self._get_queue_stream(
                    queue,
                    sample_rate=sample_rate,
                    bit_depth=bit_depth,
                    channels=channels,
                    resample=resample,
                ):
                    if sox_proc.closed:
                        return
                    await sox_proc.write(audio_chunk)
                # write eof when last packet is received
                sox_proc.write_eof()

            sox_proc.attach_task(writer())

            # read bytes from final output
            async for audio_chunk in sox_proc.iterate_chunks():
                await resp.write(audio_chunk)

        return resp

    async def serve_multi_client_queue_stream(self, request: web.Request):
        """Serve queue audio stream to multiple (group)clients."""
        queue_id = request.match_info["queue_id"]
        player_id = request.match_info["player_id"]
        fmt = request.match_info.get("format", "flac")
        queue = self.mass.players.get_player_queue(queue_id)
        player = self.mass.players.get_player(player_id)

        if queue is None or player is None:
            return web.Response(status=404)

        # prepare request
        resp = web.StreamResponse(
            status=200,
            reason="OK",
            headers={"Content-Type": f"audio/{fmt}"},
        )
        await resp.prepare(request)

        # start delivering audio chunks
        await self.subscribe_client(queue_id, player_id)
        try:
            while True:
                client_queue = self._client_queues.get(queue_id).get(player_id)
                if not client_queue:
                    break
                audio_chunk = await client_queue.get()
                if audio_chunk == b"":
                    # eof
                    break
                await resp.write(audio_chunk)
                client_queue.task_done()
        finally:
            await self.unsubscribe_client(queue_id, player_id)
        return resp

    async def subscribe_client(self, queue_id: str, player_id: str) -> None:
        """Subscribe client to queue stream."""
        if queue_id not in self._stream_tasks:
            raise MusicAssistantError(f"No Queue stream available for {queue_id}")

        if queue_id not in self._subscribers:
            self._subscribers[queue_id] = set()
        self._subscribers[queue_id].add(player_id)

        self.logger.debug(
            "Subscribed player %s to multi queue stream %s",
            player_id,
            queue_id,
        )

    async def unsubscribe_client(self, queue_id: str, player_id: str):
        """Unsubscribe client from queue stream."""
        if player_id in self._subscribers[queue_id]:
            self._subscribers[queue_id].remove(player_id)

        self.__cleanup_client_queue(queue_id, player_id)
        self.logger.debug(
            "Unsubscribed player %s from multi queue stream %s", player_id, queue_id
        )
        if len(self._subscribers[queue_id]) == 0:
            # no more clients, cancel stream task
            if task := self._stream_tasks.pop(queue_id, None):
                self.logger.debug(
                    "Aborted multi queue stream %s due to no more clients", queue_id
                )
                task.cancel()

    async def start_multi_client_queue_stream(
        self, queue_id: str, expected_clients: Set[str], output_fmt: ContentType
    ) -> None:
        """Start the Queue stream feeding callbacks of listeners.."""
        assert queue_id not in self._stream_tasks, "already running!"

        self._time_started[queue_id] = time()

        # create queue for expected clients
        self._client_queues.setdefault(queue_id, {})
        for child_id in expected_clients:
            self._client_queues[queue_id][child_id] = asyncio.Queue(10)

        self._stream_tasks[queue_id] = asyncio.create_task(
            self.__multi_client_queue_stream_runner(queue_id, output_fmt)
        )

    async def stop_multi_client_queue_stream(self, queue_id: str) -> None:
        """Signal a running queue stream task and its listeners to stop."""
        if queue_id not in self._stream_tasks:
            return

        # send stop to child players
        await asyncio.gather(
            *[
                self.mass.players.get_player(client_id).stop()
                for client_id in self._subscribers.get(queue_id, {})
            ]
        )

        # stop background task
        if stream_task := self._stream_tasks.pop(queue_id, None):
            stream_task.cancel()

        # wait for cleanup
        while len(self._subscribers.get(queue_id, {})) != 0:
            await asyncio.sleep(0.1)
        while len(self._client_queues.get(queue_id, {})) != 0:
            await asyncio.sleep(0.1)

    async def __multi_client_queue_stream_runner(
        self, queue_id: str, output_fmt: ContentType
    ):
        """Distribute audio chunks over connected clients in a multi client queue stream."""
        queue = self.mass.players.get_player_queue(queue_id)

        start_streamdetails = await queue.queue_stream_prepare()
        # work out sample rate
        if queue.settings.crossfade_mode == CrossFadeMode.ALWAYS:
            sample_rate = min(96000, queue.max_sample_rate)
            bit_depth = 24
            channels = 2
            resample = True
        else:
            sample_rate = start_streamdetails.sample_rate
            bit_depth = start_streamdetails.bit_depth
            channels = start_streamdetails.channels
            resample = False
        sox_args = await get_sox_args_for_pcm_stream(
            sample_rate,
            bit_depth,
            channels,
            output_format=output_fmt,
        )
        self.logger.debug("Multi client queue stream %s started", queue.queue_id)
        try:

            # get the raw pcm bytes from the queue stream and on the fly encode to wanted format
            # send the compressed/endoded stream to the client.
            async with AsyncProcess(sox_args, True) as sox_proc:

                async def writer():
                    """Task that sends the raw pcm audio to the sox/ffmpeg process."""
                    async for audio_chunk in self._get_queue_stream(
                        queue,
                        sample_rate=sample_rate,
                        bit_depth=bit_depth,
                        channels=channels,
                        resample=resample,
                    ):
                        if sox_proc.closed:
                            return
                        await sox_proc.write(audio_chunk)
                    # write eof when last packet is received
                    sox_proc.write_eof()

                async def reader():
                    """Read bytes from final output and put chunk on child queues."""
                    chunks_sent = 0
                    async for chunk in sox_proc.iterate_chunks():
                        chunks_sent += 1
                        coros = []
                        for player_id in list(self._client_queues[queue_id].keys()):
                            if (
                                self._client_queues[queue_id][player_id].full()
                                and chunks_sent >= 10
                                and player_id not in self._subscribers[queue_id]
                            ):
                                # assume client did not connect at all or got disconnected somehow
                                self.__cleanup_client_queue(queue_id, player_id)
                                self._client_queues[queue_id].pop(player_id, None)
                            else:
                                coros.append(
                                    self._client_queues[queue_id][player_id].put(chunk)
                                )
                        await asyncio.gather(*coros)

                # launch the reader and writer
                await asyncio.gather(*[writer(), reader()])
                # wait for all queues to consume their data
                await asyncio.gather(
                    *[cq.join() for cq in self._client_queues[queue_id].values()]
                )
                # send empty chunk to inform EOF
                await asyncio.gather(
                    *[cq.put(b"") for cq in self._client_queues[queue_id].values()]
                )

        finally:
            self.logger.debug("Multi client queue stream %s finished", queue.queue_id)
            # cleanup
            self._stream_tasks.pop(queue_id, None)
            for player_id in list(self._client_queues[queue_id].keys()):
                self.__cleanup_client_queue(queue_id, player_id)

            self.logger.debug("Multi client queue stream %s ended", queue.queue_id)

    def __cleanup_client_queue(self, queue_id: str, player_id: str):
        """Cleanup a client queue after it completes/disconnects."""
        if client_queue := self._client_queues.get(queue_id, {}).pop(player_id, None):
            for _ in range(client_queue.qsize()):
                client_queue.get_nowait()
                client_queue.task_done()
            client_queue.put_nowait(b"")

    async def _get_queue_stream(
        self,
        queue: PlayerQueue,
        sample_rate: int,
        bit_depth: int,
        channels: int = 2,
        resample: bool = False,
    ) -> AsyncGenerator[None, bytes]:
        """Stream the PlayerQueue's tracks as constant feed of PCM raw audio."""
        last_fadeout_data = b""
        queue_index = None
        track_count = 0
        prev_track: Optional[QueueItem] = None

        pcm_fmt = ContentType.from_bit_depth(bit_depth)
        self.logger.info(
            "Starting Queue audio stream for Queue %s (PCM format: %s - sample rate: %s)",
            queue.player.name,
            pcm_fmt.value,
            sample_rate,
        )

        # stream queue tracks one by one
        while True:
            start_timestamp = time()
            # get the (next) track in queue
            track_count += 1
            if track_count == 1:
                # report start of queue playback so we can calculate current track/duration etc.
                queue_index = await queue.queue_stream_start()
            else:
                queue_index = await queue.queue_stream_next(queue_index)
            queue_track = queue.get_item(queue_index)
            if not queue_track:
                self.logger.debug(
                    "Abort Queue stream %s: no (more) tracks in queue", queue.queue_id
                )
                break
            # get streamdetails
            try:
                streamdetails = await get_stream_details(
                    self.mass, queue_track, queue.queue_id
                )
            except MediaNotFoundError as err:
                self.logger.warning(
                    "Skip track %s due to missing streamdetails",
                    queue_track.name,
                    exc_info=err,
                )
                continue

            # check the PCM samplerate/bitrate
            if not resample and streamdetails.bit_depth > bit_depth:
                await queue.queue_stream_signal_next()
                self.logger.debug(
                    "Abort queue stream %s due to bit depth mismatch", queue.player.name
                )
                break
            if (
                not resample
                and streamdetails.sample_rate > sample_rate
                and streamdetails.sample_rate <= queue.max_sample_rate
            ):
                self.logger.debug(
                    "Abort queue stream %s due to sample rate mismatch",
                    queue.player.name,
                )
                await queue.queue_stream_signal_next()
                break

            # check crossfade ability
            use_crossfade = queue.settings.crossfade_mode != CrossFadeMode.DISABLED
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

            sample_size = int(sample_rate * (bit_depth / 8) * channels)  # 1 second
            buffer_size = sample_size * (queue.settings.crossfade_duration or 2)
            # force small buffer for radio to prevent too much lag at start
            if queue_track.media_type == MediaType.RADIO:
                use_crossfade = False
                buffer_size = sample_size

            self.logger.info(
                "Start Streaming queue track: %s (%s) for queue %s",
                queue_track.uri,
                queue_track.name,
                queue.player.name,
            )
            fade_in_part = b""
            cur_chunk = 0
            prev_chunk = None
            bytes_written = 0
            # handle incoming audio chunks
            async for is_last_chunk, chunk in get_media_stream(
                self.mass,
                streamdetails,
                pcm_fmt,
                resample=sample_rate,
                chunk_size=buffer_size,
            ):
                cur_chunk += 1

                # HANDLE FIRST PART OF TRACK
                if not chunk and bytes_written == 0 and is_last_chunk:
                    # stream error: got empy first chunk
                    self.logger.warning("Stream error on %s", queue_track.uri)
                    # prevent player queue get stuck by just skipping to the next track
                    queue_track.duration = 0
                    continue
                if cur_chunk <= 2 and not last_fadeout_data:
                    # no fadeout_part available so just pass it to the output directly
                    yield chunk
                    bytes_written += len(chunk)
                    del chunk
                elif cur_chunk == 1 and last_fadeout_data:
                    prev_chunk = chunk
                    del chunk
                # HANDLE CROSSFADE OF PREVIOUS TRACK FADE_OUT AND THIS TRACK FADE_IN
                elif cur_chunk == 2 and last_fadeout_data:
                    # combine the first 2 chunks and strip off silence
                    first_part = await strip_silence(
                        prev_chunk + chunk, pcm_fmt, sample_rate
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
                        queue.settings.crossfade_duration,
                        pcm_fmt,
                        sample_rate,
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
                        prev_chunk + chunk, pcm_fmt, sample_rate, True
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
                # allow clients to only buffer max ~15 seconds ahead
                seconds_streamed = bytes_written / sample_size
                seconds_needed = int(time() - start_timestamp) + 15
                diff = seconds_streamed - seconds_needed
                if diff:
                    await asyncio.sleep(diff)
            # end of the track reached
            # update actual duration to the queue for more accurate now playing info
            accurate_duration = bytes_written / sample_size
            queue_track.duration = accurate_duration
            self.logger.debug(
                "Finished Streaming queue track: %s (%s) on queue %s",
                queue_track.uri,
                queue_track.name,
                queue.player.name,
            )
        # end of queue reached, pass last fadeout bits to final output
        yield last_fadeout_data
        # END OF QUEUE STREAM
        self.logger.info("Queue stream for Queue %s finished.", queue.player.name)
