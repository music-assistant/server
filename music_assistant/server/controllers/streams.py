"""Controller to stream audio to players."""
from __future__ import annotations

import asyncio
import logging
import urllib.parse
from typing import TYPE_CHECKING, Any, AsyncGenerator
import shortuuid
from aiohttp import web

from music_assistant.common.models.enums import ContentType
from music_assistant.common.models.player import Player
from music_assistant.common.models.queue_item import QueueItem
from music_assistant.constants import (
    CONF_WEB_HOST,
    CONF_WEB_PORT,
    DEFAULT_PORT,
    ROOT_LOGGER_NAME,
)
from music_assistant.server.helpers.audio import (
    check_audio_support,
    get_media_stream,
    get_preview_stream,
    get_stream_details,
)
from music_assistant.server.helpers.process import AsyncProcess

if TYPE_CHECKING:
    from music_assistant.server import MusicAssistant

LOGGER = logging.getLogger(f"{ROOT_LOGGER_NAME}.streams")


class StreamJob:
    """
    Representation of a (multisubscriber) Audio Queue (item)stream job/task.

    The whole idea here is that in case of a player (sync)group,
    all players receive the exact same PCM audio chunks from the source audio.
    A StreamJob is tied to a queueitem,
    meaning that streaming of each QueueItem will have its own StreamJob.
    In case a QueueItem is restarted (e.g. when seeking), a new StreamJob will be created.
    """

    def __init__(
        self,
        queue_item: QueueItem,
        audio_source: AsyncGenerator[bytes, None],
        pcm_sample_rate: int,
        pcm_bit_depth: int,
        pcm_channels: int,
    ) -> None:
        """Initialize MultiQueue instance."""
        self.queue_item = queue_item
        self.audio_source = audio_source
        self.pcm_sample_rate = pcm_sample_rate
        self.pcm_bit_depth = pcm_bit_depth
        self.pcm_channels = pcm_channels
        self.stream_id = shortuuid.uuid()
        self.expected_consumers = 1
        self._audio_task = asyncio.create_task(self._stream_job_runner())
        self._subscribers: list[asyncio.Queue[bytes]] = []
        self._all_clients_connected = asyncio.Event()

    @property
    def finished(self) -> bool:
        """Return if this StreamJob is finished."""
        if self._audio_task is None:
            return False
        if not self._all_clients_connected.is_set():
            return False
        return self._audio_task.cancelled() or self._audio_task.done()

    @property
    def pending(self) -> bool:
        """Return if this Job is pending start."""
        return not self._all_clients_connected.is_set()

    async def subscribe(self) -> AsyncGenerator[bytes, None]:
        """Subscribe consumer and iterate incoming chunks on the queue."""
        try:
            sub_queue = asyncio.Queue(10)
            self._subscribers.append(sub_queue)
            if len(self._subscribers) == self.expected_consumers:
                # we reached the number of expected subscribers, set event
                # so that chunks can be pushed
                self._all_clients_connected.set()
            else:
                # wait until all expected subscribers arrived
                # TODO: handle edge case where a player does not connect at all ?!
                await self._all_clients_connected.wait()
            # keep reading audio chunks from the queue until we receive an empty one
            while True:
                chunk = await sub_queue.get()
                if chunk == b"":
                    # EOF chunk received
                    break
                yield chunk
        finally:
            # 1 second delay here to account for misbehaving (reconnecting) players
            await asyncio.sleep(1)
            self._subscribers.remove(sub_queue)
            # check if this was the last subscriber and we should cancel
            if len(self._subscribers) == 0 and self._audio_task and not self.finished:
                self._audio_task.cancel()

    async def _put_data(self, data: Any, timeout: float = 600) -> None:
        """Put chunk of data to all subscribers."""
        await asyncio.wait([x.put(data) for x in self._subscribers], timeout=timeout)

    async def _stream_job_runner(self) -> None:
        """Feed audio chunks to StreamJob subscribers."""
        chunk_num = 0
        async for chunk in self.audio_source:
            chunk_num += 1
            if chunk_num == 1:
                # wait until all expected clients are connected
                # TODO: handle edge case where a player does not connect at all ?!
                await asyncio.wait([self._all_clients_connected.wait()], timeout=30)
            await self._put_data(chunk)
        # mark EOF with empty chunk
        await self._put_data(b"")


