"""Controller to stream audio to players."""
from __future__ import annotations

import asyncio
from asyncio import Task
from typing import Awaitable, Callable, Dict, List, Tuple
from urllib.error import HTTPError

from aiohttp import web
from music_assistant.helpers.audio import (
    create_wave_header,
    crossfade_pcm_parts,
    get_media_stream,
    get_stream_details,
    strip_silence,
)

from music_assistant.helpers.typing import MusicAssistant
from music_assistant.helpers.util import get_ip
from music_assistant.music.models import ContentType


class StreamController:
    """Controller to stream audio to players."""

    def __init__(self, mass: MusicAssistant, port: int = 8095):
        """Initialize instance."""
        self.mass = mass
        self.logger = mass.logger.getChild("stream")
        self._port = port
        self._ip: str = get_ip()
        self._app: web.Application = None
        self._subscribers: Dict[str, Dict[str, List[Callable]]] = {}
        self._stream_tasks: Dict[str, Task] = {}

    def get_stream_url(self, queue_id: str) -> str:
        """Return the full stream url for the PlayerQueue Stream."""
        return f"http://{self._ip}:{self._port}/{queue_id}.wav"

    async def setup(self) -> None:
        """Async initialize of module."""
        self._app = web.Application()
        self._app.router.add_get("/{queue_id}.wav", self.serve_stream_client)
        self._app.router.add_get("/{queue_id}", self.serve_stream_client)
        runner = web.AppRunner(self._app, access_log=None)
        await runner.setup()
        # set host to None to bind to all addresses on both IPv4 and IPv6
        http_site = web.TCPSite(runner, host=None, port=self._port)
        await http_site.start()
        self.logger.info("Started stream server on port %s", self._port)

    async def serve_stream_client(self, request: web.Request):
        """Serve queue audio stream to client."""
        queue_id = request.match_info["queue_id"]
        clientid = request.remote

        if not self.mass.players.get_player_queue(queue_id):
            return web.Response(status=404)

        # prepare request
        resp = web.StreamResponse(
            status=200, reason="OK", headers={"Content-Type": "audio/x-wav"}
        )
        await resp.prepare(request)

        last_chunk_received = asyncio.Event()
        wav_header_written = asyncio.Event()

        async def audio_callback(
            audio_chunk, is_last_part: bool, pcm_args: Tuple[int, int, int]
        ):
            if not wav_header_written.is_set():
                channels, sample_rate, bit_depth = pcm_args
                wav_header = create_wave_header(sample_rate, channels, bit_depth)
                wav_header_written.set()
                await resp.write(wav_header)

            await resp.write(audio_chunk)
            if is_last_part:
                last_chunk_received.set()

        # start delivering audio chunks
        await self.subscribe(queue_id, clientid, audio_callback)
        try:
            await last_chunk_received.wait()
        finally:
            await self.unsubscribe(queue_id, clientid)
        return resp

    async def subscribe(self, queue_id: str, clientid: str, callback: Awaitable):
        """Subscribe client to queue stream."""
        self._subscribers.setdefault(queue_id, {})
        if queue_id in self._subscribers[queue_id]:
            # client is already subscribed ?
            await self.unsubscribe(queue_id, clientid)
        self._subscribers[queue_id][clientid] = callback
        stream_task = self._stream_tasks.get(queue_id)
        if not stream_task or stream_task.cancelled():
            # first connect, start the stream task
            task = asyncio.create_task(self._queue_stream(queue_id))

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
            if task := self._stream_tasks.pop(queue_id, None):
                task.cancel()

    async def _queue_stream(self, queue_id: str) -> None:
        """Stream the PlayerQueue's tracks as constant feed of PCM raw audio."""
        queue = self.mass.players.get_player_queue(queue_id)
        last_fadeout_data = b""
        queue_index = None
        bit_depth: int = None
        sample_rate: int = None
        channels = 2  # hardcoded 2 channels

        async def write_chunk(data: bytes, is_last_chunk: bool = False):
            await asyncio.gather(
                *[
                    x(data, is_last_chunk, (channels, sample_rate, bit_depth))
                    for x in self._subscribers[queue_id].values()
                ]
            )

        # stream queue tracks one by one
        while True:
            # get the (next) track in queue
            if queue_index is None:
                # report start of queue playback so we can calculate current track/duration etc.
                queue_index = await queue.queue_stream_start()
            else:
                queue_index = await queue.queue_stream_next(queue_index)
            queue_track = queue.get_item(queue_index)
            if not queue_track:
                self.logger.debug("no (more) tracks in queue %s", queue_id)
                break
            # get streamdetails
            streamdetails = await get_stream_details(
                self.mass, queue_track, queue.queue_id
            )
            # get the PCM samplerate/bitrate
            if bit_depth is not None and streamdetails.bit_depth > bit_depth:
                break  # bit depth mismatch
            if bit_depth is None or streamdetails.bit_depth <= bit_depth:
                bit_depth = streamdetails.bit_depth
            if sample_rate is not None and streamdetails.sample_rate > sample_rate:
                break  # sample rate mismatch
            if sample_rate is None or streamdetails.sample_rate <= sample_rate:
                sample_rate = streamdetails.sample_rate

            pcm_fmt = ContentType.from_bit_depth(bit_depth)
            sample_size = int(sample_rate * (bit_depth / 8) * channels)  # 1 second
            buffer_size = sample_size * (
                queue.crossfade_duration or 1
            )  # 2...10 seconds

            self.logger.debug(
                "Start Streaming queue track: %s (%s) for player %s",
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
                    await write_chunk(chunk)
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
                    await write_chunk(crossfade_part)
                    bytes_written += len(crossfade_part)
                    del crossfade_part
                    del fade_in_part
                    last_fadeout_data = b""
                    # also write the leftover bytes from the strip action
                    await write_chunk(remaining_bytes)
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
                        await write_chunk(last_part)
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
                            await write_chunk(remaining_bytes)
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
                        await write_chunk(prev_chunk)
                        bytes_written += len(prev_chunk)
                        prev_chunk = chunk
                    else:
                        prev_chunk = chunk
                    del chunk
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
        await write_chunk(last_fadeout_data, True)
        del last_fadeout_data
        # END OF QUEUE STREAM
