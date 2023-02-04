"""Controller to stream audio to players."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import logging

from dataclasses import dataclass, field
import urllib.parse
from typing import TYPE_CHECKING, Any, AsyncContextManager, AsyncGenerator
import shortuuid

from aiohttp import web
from music_assistant.common.helpers.util import empty_queue

from music_assistant.common.models.enums import (
    ContentType,
    MetadataMode,
    StreamState,
)
from music_assistant.common.models.errors import (
    PlayerUnavailableError,
)

from music_assistant.common.models.media_items import StreamDetails
from music_assistant.common.models.player import Player
from music_assistant.common.models.queue_item import QueueItem


from music_assistant.constants import (
    DEFAULT_PORT,
    ROOT_LOGGER_NAME,
)
from music_assistant.server.helpers.audio import (
    check_audio_support,
    get_chunksize,
    get_media_stream,
    get_preview_stream,
    get_stream_details,
    strip_silence,
)
from music_assistant.server.helpers.process import AsyncProcess

from music_assistant.constants import CONF_WEB_HOST, CONF_WEB_PORT

if TYPE_CHECKING:
    # from music_assistant.common.models.queue_stream import QueueStream
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
        self, queue_item: QueueItem, seek_position: int = 0, fade_in: bool = False
    ) -> None:
        """Initialize MultiQueue instance."""
        self.queue_item = queue_item
        self.seek_position = seek_position
        self.fade_in = fade_in
        self.stream_id = shortuuid.uuid()
        self.expected_consumers = 1
        self._subscribers: list[asyncio.Queue[bytes]] = []
        self.audio_task: asyncio.Task | None = None
        self._start = asyncio.Event()

    @property
    def finished(self) -> bool:
        """Return if this StreamJob is finished."""
        if self.audio_task is None:
            return False
        if not self._start.is_set():
            return False
        return self.audio_task.cancelled() or self.audio_task.done()

    @property
    def pending(self) -> bool:
        """Return if this Job is pending start."""
        return not self._start.is_set()

    async def subscribe(self) -> AsyncGenerator[bytes, None]:
        """Subscribe consumer and iterate incoming chunks on the queue."""
        try:
            sub_queue = asyncio.Queue(10)
            self._subscribers.append(sub_queue)
            if len(self._subscribers) == self.expected_consumers:
                # we reached the number of expected subscribers, release awaiting subscribers
                self._start.set()
            else:
                # wait until all expected subscribers arrived
                # TODO: handle edge case where a player does not connect at all ?!
                await self._start.wait()
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
            if len(self._subscribers) == 0 and self.audio_task and not self.finished:
                self.audio_task.cancel()

    async def put_data(self, data: Any, timeout: float | None) -> None:
        """Put chunk of data to all subscribers."""
        await asyncio.wait(
            *(x.put(data) for x in list(self._subscribers)), timeout=timeout
        )


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

        self.mass.webapp.router.add_get("/stream/preview", self.serve_preview)
        self.mass.webapp.router.add_get(
            "/stream/{player_id}/{queue_item_id}{stream_id}.{fmt}",
            self.serve_queue_item_stream,
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

        LOGGER.info("Started stream controller")

    async def close(self) -> None:
        """Cleanup on exit."""

    @property
    def base_url(self) -> str:
        """Return the base url for the stream engine."""

        host = self.mass.config.get(CONF_WEB_HOST, "127.0.0.1")
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

        - queue_item: the QueueItem that is about to be played (or buffered).
        - player_id: the player_id of the player that will play the stream.
          In case of a multi subscriber stream (e.g. sync/groups),
          call resolve for every child player.
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
            stream_job = StreamJob(
                queue_item, seek_position=seek_position, fade_in=fade_in
            )
            self.stream_jobs[stream_job.stream_id] = stream_job
        # mark this queue item as the one in buffer
        queue = self.mass.players.queues.get(queue_item.queue_id)
        item_index = self.mass.players.queues.get_item(
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

    async def serve_queue_item_stream(self, request: web.Request) -> web.Response:
        """Stream a queue item to player(s)."""
        queue_id = request.match_info["queue_id"]
        queue_item_id = request.match_info["queue_item_id"]
        seek_position = int(request.query.get("seek", "0"))
        player_queue = self.mass.players.queues.get(queue_id)
        player = self.mass.players.get(queue_id)
        if not player_queue or not player:
            raise web.HTTPNotFound(reason="player(queue) not found")

        LOGGER.debug("Stream request for %s", queue_id)

        queue_item = self.mass.players.queues.get_item(queue_id, queue_item_id)
        if not queue_item:
            raise web.HTTPNotFound(reason="invalid queue item_id")

        streamdetails = await get_stream_details(self.mass, queue_item, queue_id)
        pcm_sample_rate = min(streamdetails.sample_rate, player.max_sample_rate)
        output_format_str = request.query.get("fmt", "wav").lower()
        output_format = ContentType.try_parse(output_format_str)
        if output_format_str == "wav":
            output_format_str = (
                f"x-wav;codec=pcm;rate={pcm_sample_rate};"
                f"bitrate={streamdetails.bit_depth};channels={streamdetails.channels}"
            )

        # prepare request
        resp = web.StreamResponse(
            status=200,
            reason="OK",
            headers={"Content-Type": f"audio/{output_format_str}"},
        )
        await resp.prepare(request)

        async for chunk in self._get_queue_item_stream(
            streamdetails,
            player,
            output_format=output_format,
            sample_rate=pcm_sample_rate,
            seek_position=seek_position,
        ):
            await resp.write(chunk)

        return

    async def serve_preview(self, request: web.Request):
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

    async def _get_queue_item_stream(
        self,
        streamdetails: StreamDetails,
        player: Player,
        output_format: ContentType,
        sample_rate: int,
        seek_position: int = 0,
    ) -> AsyncGenerator[bytes, None]:
        """Get (playerspecific) audio stream for a Queue Item."""
        # start streaming
        LOGGER.debug(
            "Start streaming %s (%s) on player %s",
            streamdetails.stream_title,
            streamdetails.uri,
            player.name,
        )
        # determine PCM details
        bit_depth = streamdetails.bit_depth
        channels = streamdetails.channels
        ffmpeg_args = self._get_player_ffmpeg_args(player_id=player)
        async with AsyncProcess(ffmpeg_args, True) as ffmpeg_proc:

            # feed stdin with pcm audio chunks from origin
            async def read_audio():
                # use 5 seconds chunks so we can strip silence
                chunk_size = int(sample_rate * (bit_depth / 8) * channels * 5)
                async for audio_chunk in get_media_stream(
                    mass=self.mass,
                    streamdetails=streamdetails,
                    sample_rate=sample_rate,
                    bit_depth=bit_depth,
                    channels=channels,
                    seek_position=seek_position,
                ):
                    await resp.write(audio_chunk)

            ffmpeg_proc.attach_task(read_audio())

            # read final chunks from stdout
            async for chunk in ffmpeg_proc.iter_any():
                await resp.write(chunk)

        LOGGER.debug(
            "Finished streaming %s (%s) on player %s (queue %s)",
            queue_item_id,
            queue_item.name,
            player.name,
            queue_id,
        )

    def _get_player_ffmpeg_args(
        self,
        player: Player,
        input_format: ContentType,
        channels: int,
        sample_rate: int,
        output_format: ContentType,
    ) -> list[str]:
        """Get player specific arguments for the given input and output details."""
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
            input_format,
            "-ac",
            str(channels),
            "-ar",
            str(sample_rate),
            "-i",
            "-",
        ]
        # TODO: insert filters/convolution/DSP here!
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

    async def _stream_job_runner(self, stream_job: StreamJob) -> None:
        """Feed audio chunks to StreamJob."""
        queue_item = stream_job.queue_item
        streamdetails = queue_item.streamdetails
        await get_stream_details(self.mass, queue_item)

    async def cleanup_stale(self) -> None:
        """Cleanup stale/done stream tasks."""
        stale = set()
        for stream_id, stream in self.queue_streams.items():
            if stream.done.is_set() and not stream.connected_clients:
                stale.add(stream_id)
        for stream_id in stale:
            self.queue_streams.pop(stream_id, None)
