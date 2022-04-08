"""Controller to stream audio to players."""
from __future__ import annotations

import asyncio
from asyncio import Task
from time import time
from typing import AsyncGenerator, Dict, Optional, Set

from aiohttp import web
from music_assistant.constants import EventType
from music_assistant.helpers.audio import (
    check_audio_support,
    crossfade_pcm_parts,
    get_media_stream,
    get_sox_args_for_pcm_stream,
    get_stream_details,
    strip_silence,
)
from music_assistant.helpers.process import AsyncProcess
from music_assistant.helpers.typing import MusicAssistant
from music_assistant.helpers.util import get_ip
from music_assistant.models.errors import MediaNotFoundError
from music_assistant.models.media_items import ContentType
from music_assistant.models.player_queue import PlayerQueue


class StreamController:
    """Controller to stream audio to players."""

    def __init__(self, mass: MusicAssistant, port: int = 8095):
        """Initialize instance."""
        self.mass = mass
        self.logger = mass.logger.getChild("stream")
        self._port = port
        self._ip: str = get_ip()
        self._subscribers: Dict[str, Set[str]] = {}
        self._client_queues: Dict[str, Dict[str, asyncio.Queue]] = {}
        self._stream_tasks: Dict[str, Task] = {}
        self._time_started: Dict[str, float] = {}

    def get_stream_url(
        self, queue_id: str, child_player: Optional[str] = None, fmt: str = "flac"
    ) -> str:
        """Return the full stream url for the PlayerQueue Stream."""
        if child_player:
            return f"http://{self._ip}:{self._port}/{queue_id}/{child_player}.{fmt}"
        return f"http://{self._ip}:{self._port}/{queue_id}.{fmt}"

    async def setup(self) -> None:
        """Async initialize of module."""
        app = web.Application()

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

        async def on_shutdown_event(*args, **kwargs):
            """Handle shutdown event."""
            await http_site.stop()
            await runner.cleanup()
            await app.shutdown()
            await app.cleanup()
            self.logger.info("Streamserver exited.")

        self.mass.subscribe(on_shutdown_event, EventType.SHUTDOWN)

        sox_present, ffmpeg_present = await check_audio_support(True)
        if not ffmpeg_present:
            self.logger.error(
                "The FFmpeg binary was not found on your system, "
                "you might have issues with playback. "
                "Please install FFmpeg with your OS package manager.",
            )
        elif not sox_present:
            self.logger.warning(
                "The SoX binary was not found on your system so FFmpeg is used as fallback. "
                "For best audio quality, please install SoX with your OS package manager.",
            )

        self.logger.info("Started stream server on port %s", self._port)

    async def serve_queue_stream(self, request: web.Request):
        """Serve queue audio stream to a single player (encoded to fileformat of choice)."""
        queue_id = request.match_info["queue_id"]
        fmt = request.match_info.get("format", "flac")
        queue = self.mass.players.get_player_queue(queue_id)

        if queue is None:
            return web.Response(status=404)

        # prepare request
        resp = web.StreamResponse(
            status=200, reason="OK", headers={"Content-Type": f"audio/{fmt}"}
        )
        await resp.prepare(request)

        start_streamdetails = await queue.queue_stream_prepare()
        output_fmt = ContentType(fmt)
        sox_args = await get_sox_args_for_pcm_stream(
            start_streamdetails.sample_rate,
            start_streamdetails.bit_depth,
            start_streamdetails.channels,
            output_format=output_fmt,
        )
        # get the raw pcm bytes from the queue stream and on the fly encode as to wanted format
        # send the compressed/endoded stream to the client.
        async with AsyncProcess(sox_args, True) as sox_proc:

            async def writer():
                # task that sends the raw pcm audio to the sox/ffmpeg process
                async for audio_chunk in self._get_queue_stream(
                    queue,
                    sample_rate=start_streamdetails.sample_rate,
                    bit_depth=start_streamdetails.bit_depth,
                    channels=start_streamdetails.channels,
                ):
                    if sox_proc.closed:
                        return
                    await sox_proc.write(audio_chunk)
                # write eof when last packet is received
                sox_proc.write_eof()

            self.mass.create_task(writer)

            # read bytes from final output
            chunksize = 32000 if output_fmt == ContentType.MP3 else 90000
            async for audio_chunk in sox_proc.iterate_chunks(chunksize):
                await resp.write(audio_chunk)

        return resp

    async def serve_multi_client_queue_stream(self, request: web.Request):
        """Serve queue audio stream to multiple (group)clients in the raw PCM format."""
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
            headers={"Content-Type": fmt},
        )
        await resp.prepare(request)

        # start delivering audio chunks
        await self.subscribe_client(queue_id, player_id)
        try:
            while True:
                audio_chunk = await self._client_queues[queue_id][player_id].get()
                await resp.write(audio_chunk)
                self._client_queues[queue_id][player_id].task_done()
                if audio_chunk == b"":
                    # last chunk
                    break
        finally:
            await self.unsubscribe_client(queue_id, player_id)
        return resp

    async def subscribe_client(self, queue_id: str, player_id: str) -> None:
        """Subscribe client to queue stream."""
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

        self._client_queues.get(queue_id, {}).pop(player_id, None)
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

        async def unpause_synced_start():
            # unpause if we got all clients (or timeout) for (more or less) synced start
            expected_clients = len(self._client_queues[queue_id])
            time_started = self._time_started[queue_id]
            while True:
                await asyncio.sleep(0.1)
                if (len(self._subscribers) >= expected_clients) or (
                    time() - time_started
                ) > 5:
                    await asyncio.gather(
                        *[
                            self.mass.players.get_player(client_id).play()
                            for client_id in self._subscribers.get(queue_id, {})
                        ]
                    )
                    break

        self.mass.create_task(unpause_synced_start)

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

        def cleanup_client_queue(player_id: str):
            if client_queue := self._client_queues[queue_id].get(player_id, None):
                self.logger.debug("cleaning up child queue %s", player_id)
                for _ in range(client_queue.qsize()):
                    client_queue.get_nowait()
                    client_queue.task_done()
                client_queue.put_nowait(b"")

        start_streamdetails = await queue.queue_stream_prepare()
        sox_args = await get_sox_args_for_pcm_stream(
            start_streamdetails.sample_rate,
            start_streamdetails.bit_depth,
            start_streamdetails.channels,
            output_format=output_fmt,
        )
        self.logger.debug("Multi client queue stream %s started", queue.queue_id)
        try:

            # get the raw pcm bytes from the queue stream and on the fly encode as to wanted format
            # send the compressed/endoded stream to the client.
            async with AsyncProcess(sox_args, True) as sox_proc:

                async def writer():
                    # task that sends the raw pcm audio to the sox/ffmpeg process
                    async for audio_chunk in self._get_queue_stream(
                        queue,
                        sample_rate=start_streamdetails.sample_rate,
                        bit_depth=start_streamdetails.bit_depth,
                        channels=start_streamdetails.channels,
                    ):
                        if sox_proc.closed:
                            return
                        await sox_proc.write(audio_chunk)
                    # write eof when last packet is received
                    sox_proc.write_eof()

                async def reader():
                    # read bytes from final output
                    chunks_sent = 0
                    chunksize = 32000 if output_fmt == ContentType.MP3 else 90000
                    async for chunk in sox_proc.iterate_chunks(chunksize):
                        chunks_sent += 1
                        coros = []
                        for player_id in list(self._client_queues[queue_id].keys()):
                            if (
                                chunks_sent >= 20
                                and player_id not in self._subscribers[queue_id]
                            ):
                                # assume client did not connect or got disconnected somehow
                                cleanup_client_queue(player_id)
                                self._client_queues[queue_id].pop(player_id, None)
                            else:
                                coros.append(
                                    self._client_queues[queue_id][player_id].put(chunk)
                                )
                        await asyncio.gather(*coros)

                await asyncio.gather(*[writer(), reader()])
            # send empty chunk to inform EOF
            await asyncio.gather(
                *[cq.put(b"") for cq in self._client_queues[queue_id].values()]
            )

        finally:
            # cleanup
            self._stream_tasks.pop(queue_id, None)
            for player_id in list(self._client_queues[queue_id].keys()):
                cleanup_client_queue(player_id)

            self.logger.debug("Multi client queue stream %s ended", queue.queue_id)

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
        start_timestamp = time()

        pcm_fmt = ContentType.from_bit_depth(bit_depth)
        self.logger.info(
            "Starting Queue audio stream for Queue %s (PCM format: %s - sample rate: %s)",
            queue.player.name,
            pcm_fmt,
            sample_rate,
        )

        # stream queue tracks one by one
        while True:
            # get the (next) track in queue
            track_count += 1
            if track_count == 1:
                # report start of queue playback so we can calculate current track/duration etc.
                queue_index = await queue.queue_stream_start()
            else:
                queue_index = await queue.queue_stream_next(queue_index)
            queue_track = queue.get_item(queue_index)
            if not queue_track:
                self.logger.debug("no (more) tracks in queue %s", queue.queue_id)
                break
            # get streamdetails
            try:
                streamdetails = await get_stream_details(
                    self.mass, queue_track, queue.queue_id, lazy=track_count == 1
                )
            except MediaNotFoundError as err:
                self.logger.error(str(err), exc_info=err)
                streamdetails = None

            if not streamdetails:
                self.logger.warning("Skip track due to missing streamdetails")
                continue

            # check the PCM samplerate/bitrate
            if not resample and streamdetails.bit_depth > bit_depth:
                await queue.queue_stream_signal_next()
                self.logger.debug("Abort queue stream due to bit depth mismatch")
                await queue.queue_stream_signal_next()
                break
            if (
                not resample
                and streamdetails.sample_rate > sample_rate
                and streamdetails.sample_rate <= queue.max_sample_rate
            ):
                self.logger.debug("Abort queue stream due to sample rate mismatch")
                await queue.queue_stream_signal_next()
                break

            sample_size = int(sample_rate * (bit_depth / 8) * channels)  # 1 second
            buffer_size = sample_size * (
                queue.crossfade_duration or 1
            )  # 1...10 seconds

            self.logger.debug(
                "Start Streaming queue track: %s (%s) for queue %s",
                queue_track.item_id,
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
                if not chunk and bytes_written == 0:
                    # stream error: got empy first chunk
                    self.logger.error("Stream error on track %s", queue_track.item_id)
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
                        queue.crossfade_duration,
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
                    if not queue.crossfade_duration or len(last_part) < buffer_size:
                        # crossfading is not enabled or not enough data,
                        # so just pass the (stripped) audio data
                        if queue.crossfade_duration:
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
                # guard for clients buffering too much
                seconds_streamed = bytes_written / sample_size
                seconds_needed = int(time() - start_timestamp)
                if (seconds_streamed - seconds_needed) >= 10:
                    await asyncio.sleep(8)
            # end of the track reached
            # update actual duration to the queue for more accurate now playing info
            accurate_duration = bytes_written / sample_size
            queue_track.duration = accurate_duration
            self.logger.debug(
                "Finished Streaming queue track: %s (%s) on queue %s",
                queue_track.item_id,
                queue_track.name,
                queue.player.name,
            )
        # end of queue reached, pass last fadeout bits to final output
        yield last_fadeout_data
        # END OF QUEUE STREAM
        self.logger.info("Queue stream for Queue %s finished.", queue.queue_id)
