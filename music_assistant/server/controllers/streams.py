"""Controller to stream audio to players."""
from __future__ import annotations

import asyncio
import logging
import urllib.parse
from typing import TYPE_CHECKING, Any, AsyncGenerator

import shortuuid
from aiohttp import web

from music_assistant.common.helpers.util import empty_queue
from music_assistant.common.models.enums import ContentType
from music_assistant.common.models.errors import MediaNotFoundError, QueueEmpty
from music_assistant.common.models.queue_item import QueueItem
from music_assistant.constants import (
    CONF_EQ_BASS,
    CONF_EQ_MID,
    CONF_EQ_TREBLE,
    CONF_OUTPUT_CHANNELS,
    FALLBACK_DURATION,
    ROOT_LOGGER_NAME,
)
from music_assistant.server.helpers.audio import (
    check_audio_support,
    crossfade_pcm_parts,
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

    _audio_task: asyncio.Task | None = None

    def __init__(
        self,
        queue_item: QueueItem,
        pcm_sample_rate: int,
        pcm_bit_depth: int,
        audio_source: AsyncGenerator[bytes, None] | None = None,
        flow_mode: bool = False,
    ) -> None:
        """Initialize MultiQueue instance."""
        self.queue_item = queue_item
        self.audio_source = audio_source
        # internally all audio within MA is raw PCM, hence the pcm details
        self.pcm_sample_rate = pcm_sample_rate
        self.pcm_bit_depth = pcm_bit_depth
        self.stream_id = shortuuid.uuid()
        self.expected_consumers: set[str] = set()
        self.flow_mode = flow_mode
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
        if not self._audio_task:
            self._audio_task = asyncio.create_task(self._stream_job_runner())
        try:
            sub_queue = asyncio.Queue(10)
            self._subscribers.append(sub_queue)
            if len(self._subscribers) == len(self.expected_consumers):
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
            empty_queue(sub_queue)
            self._subscribers.remove(sub_queue)
            # 1 second delay here to account for misbehaving (reconnecting) players
            await asyncio.sleep(2)
            # check if this was the last subscriber and we should cancel
            if len(self._subscribers) == 0 and self._audio_task and not self.finished:
                self._audio_task.cancel()

    async def _put_data(self, data: Any, timeout: float = 600) -> None:
        """Put chunk of data to all subscribers."""
        async with asyncio.timeout(timeout):
            async with asyncio.TaskGroup() as tg:
                for sub in self._subscribers:
                    tg.create_task(sub.put(data))

    async def _stream_job_runner(self) -> None:
        """Feed audio chunks to StreamJob subscribers."""
        chunk_num = 0
        async for chunk in self.audio_source:
            chunk_num += 1
            if chunk_num == 1:
                # wait until all expected clients are connected
                # TODO: handle edge case where a player does not connect at all ?!
                async with asyncio.timeout(30):
                    await self._all_clients_connected.wait()
            await self._put_data(chunk)
        # mark EOF with empty chunk
        await self._put_data(b"")

    def start(self) -> None:
        """Start running the stream job."""
        self._audio_task = asyncio.create_task(self._stream_job_runner())


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

    async def resolve_stream_url(
        self,
        queue_item: QueueItem,
        player_id: str,
        seek_position: int = 0,
        fade_in: bool = False,
        content_type: ContentType = ContentType.WAV,
        auto_start_runner: bool = True,
        flow_mode: bool = False,
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
        - auto_start_runner: Start the audio stream in advance (stream track now).
        - flow_mode: enable flow mode where the queue tracks are streamed as continuous stream.
        """
        # check if there is already a pending job
        for stream_job in self.stream_jobs.values():
            if stream_job.finished:
                continue
            if stream_job.queue_item.queue_id != queue_item.queue_id:
                continue
            if stream_job.queue_item.queue_item_id != queue_item.queue_item_id:
                continue
            # if we hit this point, we have a match
            stream_job.expected_consumers.add(player_id)
            break
        else:
            # register a new stream job
            if flow_mode:
                # flow mode streamjob
                sample_rate = 48000  # TODO: make this confgurable ?
                bit_depth = 24
                stream_job = StreamJob(
                    queue_item=queue_item,
                    pcm_sample_rate=sample_rate,
                    pcm_bit_depth=bit_depth,
                    flow_mode=True,
                )
                stream_job.audio_source = self._get_flow_stream(
                    stream_job, seek_position=seek_position, fade_in=fade_in
                )
            else:
                # regular streamjob
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
                )
            stream_job.expected_consumers.add(player_id)
            self.stream_jobs[stream_job.stream_id] = stream_job
            if auto_start_runner:
                stream_job.start()

        # mark this queue item as the one in buffer
        queue = self.mass.players.queues.get(queue_item.queue_id)
        item_index = self.mass.players.queues.index_by_id(
            queue.queue_id, queue_item.queue_item_id
        )
        queue.index_in_buffer = item_index
        # generate player-specific URL for the stream job
        fmt = content_type.value
        url = f"{self.mass.base_url}/stream/{player_id}/{queue_item.queue_item_id}/{stream_job.stream_id}.{fmt}"
        return url

    async def get_preview_url(self, provider: str, track_id: str) -> str:
        """Return url to short preview sample."""
        enc_track_id = urllib.parse.quote(track_id)
        return (
            f"{self.mass.base_url}/preview?provider={provider}&item_id={enc_track_id}"
        )

    async def _serve_queue_stream(self, request: web.Request) -> web.Response:
        """Serve Queue Stream audio to player(s)."""
        player_id = request.match_info["player_id"]
        player = self.mass.players.get(player_id)
        if not player:
            raise web.HTTPNotFound(reason=f"Unknown player_id: {player_id}")
        stream_id = request.match_info["stream_id"]
        stream_job = self.stream_jobs.get(stream_id)
        if not stream_job or stream_job.finished:
            # TODO: do we want to create a new stream job on the fly for this queue item if this happens?
            LOGGER.error(
                "Got stream request for an already finished stream job for player %s",
                player.display_name,
            )
            queue = self.mass.players.queues.get_active_queue(player_id)
            if queue.current_index is not None:
                await self.mass.players.queues.resume(queue.queue_id)
            raise web.HTTPNotFound(reason=f"Unknown stream_id: {stream_id}")

        output_format_str = request.match_info["fmt"]
        output_format = ContentType.try_parse(output_format_str)
        if output_format == ContentType.PCM:
            # resolve generic pcm type
            output_format = ContentType.from_bit_depth(stream_job.pcm_bit_depth)
        if output_format.is_pcm() or output_format == ContentType.WAV:
            output_channels = self.mass.config.get_player_config_value(
                player_id, CONF_OUTPUT_CHANNELS
            ).value
            channels = 1 if output_channels != "stereo" else 2
            output_format_str = (
                f"x-wav;codec=pcm;rate={stream_job.pcm_sample_rate};"
                f"bitrate={stream_job.pcm_bit_depth};channels={channels}"
            )

        # prepare request, add some DLNA/UPNP compatible headers
        wants_icy = request.headers.get("Icy-MetaData", "") == "1"
        enable_icy = wants_icy and not output_format.is_lossless()
        headers = {
            "Content-Type": f"audio/{output_format_str}",
            "transferMode.dlna.org": "Streaming",
            "contentFeatures.dlna.org": "DLNA.ORG_OP=00;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=0d500000000000000000000000000000",
            "Cache-Control": "no-cache",
        }
        if enable_icy:
            headers["icy-name"] = "Music Assistant"
            headers["icy-pub"] = "1"
            headers["icy-metaint"] = "8192"

        resp = web.StreamResponse(
            status=200,
            reason="OK",
            headers=headers,
        )
        await resp.prepare(request)

        # return early if this is only a HEAD request
        if request.method.upper() == "HEAD":
            return resp

        LOGGER.debug("Start serving audio stream %s to %s", stream_id, player.name)

        # collect player specific ffmpeg args to re-encode the source PCM stream
        ffmpeg_args = self._get_player_ffmpeg_args(
            player.player_id,
            pcm_sample_rate=stream_job.pcm_sample_rate,
            pcm_bit_depth=stream_job.pcm_bit_depth,
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
            iterator = (
                ffmpeg_proc.iter_chunked(8192) if enable_icy else ffmpeg_proc.iter_any()
            )
            async for chunk in iterator:
                try:
                    await resp.write(chunk)
                    if not enable_icy:
                        continue

                    # if icy metadata is enabled, send the icy metadata after the chunk
                    item_in_buf = stream_job.queue_item
                    if item_in_buf and item_in_buf.streamdetails.stream_title:
                        title = item_in_buf.streamdetails.stream_title
                    elif item_in_buf and item_in_buf.name:
                        title = item_in_buf.name
                    else:
                        title = "Music Assistant"
                    metadata = f"StreamTitle='{title}';".encode()
                    while len(metadata) % 16 != 0:
                        metadata += b"\x00"
                    length = len(metadata)
                    length_b = chr(int(length / 16)).encode()
                    await resp.write(length_b + metadata)

                except (BrokenPipeError, ConnectionResetError):
                    # connection lost
                    break

        return resp

    async def _get_flow_stream(
        self,
        stream_job: StreamJob,
        seek_position: int = 0,
        fade_in: bool = False,
    ) -> AsyncGenerator[bytes, None]:
        """Get a flow stream of all tracks in the queue."""

        queue = self.mass.players.queues.get(stream_job.queue_item.queue_id)
        queue_track = None
        last_fadeout_part = b""

        LOGGER.info("Start Queue Flow stream for Queue %s", queue.display_name)

        while True:
            # get (next) queue item to stream
            if queue_track is None:
                queue_track = stream_job.queue_item
                use_crossfade = queue.crossfade_enabled
            else:
                seek_position = 0
                fade_in = False
                try:
                    (
                        queue_track,
                        use_crossfade,
                    ) = self.mass.players.queues.player_ready_for_next_track(
                        queue.queue_id, queue_track.queue_item_id
                    )
                except QueueEmpty:
                    break

            # store reference to the current queueitem on the streamjob
            stream_job.queue_item = queue_track

            # get streamdetails
            try:
                streamdetails = await get_stream_details(self.mass, queue_track)
            except MediaNotFoundError as err:
                # streamdetails retrieval failed, skip to next track instead of bailing out...
                LOGGER.warning(
                    "Skip track %s due to missing streamdetails",
                    queue_track.name,
                    exc_info=err,
                )
                continue

            LOGGER.debug(
                "Start Streaming queue track: %s (%s) for queue %s - crossfade: %s",
                streamdetails.uri,
                queue_track.name,
                queue.display_name,
                use_crossfade,
            )

            # set some basic vars
            sample_rate = stream_job.pcm_sample_rate
            bit_depth = stream_job.pcm_bit_depth
            pcm_sample_size = int(sample_rate * (bit_depth / 8) * 2)
            crossfade_duration = 10
            crossfade_size = int(pcm_sample_size * crossfade_duration)
            queue_track.streamdetails.seconds_skipped = seek_position
            # predict total size to expect for this track from duration
            stream_duration = (
                queue_track.duration or FALLBACK_DURATION
            ) - seek_position
            # buffer_duration has some overhead to account for padded silence
            buffer_duration = (crossfade_duration + 4) if use_crossfade else 4

            buffer = b""
            bytes_written = 0
            chunk_num = 0
            # handle incoming audio chunks
            async for chunk in get_media_stream(
                self.mass,
                streamdetails,
                seek_position=seek_position,
                fade_in=fade_in,
                sample_rate=sample_rate,
                bit_depth=bit_depth,
            ):

                chunk_num += 1
                seconds_in_buffer = len(buffer) / pcm_sample_size

                ####  HANDLE FIRST PART OF TRACK

                # buffer full for crossfade
                if last_fadeout_part and (seconds_in_buffer >= buffer_duration):
                    first_part = buffer + chunk
                    # perform crossfade
                    fadein_part = first_part[:crossfade_size]
                    remaining_bytes = first_part[crossfade_size:]
                    crossfade_part = await crossfade_pcm_parts(
                        fadein_part,
                        last_fadeout_part,
                        bit_depth,
                        sample_rate,
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
                LOGGER.warning("Stream error on %s", streamdetails.uri)
                queue_track.streamdetails.seconds_streamed = 0
                continue

            if buffer:
                if use_crossfade:
                    # if crossfade is enabled, save fadeout part to pickup for next track
                    last_fadeout_part = buffer
                else:
                    # no crossfade enabled, just yield the buffer last part
                    yield buffer
                    bytes_written += len(buffer)

            # end of the track reached - store accurate duration
            buffer = b""
            queue_track.streamdetails.seconds_streamed = bytes_written / pcm_sample_size
            LOGGER.debug(
                "Finished Streaming queue track: %s (%s) on queue %s",
                queue_track.streamdetails.uri,
                queue_track.name,
                queue.display_name,
            )

        LOGGER.info("Finished Queue Flow stream for Queue %s", queue.display_name)

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
        player_id: str,
        pcm_sample_rate: int,
        pcm_bit_depth: int,
        output_format: ContentType,
    ) -> list[str]:
        """Get player specific arguments for the given (pcm) input and output details."""
        player_conf = self.mass.config.get_player_config(player_id)
        conf_channels = player_conf.get_value(CONF_OUTPUT_CHANNELS)
        # generic args
        generic_args = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "quiet",
            "-ignore_unknown",
        ]
        # input args
        input_args = [
            "-f",
            ContentType.from_bit_depth(pcm_bit_depth).value,
            "-ac",
            "2",
            "-ar",
            str(pcm_sample_rate),
            "-i",
            "-",
        ]
        # output args
        output_args = [
            # output args
            "-f",
            output_format.value,
            "-ac",
            "1" if conf_channels != "stereo" else "2",
            "-compression_level",
            "0",
            "-",
        ]
        # collect extra and filter args
        # TODO: add convolution/DSP/roomcorrections here!
        extra_args = []
        filter_params = []

        # the below is a very basic 3-band equalizer, this could be a lot more sophisticated at some point
        if eq_bass := player_conf.get_value(CONF_EQ_BASS):
            filter_params.append(
                f"equalizer=frequency=100:width=200:width_type=h:gain={eq_bass}"
            )
        if eq_mid := player_conf.get_value(CONF_EQ_MID):
            filter_params.append(
                f"equalizer=frequency=900:width=1800:width_type=h:gain={eq_mid}"
            )
        if eq_treble := player_conf.get_value(CONF_EQ_TREBLE):
            filter_params.append(
                f"equalizer=frequency=9000:width=18000:width_type=h:gain={eq_treble}"
            )
        # handle output mixing only left or right
        if conf_channels == "left":
            filter_params.append("pan=mono|c0=FL")
        elif conf_channels == "right":
            filter_params.append("pan=mono|c0=FR")

        if filter_params:
            extra_args += ["-af", ",".join(filter_params)]

        return generic_args + input_args + extra_args + output_args

    async def _cleanup_stale(self) -> None:
        """Cleanup stale/done stream tasks."""
        stale = set()
        for stream_id, job in self.stream_jobs.items():
            if job.finished:
                stale.add(stream_id)
        for stream_id in stale:
            self.stream_jobs.pop(stream_id, None)

        # reschedule self to run every 5 minutes
        def reschedule():
            self.mass.create_task(self._cleanup_stale())

        self.mass.loop.call_later(300, reschedule)
