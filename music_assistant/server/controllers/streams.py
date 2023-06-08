"""Controller to stream audio to players."""
from __future__ import annotations

import asyncio
import logging
import urllib.parse
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any

import shortuuid
from aiohttp import web

from music_assistant.common.helpers.util import empty_queue
from music_assistant.common.models.enums import ContentType, PlayerState
from music_assistant.common.models.errors import MediaNotFoundError, QueueEmpty
from music_assistant.common.models.queue_item import QueueItem
from music_assistant.constants import (
    CONF_EQ_BASS,
    CONF_EQ_MID,
    CONF_EQ_TREBLE,
    CONF_OUTPUT_CHANNELS,
    CONF_OUTPUT_CODEC,
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
    from music_assistant.common.models.player import Player
    from music_assistant.server import MusicAssistant

LOGGER = logging.getLogger(f"{ROOT_LOGGER_NAME}.streams")


class StreamJob:
    """Representation of a (multisubscriber) Audio Queue (item)stream job/task.

    The whole idea here is that in case of a player (sync)group,
    all players receive the exact same PCM audio chunks from the source audio.
    A StreamJob is tied to a queueitem,
    meaning that streaming of each QueueItem will have its own StreamJob.
    In case a QueueItem is restarted (e.g. when seeking), a new StreamJob will be created.
    """

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
        self.pcm_sample_size = int(pcm_sample_rate * (pcm_bit_depth / 8) * 2)
        self.stream_id = shortuuid.uuid()
        self.expected_consumers: set[str] = set()
        self.flow_mode = flow_mode
        self.subscribers: dict[str, asyncio.Queue[bytes]] = {}
        self._all_clients_connected = asyncio.Event()
        self._audio_task: asyncio.Task | None = None
        self.seen_players: set[str] = set()

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

    @property
    def running(self) -> bool:
        """Return if this Job is running."""
        return not self.finished and not self.pending

    async def subscribe(self, player_id: str) -> AsyncGenerator[bytes, None]:
        """Subscribe consumer and iterate incoming chunks on the queue."""
        self.start()
        self.seen_players.add(player_id)
        try:
            sub_queue = asyncio.Queue(1)

            # some checks
            assert player_id not in self.subscribers, "No duplicate subscriptions allowed"
            assert not self.finished, "Already finished"
            assert not self.running, "Already running"

            self.subscribers[player_id] = sub_queue
            if len(self.subscribers) == len(self.expected_consumers):
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
            self.subscribers.pop(player_id)
            # some delay here to detect misbehaving (reconnecting) players
            await asyncio.sleep(2)
            # check if this was the last subscriber and we should cancel
            if len(self.subscribers) == 0 and self._audio_task and not self.finished:
                self._audio_task.cancel()

    async def _put_data(self, data: Any, timeout: float = 120) -> None:
        """Put chunk of data to all subscribers."""
        async with asyncio.timeout(timeout):
            while len(self.subscribers) == 0:
                # this may happen with misbehaving clients that do
                # multiple GET requests for the same audio stream.
                # they receive the first chunk, disconnect and then
                # directly reconnect again.
                if not self._audio_task or self.finished:
                    return
                await asyncio.sleep(0.1)
            async with asyncio.TaskGroup() as tg:
                for sub_id in self.subscribers:
                    sub_queue = self.subscribers[sub_id]
                    tg.create_task(sub_queue.put(data))

    async def _stream_job_runner(self) -> None:
        """Feed audio chunks to StreamJob subscribers."""
        chunk_num = 0
        async for chunk in self.audio_source:
            chunk_num += 1
            if chunk_num == 1:
                # wait until all expected clients are connected
                try:
                    async with asyncio.timeout(10):
                        await self._all_clients_connected.wait()
                except TimeoutError as err:
                    if len(self.subscribers) == 0:
                        raise TimeoutError("Clients did not connect within 10 seconds.") from err
                    self._all_clients_connected.set()
                    LOGGER.warning(
                        "Starting stream job %s but not all clients connected within 10 seconds."
                    )

            await self._put_data(chunk)

        # mark EOF with empty chunk
        await self._put_data(b"")

    def start(self) -> None:
        """Start running the stream job."""
        if self._audio_task:
            return
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
        # some players do multiple GET requests for the same audio stream
        # to determine content type or content length
        # we try to detect/report these players and workaround it.
        # if a player_id is in the below set of player_ids, the first GET request
        # of that player will be ignored and audio is served only in the 2nd request
        self.workaround_players: set[str] = set()

    async def setup(self) -> None:
        """Async initialize of module."""
        ffmpeg_present, libsoxr_support, version = await check_audio_support()
        if not ffmpeg_present:
            LOGGER.error("FFmpeg binary not found on your system, playback will NOT work!.")
        elif not libsoxr_support:
            LOGGER.warning(
                "FFmpeg version found without libsoxr support, "
                "highest quality audio not available. "
            )
        await self._cleanup_stale()
        LOGGER.info(
            "Started stream controller (using ffmpeg version %s %s)",
            version,
            "with libsoxr support" if libsoxr_support else "",
        )

    async def close(self) -> None:
        """Cleanup on exit."""

    async def resolve_stream_url(
        self,
        queue_item: QueueItem,
        player_id: str,
        seek_position: int = 0,
        fade_in: bool = False,
        auto_start_runner: bool = True,
        flow_mode: bool = False,
        output_codec: ContentType | None = None,
    ) -> str:
        """Resolve the stream URL for the given QueueItem.

        This is called just-in-time by the player implementation to get the URL to the audio.
        It will create a StreamJob which is a background task responsible for feeding
        the PCM audio chunks to the consumer(s).

        - queue_item: the QueueItem that is about to be played (or buffered).
        - player_id: the player_id of the player that will play the stream.
          In case of a multi subscriber stream (e.g. sync/groups),
          call resolve for every child player.
        - seek_position: start playing from this specific position.
        - fade_in: fade in the music at start (e.g. at resume).
        - auto_start_runner: Start the audio stream in advance (stream track now).
        - flow_mode: enable flow mode where the queue tracks are streamed as continuous stream.
        - output_codec: Encode the stream in the given format (None for auto select).
        """
        # check if there is already a pending job
        for stream_job in self.stream_jobs.values():
            if stream_job.finished or stream_job.running:
                continue
            if stream_job.queue_item.queue_id != queue_item.queue_id:
                continue
            if stream_job.queue_item.queue_item_id != queue_item.queue_item_id:
                continue
            # if we hit this point, we have a match
            break
        else:
            # register a new stream job
            if flow_mode:
                # flow mode streamjob
                sample_rate = 48000  # hardcoded for now
                bit_depth = 24  # hardcoded for now
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

        # generate player-specific URL for the stream job
        if output_codec is None:
            output_codec = ContentType(
                await self.mass.config.get_player_config_value(player_id, CONF_OUTPUT_CODEC)
            )
        fmt = output_codec.value
        url = f"{self.mass.webserver.base_url}/stream/{player_id}/{queue_item.queue_item_id}/{stream_job.stream_id}.{fmt}"  # noqa: E501
        # handle pcm
        if output_codec.is_pcm():
            player = self.mass.players.get(player_id)
            output_sample_rate = min(stream_job.pcm_sample_rate, player.max_sample_rate)
            player_max_bit_depth = 32 if player.supports_24bit else 16
            output_bit_depth = min(stream_job.pcm_bit_depth, player_max_bit_depth)
            output_channels = await self.mass.config.get_player_config_value(
                player_id, CONF_OUTPUT_CHANNELS
            )
            channels = 1 if output_channels != "stereo" else 2
            url += (
                f";codec=pcm;rate={output_sample_rate};"
                f"bitrate={output_bit_depth};channels={channels}"
            )
        return url

    def get_preview_url(self, provider_instance_id_or_domain: str, track_id: str) -> str:
        """Return url to short preview sample."""
        enc_track_id = urllib.parse.quote(track_id)
        return (
            f"{self.mass.webserver.base_url}/stream/preview?"
            f"provider={provider_instance_id_or_domain}&item_id={enc_track_id}"
        )

    async def serve_queue_stream(self, request: web.Request) -> web.Response:
        """Serve Queue Stream audio to player(s)."""
        LOGGER.debug(
            "Got %s request to %s from %s\nheaders: %s\n",
            request.method,
            request.path,
            request.remote,
            request.headers,
        )
        player_id = request.match_info["player_id"]
        player = self.mass.players.get(player_id)
        queue = self.mass.players.queues.get_active_queue(player_id)
        if not player:
            raise web.HTTPNotFound(reason=f"Unknown player_id: {player_id}")
        stream_id = request.match_info["stream_id"]
        stream_job = self.stream_jobs.get(stream_id)
        if not stream_job or stream_job.finished:
            # Player is trying to play a stream that already exited
            if player.state == PlayerState.PAUSED:
                await self.mass.players.queues.resume(player_id)
            LOGGER.warning(
                "Got stream request for an already finished stream job for player %s",
                player.display_name,
            )
            raise web.HTTPNotFound(reason=f"Unknown stream_id: {stream_id}")

        output_format_str = request.match_info["fmt"]
        output_format = ContentType.try_parse(output_format_str)
        output_sample_rate = min(stream_job.pcm_sample_rate, player.max_sample_rate)
        player_max_bit_depth = 32 if player.supports_24bit else 16
        output_bit_depth = min(stream_job.pcm_bit_depth, player_max_bit_depth)
        if output_format == ContentType.PCM:
            # resolve generic pcm type
            output_format = ContentType.from_bit_depth(output_bit_depth)
        if output_format.is_pcm() or output_format == ContentType.WAV:
            output_channels = await self.mass.config.get_player_config_value(
                player_id, CONF_OUTPUT_CHANNELS
            )
            channels = 1 if output_channels != "stereo" else 2
            output_format_str = (
                f"x-wav;codec=pcm;rate={output_sample_rate};"
                f"bitrate={output_bit_depth};channels={channels}"
            )

        # prepare request, add some DLNA/UPNP compatible headers
        enable_icy = request.headers.get("Icy-MetaData", "") == "1"
        icy_meta_interval = 65536 if output_format.is_lossless() else 8192
        headers = {
            "Content-Type": f"audio/{output_format_str}",
            "transferMode.dlna.org": "Streaming",
            "contentFeatures.dlna.org": "DLNA.ORG_OP=00;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=0d500000000000000000000000000000",  # noqa: E501
            "Cache-Control": "no-cache",
            "Connection": "close",
            "icy-name": "Music Assistant",
            "icy-pub": "1",
        }
        if enable_icy:
            headers["icy-metaint"] = str(icy_meta_interval)

        resp = web.StreamResponse(
            status=200,
            reason="OK",
            headers=headers,
        )
        await resp.prepare(request)

        # return early if this is only a HEAD request
        if request.method == "HEAD":
            return resp

        # handle workaround for players that do 2 multiple GET requests
        # for the same audio stream (because of the missing duration/length)
        if player_id in self.workaround_players and player_id not in stream_job.seen_players:
            stream_job.seen_players.add(player_id)
            return resp

        # guard for the same player connecting multiple times for the same stream
        if player_id in stream_job.subscribers:
            LOGGER.error(
                "Player %s is making multiple requests for the same stream,"
                " please create an issue report on the Music Assistant issue tracker.",
                player.display_name,
            )
            # add the player to the list of players that need the workaround
            self.workaround_players.add(player_id)
            raise web.HTTPBadRequest(reason="Multiple connections are not allowed.")
        if stream_job.running:
            LOGGER.error(
                "Player %s is making a request for an already running stream,"
                " please create an issue report on the Music Assistant issue tracker.",
                player.display_name,
            )
            self.mass.create_task(self.mass.players.queues.next(player_id))
            raise web.HTTPBadRequest(reason="Stream is already running.")

        # all checks passed, start streaming!
        LOGGER.debug("Start serving audio stream %s to %s", stream_id, player.name)

        # collect player specific ffmpeg args to re-encode the source PCM stream
        ffmpeg_args = await self._get_player_ffmpeg_args(
            player,
            input_sample_rate=stream_job.pcm_sample_rate,
            input_bit_depth=stream_job.pcm_bit_depth,
            output_format=output_format,
            output_sample_rate=output_sample_rate,
        )

        async with AsyncProcess(ffmpeg_args, True) as ffmpeg_proc:
            # feed stdin with pcm audio chunks from origin
            async def read_audio():
                try:
                    async for chunk in stream_job.subscribe(player_id):
                        try:
                            await ffmpeg_proc.write(chunk)
                        except BrokenPipeError:
                            break
                finally:
                    ffmpeg_proc.write_eof()

            ffmpeg_proc.attach_task(read_audio())

            # read final chunks from stdout
            iterator = (
                ffmpeg_proc.iter_chunked(icy_meta_interval)
                if enable_icy
                else ffmpeg_proc.iter_chunked(128000)
            )
            async for chunk in iterator:
                try:
                    await resp.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    # race condition
                    break

                if not enable_icy:
                    continue

                # if icy metadata is enabled, send the icy metadata after the chunk
                current_item = self.mass.players.queues.get_item(
                    queue.queue_id, queue.index_in_buffer
                )
                if (
                    current_item
                    and current_item.streamdetails
                    and current_item.streamdetails.stream_title
                ):
                    title = current_item.streamdetails.stream_title
                elif queue and current_item and current_item.name:
                    title = current_item.name
                else:
                    title = "Music Assistant"
                metadata = f"StreamTitle='{title}';".encode()
                while len(metadata) % 16 != 0:
                    metadata += b"\x00"
                length = len(metadata)
                length_b = chr(int(length / 16)).encode()
                await resp.write(length_b + metadata)

        return resp

    async def _get_flow_stream(
        self,
        stream_job: StreamJob,
        seek_position: int = 0,
        fade_in: bool = False,
    ) -> AsyncGenerator[bytes, None]:
        """Get a flow stream of all tracks in the queue."""
        # ruff: noqa: PLR0915
        queue_id = stream_job.queue_item.queue_id
        queue = self.mass.players.queues.get(queue_id)
        queue_player = self.mass.players.get(queue_id)
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
                    ) = await self.mass.players.queues.player_ready_for_next_track(
                        queue_id, queue_track.queue_item_id
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
            buffer_size = crossfade_size if use_crossfade else int(pcm_sample_size * 2)

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
                # only allow strip silence from begin if track is being crossfaded
                strip_silence_begin=last_fadeout_part != b"",
            ):
                chunk_num += 1

                # slow down if the player buffers too aggressively
                seconds_streamed = int(bytes_written / stream_job.pcm_sample_size)
                if (
                    seconds_streamed > 10
                    and queue_player.corrected_elapsed_time > 10
                    and (seconds_streamed - queue_player.corrected_elapsed_time) > 10
                ):
                    await asyncio.sleep(1)

                ####  HANDLE FIRST PART OF TRACK

                # buffer full for crossfade
                if last_fadeout_part and (len(buffer) >= buffer_size):
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

                # enough data in buffer, feed to output
                if len(buffer) >= (buffer_size * 2):
                    yield buffer[:buffer_size]
                    bytes_written += buffer_size
                    buffer = buffer[buffer_size:] + chunk
                    continue

                # all other: fill buffer
                buffer += chunk
                continue

            #### HANDLE END OF TRACK

            if bytes_written == 0:
                # stream error: got empty first chunk ?!
                LOGGER.warning("Stream error on %s", streamdetails.uri)
                queue_track.streamdetails.seconds_streamed = 0
                continue

            if buffer and use_crossfade:
                # if crossfade is enabled, save fadeout part to pickup for next track
                last_fadeout_part = buffer[-crossfade_size:]
                remaining_bytes = buffer[:-crossfade_size]
                yield remaining_bytes
                bytes_written += len(remaining_bytes)
            elif buffer:
                # no crossfade enabled, just yield the buffer last part
                yield buffer
                bytes_written += len(buffer)

            # end of the track reached - store accurate duration
            queue_track.streamdetails.seconds_streamed = bytes_written / pcm_sample_size
            LOGGER.debug(
                "Finished Streaming queue track: %s (%s) on queue %s",
                queue_track.streamdetails.uri,
                queue_track.name,
                queue.display_name,
            )

        LOGGER.info("Finished Queue Flow stream for Queue %s", queue.display_name)

    async def serve_preview(self, request: web.Request):
        """Serve short preview sample."""
        provider_instance_id_or_domain = request.query["provider"]
        item_id = urllib.parse.unquote(request.query["item_id"])
        resp = web.StreamResponse(status=200, reason="OK", headers={"Content-Type": "audio/mp3"})
        await resp.prepare(request)
        async for chunk in get_preview_stream(self.mass, provider_instance_id_or_domain, item_id):
            await resp.write(chunk)
        return resp

    async def _get_player_ffmpeg_args(
        self,
        player: Player,
        input_sample_rate: int,
        input_bit_depth: int,
        output_format: ContentType,
        output_sample_rate: int,
    ) -> list[str]:
        """Get player specific arguments for the given (pcm) input and output details."""
        player_conf = await self.mass.config.get_player_config(player.player_id)
        conf_channels = player_conf.get_value(CONF_OUTPUT_CHANNELS)
        # generic args
        generic_args = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "warning" if LOGGER.isEnabledFor(logging.DEBUG) else "quiet",
            "-ignore_unknown",
        ]
        # input args
        input_args = [
            "-f",
            ContentType.from_bit_depth(input_bit_depth).value,
            "-ac",
            "2",
            "-ar",
            str(input_sample_rate),
            "-i",
            "-",
        ]
        input_args += ["-metadata", 'title="Music Assistant"']
        # select output args
        if output_format == ContentType.FLAC:
            output_args = ["-f", "flac", "-compression_level", "3"]
        elif output_format == ContentType.AAC:
            output_args = ["-f", "adts", "-c:a", output_format.value, "-b:a", "320k"]
        elif output_format == ContentType.MP3:
            output_args = ["-f", "mp3", "-c:a", output_format.value, "-b:a", "320k"]
        else:
            output_args = ["-f", output_format.value]

        output_args += [
            # append channels
            "-ac",
            "1" if conf_channels != "stereo" else "2",
            # append sample rate
            "-ar",
            str(output_sample_rate),
            # output = pipe
            "-",
        ]
        # collect extra and filter args
        # TODO: add convolution/DSP/roomcorrections here!
        extra_args = []
        filter_params = []

        # the below is a very basic 3-band equalizer,
        # this could be a lot more sophisticated at some point
        if eq_bass := player_conf.get_value(CONF_EQ_BASS):
            filter_params.append(f"equalizer=frequency=100:width=200:width_type=h:gain={eq_bass}")
        if eq_mid := player_conf.get_value(CONF_EQ_MID):
            filter_params.append(f"equalizer=frequency=900:width=1800:width_type=h:gain={eq_mid}")
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