class StreamsController:
    """Controller to stream audio to players."""

    def __init__(self, mass: MusicAssistant):
        """Initialize instance."""
        self.mass = mass
        # streamjobs contains all active stream jobs
        # there may be multiple jobs for the same queue item (e.g. when seeking)
        # the key is the (unique) stream_id for the StreamJob
        self.stream_jobs: dict[str, StreamJob] = {}

    async def setup(self) -> None:
        """Async initialize of module."""

        self.mass.webapp.router.add_get("/stream/preview", self._serve_preview)
        self.mass.webapp.router.add_get(
            "/stream/{player_id}/{queue_item_id}/{stream_id}.{fmt}",
            self._serve_queue_stream,
        )

        ffmpeg_present, libsoxr_support = await check_audio_support(True)
        if not ffmpeg_present:
            LOGGER.error(
                "FFmpeg binary not found on your system, playback will NOT work!."
            )
        elif not libsoxr_support:
            LOGGER.warning(
                "FFmpeg version found without libsoxr support, "
                "highest quality audio not available. "
            )
        await self._cleanup_stale()
        LOGGER.info("Started stream controller")

    async def close(self) -> None:
        """Cleanup on exit."""

    @property
    def base_url(self) -> str:
        """Return the base url for the stream engine."""

        host = self.mass.config.get(CONF_WEB_HOST, "localhost")
        port = self.mass.config.get(CONF_WEB_PORT, DEFAULT_PORT)
        return f"http://{host}:{port}"

    async def resolve_stream(
        self,
        queue_item: QueueItem,
        player_id: str,
        seek_position: int = 0,
        fade_in: bool = False,
        content_type: ContentType = ContentType.WAV,
    ) -> str:
        """
        Resolve the stream URL for the given QueueItem.

        This is called just-in-time by the player implementation to get the URL to the audio.
        It will create a StreamJob which is a background task responsible for feeding
        the PCM audio chunks to the consumer(s).

        - queue_item: the QueueItem that is about to be played (or buffered).
        - player_id: the player_id of the player that will play the stream.
          In case of a multi subscriber stream (e.g. sync/groups),
          call resolve for every child player.
        - seek_position: start playing from this specific position.
        - fade_in: fade in the music at start (e.g. at resume).
        - content_type: Encode the stream in the given format.
        """
        # check if there is already a pending job
        for stream_job in self.stream_jobs.values():
            if not stream_job.pending:
                continue
            if stream_job.queue_item != queue_item:
                continue
            # if we hit this point, we have a match
            stream_job.expected_consumers += 1
            break
        else:
            # register a new stream job
            streamdetails = await get_stream_details(self.mass, queue_item)
            stream_job = StreamJob(
                queue_item=queue_item,
                audio_source=get_media_stream(
                    self.mass,
                    streamdetails=streamdetails,
                    seek_position=seek_position,
                    fade_in=fade_in,
                ),
                pcm_sample_rate=streamdetails.sample_rate,
                pcm_bit_depth=streamdetails.bit_depth,
                pcm_channels=streamdetails.channels,
            )
            self.stream_jobs[stream_job.stream_id] = stream_job

        # mark this queue item as the one in buffer
        queue = self.mass.players.queues.get(queue_item.queue_id)
        item_index = self.mass.players.queues.index_by_id(
            queue.queue_id, queue_item.queue_item_id
        )
        queue.index_in_buffer = item_index
        # generate player-specific URL for the stream job
        fmt = content_type.value
        url = f"{self.base_url}/stream/{player_id}/{queue_item.queue_item_id}/{stream_job.stream_id}.{fmt}"
        return url

    async def get_preview_url(self, provider: str, track_id: str) -> str:
        """Return url to short preview sample."""
        enc_track_id = urllib.parse.quote(track_id)
        return f"{self.base_url}/preview?provider={provider}&item_id={enc_track_id}"

    async def _serve_queue_stream(self, request: web.Request) -> web.Response:
        """Serve Queue Stream audio to player(s)."""
        stream_id = request.match_info["stream_id"]
        stream_job = self.stream_jobs.get(stream_id)
        if not stream_job:
            raise web.HTTPNotFound(reason=f"Unknown stream_id: {stream_id}")
        player_id = request.match_info["player_id"]
        player = self.mass.players.get(player_id)
        if not player:
            raise web.HTTPNotFound(reason=f"Unknown player_id: {player_id}")

        LOGGER.debug("Start serving audio stream %s to %s", stream_id, player.name)

        output_format_str = request.match_info["fmt"]
        output_format = ContentType.try_parse(output_format_str)
        if output_format_str == "wav":
            output_format_str = (
                f"x-wav;codec=pcm;rate={stream_job.pcm_sample_rate};"
                f"bitrate={stream_job.pcm_bit_depth};channels={stream_job.pcm_channels}"
            )

        # prepare request
        resp = web.StreamResponse(
            status=200,
            reason="OK",
            headers={"Content-Type": f"audio/{output_format_str}"},
        )
        await resp.prepare(request)

        # collect player specific ffmpeg args to re-encode the source PCM stream
        ffmpeg_args = self._get_player_ffmpeg_args(
            player=player,
            pcm_sample_rate=stream_job.pcm_sample_rate,
            pcm_bit_depth=stream_job.pcm_bit_depth,
            pcm_channels=stream_job.pcm_channels,
            output_format=output_format,
        )
        async with AsyncProcess(ffmpeg_args, True) as ffmpeg_proc:

            # feed stdin with pcm audio chunks from origin
            async def read_audio():
                async for chunk in stream_job.subscribe():
                    await ffmpeg_proc.write(chunk)
                ffmpeg_proc.write_eof()

            ffmpeg_proc.attach_task(read_audio())

            # read final chunks from stdout
            async for chunk in ffmpeg_proc.iter_any():
                try:
                    await resp.write(chunk)
                except BrokenPipeError:
                    # connection lost
                    break

        return resp

    async def _serve_preview(self, request: web.Request):
        """Serve short preview sample."""
        provider_mapping = request.query["provider_mapping"]
        item_id = urllib.parse.unquote(request.query["item_id"])
        resp = web.StreamResponse(
            status=200, reason="OK", headers={"Content-Type": "audio/mp3"}
        )
        await resp.prepare(request)
        async for chunk in get_preview_stream(self.mass, provider_mapping, item_id):
            await resp.write(chunk)
        return resp

    def _get_player_ffmpeg_args(
        self,
        player: Player,
        pcm_sample_rate: int,
        pcm_bit_depth: int,
        pcm_channels: int,
        output_format: ContentType,
    ) -> list[str]:
        """Get player specific arguments for the given (pcm) input and output details."""
        # generic args
        ffmpeg_args = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "quiet",
            "-ignore_unknown",
        ]
        # input args
        ffmpeg_args += [
            "-f",
            ContentType.from_bit_depth(pcm_bit_depth).value,
            "-ac",
            str(pcm_channels),
            "-ar",
            str(pcm_sample_rate),
            "-i",
            "-",
        ]
        # TODO: insert playerspecific filters/convolution/DSP here!
        # output args
        ffmpeg_args += [
            # output args
            "-f",
            output_format.value,
            "-compression_level",
            "0",
            "-",
        ]
        return ffmpeg_args

    async def _cleanup_stale(self) -> None:
        """Cleanup stale/done stream tasks."""
        stale = set()
        for stream_id, job in self.stream_jobs.items():
            if job.finished:
                stale.add(stream_id)
        for stream_id in stale:
            self.stream_jobs.pop(stream_id, None)
        # reschedule self to run every 5 minutes
        self.mass.loop.call_later(300, self.mass.create_task, self._cleanup_stale)
