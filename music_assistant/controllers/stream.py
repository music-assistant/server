"""Controller to stream audio to players."""
from __future__ import annotations

import asyncio
from asyncio import Task
from dataclasses import dataclass
from time import time
from typing import AsyncGenerator, Awaitable, Callable, Dict, List
from uuid import uuid4

from aiohttp import web
from music_assistant.constants import EventType
from music_assistant.helpers.audio import (
    check_audio_support,
    create_wave_header,
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


@dataclass(frozen=True)
class PCMArgs:
    """Specify raw pcm audio."""

    sample_rate: int
    bit_depth: int
    channels: int


class StreamController:
    """Controller to stream audio to players."""

    def __init__(self, mass: MusicAssistant, port: int = 8095):
        """Initialize instance."""
        self.mass = mass
        self.logger = mass.logger.getChild("stream")
        self._port = port
        self._ip: str = get_ip()
        self._subscribers: Dict[str, Dict[str, List[Callable]]] = {}
        self._stream_tasks: Dict[str, Task] = {}
        self._pcmargs: Dict[str, PCMArgs] = {}

    def get_stream_url(self, queue_id: str) -> str:
        """Return the full stream url for the PlayerQueue Stream."""
        return f"http://{self._ip}:{self._port}/{queue_id}.flac"

    async def setup(self) -> None:
        """Async initialize of module."""
        app = web.Application()

        app.router.add_get("/{queue_id}.wav", self.serve_stream_client_pcm)
        app.router.add_get("/{queue_id}.{format}", self.serve_stream_client)
        app.router.add_get("/{queue_id}", self.serve_stream_client)

        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        # set host to None to bind to all addresses on both IPv4 and IPv6
        http_site = web.TCPSite(
            runner, host=None, port=self._port, reuse_address=True, reuse_port=True
        )
        await http_site.start()

        async def on_shutdown_event(*args, **kwargs):
            """Handle shutdown event."""
            for subscribers in self._subscribers.values():
                for callback in subscribers.values():
                    await callback(b"")
            for task in self._stream_tasks.values():
                task.cancel()
            await http_site.stop()
            await runner.shutdown()
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

    async def serve_stream_client(self, request: web.Request):
        """Serve queue audio stream to client (encoded to FLAC or MP3)."""
        queue_id = request.match_info["queue_id"]
        clientid = f'{request.remote}_{request.query.get("playerid", str(uuid4()))}'
        fmt = request.match_info.get("format", "flac")

        if self.mass.players.get_player_queue(queue_id) is None:
            return web.Response(status=404)

        # prepare request
        resp = web.StreamResponse(
            status=200, reason="OK", headers={"Content-Type": f"audio/{fmt}"}
        )
        await resp.prepare(request)

        pcmargs = await self._get_queue_stream_pcm_args(queue_id)
        output_fmt = ContentType(fmt)
        sox_args = await get_sox_args_for_pcm_stream(
            pcmargs.sample_rate,
            pcmargs.bit_depth,
            pcmargs.channels,
            output_format=output_fmt,
        )
        try:
            # get the raw pcm bytes from the queue stream and on the fly encode as flac
            # send the flac endoded stream to the subscribers.
            async with AsyncProcess(sox_args, True) as sox_proc:

                async def reader():
                    # task that reads flac endoded chunks from the subprocess
                    chunksize = 32000 if output_fmt == ContentType.MP3 else 256000
                    async for audio_chunk in sox_proc.iterate_chunks(chunksize):
                        await resp.write(audio_chunk)

                # feed raw pcm chunks into sox/ffmpeg to encode to flac
                async def audio_callback(audio_chunk):
                    if audio_chunk == b"":
                        sox_proc.write_eof()
                        return
                    await sox_proc.write(audio_chunk)

                # wait for the output task to complete
                await self.subscribe(queue_id, clientid, audio_callback)
                await reader()

        finally:
            await self.unsubscribe(queue_id, clientid)
        return resp

    async def serve_stream_client_pcm(self, request: web.Request):
        """Serve queue audio stream to client in the raw PCM format."""
        queue_id = request.match_info["queue_id"]
        queue = self.mass.players.get_player_queue(queue_id)
        clientid = f'{request.remote}_{request.query.get("playerid", str(uuid4()))}'

        if queue is None:
            return web.Response(status=404)

        # prepare request
        pcmargs = await self._get_queue_stream_pcm_args(queue_id, 32)
        fmt = f"x-wav;codec=pcm;rate={pcmargs.sample_rate};bitrate={pcmargs.bit_depth};channels={pcmargs.channels}"
        resp = web.StreamResponse(
            status=200,
            reason="OK",
            headers={"Content-Type": f"audio/{fmt}"},
        )
        await resp.prepare(request)

        # write wave header
        wav_header = create_wave_header(
            pcmargs.sample_rate,
            pcmargs.channels,
            pcmargs.bit_depth,
        )
        await resp.write(wav_header)

        # start delivering audio chunks
        last_chunk_received = asyncio.Event()
        try:

            async def audio_callback(audio_chunk):
                if audio_chunk == b"":
                    last_chunk_received.set()
                    return
                try:
                    await resp.write(audio_chunk)
                except BrokenPipeError:
                    pass  # race condition

            await self.subscribe(queue_id, clientid, audio_callback)
            await last_chunk_received.wait()
        finally:
            await self.unsubscribe(queue_id, clientid)
        return resp

    async def subscribe(
        self, queue_id: str, clientid: str, callback: Awaitable
    ) -> None:
        """Subscribe client to queue stream."""
        self._subscribers.setdefault(queue_id, {})
        if queue_id in self._subscribers[queue_id]:
            # client is already subscribed ?
            await self.unsubscribe(queue_id, clientid)
        self._subscribers[queue_id][clientid] = callback
        stream_task = self._stream_tasks.get(queue_id)
        if not stream_task or stream_task.cancelled():
            # first connect, start the stream task
            task = asyncio.create_task(self.start_queue_stream(queue_id))

            def task_done_callback(*args, **kwargs):
                self._stream_tasks.pop(queue_id, None)

            task.add_done_callback(task_done_callback)
            self._stream_tasks[queue_id] = task

        self.logger.debug("Subscribed client %s to queue stream %s", clientid, queue_id)

    async def unsubscribe(self, queue_id: str, clientid: str):
        """Unsubscribe client from queue stream."""
        self._subscribers[queue_id].pop(clientid, None)
        self.logger.debug(
            "Unsubscribed client %s from queue stream %s", clientid, queue_id
        )
        if len(self._subscribers[queue_id]) == 0:
            # no more clients, cancel stream task
            self.logger.debug(
                "Aborted queue stream %s due to no more clients", queue_id
            )
            if task := self._stream_tasks.pop(queue_id, None):
                task.cancel()
            self._pcmargs.pop(queue_id, None)

    async def start_queue_stream(self, queue_id: str) -> None:
        """Start the Queue stream feeding callbacks of listeners.."""
        queue = self.mass.players.get_player_queue(queue_id)
        pcmargs = await self._get_queue_stream_pcm_args(queue_id)

        self.logger.info(
            "Starting Queue stream for Queue %s with args: %s", queue_id, pcmargs
        )
        async for chunk in self._get_queue_stream(
            queue,
            pcmargs.sample_rate,
            pcmargs.bit_depth,
            pcmargs.channels,
        ):
            if len(self._subscribers[queue_id].values()) == 0:
                self.logger.info("Queue stream for Queue %s aborted", queue_id)
                return
            await asyncio.gather(
                *[cb(chunk) for cb in list(self._subscribers[queue_id].values())]
            )
        self.logger.info("Queue stream for Queue %s finished.", queue_id)
        # send empty chunk to inform EOF
        await asyncio.gather(
            *[cb(b"") for cb in list(self._subscribers[queue_id].values())]
        )

    async def _get_queue_stream_pcm_args(
        self, queue_id: str, forced_bit_depth: int = None
    ) -> PCMArgs:
        """Return the current/ext PCM args for the queue stream."""
        if queue_id in self._pcmargs:
            return self._pcmargs[queue_id]
        queue = self.mass.players.get_player_queue(queue_id)
        next_streamdetails = await queue.queue_stream_prepare()
        pcmargs = PCMArgs(
            sample_rate=min(next_streamdetails.sample_rate, queue.max_sample_rate),
            bit_depth=forced_bit_depth or next_streamdetails.bit_depth,
            channels=2,
        )
        self._pcmargs[queue_id] = pcmargs
        return pcmargs

    async def _get_queue_stream(
        self, queue: PlayerQueue, sample_rate: int, bit_depth: int, channels: int = 2
    ) -> AsyncGenerator[None, bytes]:
        """Stream the PlayerQueue's tracks as constant feed of PCM raw audio."""
        last_fadeout_data = b""
        queue_index = None
        track_count = 0
        start_timestamp = time()

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

            # get the PCM samplerate/bitrate
            if streamdetails.bit_depth > bit_depth:
                await queue.queue_stream_signal_next()
                self.logger.debug("Abort queue stream due to bit depth mismatch")
                await queue.queue_stream_signal_next()
                break
            if (
                streamdetails.sample_rate > sample_rate
                and streamdetails.sample_rate <= queue.max_sample_rate
            ):
                self.logger.debug("Abort queue stream due to sample rate mismatch")
                await queue.queue_stream_signal_next()
                break

            pcm_fmt = ContentType.from_bit_depth(bit_depth)
            sample_size = int(sample_rate * (bit_depth / 8) * channels)  # 1 second
            buffer_size = sample_size * (
                queue.crossfade_duration or 1
            )  # 1...10 seconds

            self.logger.debug(
                "Start Streaming queue track: %s (%s) for player %s - PCM format: %s - rate: %s",
                queue_track.item_id,
                queue_track.name,
                queue.player.name,
                pcm_fmt.value,
                sample_rate,
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
                seconds_per_chunk = buffer_size / sample_size
                seconds_needed = int(time() - start_timestamp + seconds_per_chunk)
                if (seconds_streamed) > seconds_needed:
                    await asyncio.sleep(seconds_per_chunk / 2)
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
