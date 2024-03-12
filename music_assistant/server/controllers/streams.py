"""
Controller to stream audio to players.

The streams controller hosts a basic, unprotected HTTP-only webserver
purely to stream audio packets to players and some control endpoints such as
the upnp callbacks and json rpc api for slimproto clients.
"""

from __future__ import annotations

import asyncio
import logging
import time
import urllib.parse
from collections.abc import AsyncGenerator
from contextlib import suppress
from typing import TYPE_CHECKING

import shortuuid
from aiohttp import web

from music_assistant.common.helpers.util import get_ip, select_free_port
from music_assistant.common.models.config_entries import (
    ConfigEntry,
    ConfigValueOption,
    ConfigValueType,
)
from music_assistant.common.models.enums import ConfigEntryType, ContentType, MediaType
from music_assistant.common.models.errors import MediaNotFoundError, QueueEmpty
from music_assistant.common.models.media_items import AudioFormat
from music_assistant.constants import (
    CONF_BIND_IP,
    CONF_BIND_PORT,
    CONF_CROSSFADE,
    CONF_CROSSFADE_DURATION,
    CONF_EQ_BASS,
    CONF_EQ_MID,
    CONF_EQ_TREBLE,
    CONF_OUTPUT_CHANNELS,
    CONF_PUBLISH_IP,
    SILENCE_FILE,
)
from music_assistant.server.helpers.audio import (
    check_audio_support,
    crossfade_pcm_parts,
    get_media_stream,
    set_stream_details,
)
from music_assistant.server.helpers.process import AsyncProcess
from music_assistant.server.helpers.util import get_ips
from music_assistant.server.helpers.webserver import Webserver
from music_assistant.server.models.core_controller import CoreController

if TYPE_CHECKING:
    from music_assistant.common.models.config_entries import CoreConfig
    from music_assistant.common.models.player import Player
    from music_assistant.common.models.player_queue import PlayerQueue
    from music_assistant.common.models.queue_item import QueueItem


DEFAULT_STREAM_HEADERS = {
    "transferMode.dlna.org": "Streaming",
    "contentFeatures.dlna.org": "DLNA.ORG_OP=00;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=0d500000000000000000000000000000",  # noqa: E501
    "Cache-Control": "no-cache",
    "Connection": "close",
    "Accept-Ranges": "none",
    "icy-name": "Music Assistant",
    "icy-pub": "0",
}
FLOW_MAX_SAMPLE_RATE = 192000
FLOW_MAX_BIT_DEPTH = 24

# pylint:disable=too-many-locals


class MultiClientStreamJob:
    """Representation of a (multiclient) Audio Queue stream job/task.

    The whole idea here is that in case of a player (sync)group,
    all client players receive the exact same (PCM) audio chunks from the source audio.
    A StreamJob is tied to a Queue and streams the queue flow stream,
    In case a stream is restarted (e.g. when seeking), a new MultiClientStreamJob will be created.
    """

    _audio_task: asyncio.Task | None = None

    def __init__(
        self,
        stream_controller: StreamsController,
        queue_id: str,
        pcm_format: AudioFormat,
        start_queue_item: QueueItem,
        seek_position: int = 0,
        fade_in: bool = False,
    ) -> None:
        """Initialize MultiClientStreamJob instance."""
        self.stream_controller = stream_controller
        self.queue_id = queue_id
        self.queue = self.stream_controller.mass.player_queues.get(queue_id)
        assert self.queue  # just in case
        self.pcm_format = pcm_format
        self.start_queue_item = start_queue_item
        self.seek_position = seek_position
        self.fade_in = fade_in
        self.job_id = shortuuid.uuid()
        self.expected_players: set[str] = set()
        self.subscribed_players: dict[str, asyncio.Queue[bytes]] = {}
        self.bytes_streamed: int = 0
        self._all_clients_connected = asyncio.Event()
        self.logger = stream_controller.logger.getChild(f"streamjob_{self.job_id}")
        self._finished: bool = False
        self.workaround_players_seen: set[str] = set()
        # start running the audio task in the background
        self._audio_task = asyncio.create_task(self._stream_job_runner())

    @property
    def finished(self) -> bool:
        """Return if this StreamJob is finished."""
        return self._finished or self._audio_task and self._audio_task.done()

    @property
    def pending(self) -> bool:
        """Return if this Job is pending start."""
        return not self.finished and not self._all_clients_connected.is_set()

    @property
    def running(self) -> bool:
        """Return if this Job is running."""
        return not self.finished and not self.pending

    def stop(self) -> None:
        """Stop running this job."""
        self._finished = True
        if self._audio_task and self._audio_task.done():
            return
        if self._audio_task:
            self._audio_task.cancel()
        for sub_queue in self.subscribed_players.values():
            with suppress(asyncio.QueueFull):
                sub_queue.put_nowait(b"EOF")

    def resolve_stream_url(self, child_player_id: str, output_codec: ContentType) -> str:
        """Resolve the childplayer specific stream URL to this streamjob."""
        fmt = output_codec.value
        # handle raw pcm
        if output_codec.is_pcm():
            player = self.stream_controller.mass.players.get(child_player_id)
            player_max_bit_depth = 24 if player.supports_24bit else 16
            output_sample_rate = min(self.pcm_format.sample_rate, player.max_sample_rate)
            output_bit_depth = min(self.pcm_format.bit_depth, player_max_bit_depth)
            output_channels = self.stream_controller.mass.config.get_raw_player_config_value(
                child_player_id, CONF_OUTPUT_CHANNELS, "stereo"
            )
            channels = 1 if output_channels != "stereo" else 2
            fmt += (
                f";codec=pcm;rate={output_sample_rate};"
                f"bitrate={output_bit_depth};channels={channels}"
            )
        url = f"{self.stream_controller._server.base_url}/{self.queue_id}/multi/{self.job_id}/{child_player_id}/{self.start_queue_item.queue_item_id}.{fmt}"  # noqa: E501
        self.expected_players.add(child_player_id)
        return url

    async def subscribe(self, player_id: str) -> AsyncGenerator[bytes, None]:
        """Subscribe consumer and iterate incoming chunks on the queue."""
        if (
            player_id in self.stream_controller.workaround_players
            and player_id not in self.workaround_players_seen
        ):
            self.workaround_players_seen.add(player_id)
            yield b"EOF"
            return

        try:
            self.subscribed_players[player_id] = sub_queue = asyncio.Queue(2)

            if self._all_clients_connected.is_set():
                # client subscribes while we're already started - we dont support that (for now?)
                msg = f"Client {player_id} is joining while the stream is already started"
                raise RuntimeError(msg)
            self.logger.debug("Subscribed client %s", player_id)

            if len(self.subscribed_players) == len(self.expected_players):
                # we reached the number of expected subscribers, set event
                # so that chunks can be pushed
                self._all_clients_connected.set()

            # keep reading audio chunks from the queue until we receive an EOF chunk
            while True:
                chunk = await sub_queue.get()
                if chunk == b"EOF":
                    # EOF chunk received
                    break
                yield chunk
        finally:
            self.subscribed_players.pop(player_id, None)
            self.logger.debug("Unsubscribed client %s", player_id)
            # check if this was the last subscriber and we should cancel
            await asyncio.sleep(2)
            if len(self.subscribed_players) == 0 and self._audio_task and not self.finished:
                self.logger.debug("Cleaning up, all clients disappeared...")
                self._audio_task.cancel()

    async def _put_chunk(self, chunk: bytes) -> None:
        """Put chunk of data to all subscribers."""
        async with asyncio.TaskGroup() as tg:
            for sub_queue in list(self.subscribed_players.values()):
                # put this chunk on the player's subqueue
                tg.create_task(sub_queue.put(chunk))
        self.bytes_streamed += len(chunk)

    async def _stream_job_runner(self) -> None:
        """Feed audio chunks to StreamJob subscribers."""
        chunk_num = 0
        async for chunk in self.stream_controller.get_flow_stream(
            self.queue,
            self.start_queue_item,
            self.pcm_format,
            self.seek_position,
            self.fade_in,
        ):
            chunk_num += 1
            if chunk_num == 1:
                # wait until all expected clients are connected
                try:
                    async with asyncio.timeout(10):
                        await self._all_clients_connected.wait()
                except TimeoutError:
                    if len(self.subscribed_players) == 0:
                        self.stream_controller.logger.exception(
                            "Abort multi client stream job for queue %s: "
                            "clients did not connect within timeout",
                            self.queue.display_name,
                        )
                        break
                    # not all clients connected but timeout expired, set flag and move on
                    # with all clients that did connect
                    self._all_clients_connected.set()
                else:
                    self.stream_controller.logger.debug(
                        "Starting multi client stream job for queue %s "
                        "with %s out of %s connected clients",
                        self.queue.display_name,
                        len(self.subscribed_players),
                        len(self.expected_players),
                    )

            await self._put_chunk(chunk)

        # mark EOF with EOF chunk
        await self._put_chunk(b"EOF")


def parse_pcm_info(content_type: str) -> tuple[int, int, int]:
    """Parse PCM info from a codec/content_type string."""
    params = (
        dict(urllib.parse.parse_qsl(content_type.replace(";", "&"))) if ";" in content_type else {}
    )
    sample_rate = int(params.get("rate", 44100))
    sample_size = int(params.get("bitrate", 16))
    channels = int(params.get("channels", 2))
    return (sample_rate, sample_size, channels)


class StreamsController(CoreController):
    """Webserver Controller to stream audio to players."""

    domain: str = "streams"

    def __init__(self, *args, **kwargs) -> None:
        """Initialize instance."""
        super().__init__(*args, **kwargs)
        self._server = Webserver(self.logger, enable_dynamic_routes=True)
        self.multi_client_jobs: dict[str, MultiClientStreamJob] = {}
        self.register_dynamic_route = self._server.register_dynamic_route
        self.unregister_dynamic_route = self._server.unregister_dynamic_route
        self.manifest.name = "Streamserver"
        self.manifest.description = (
            "Music Assistant's core server that is responsible for "
            "streaming audio to players on the local network as well as "
            "some player specific local control callbacks."
        )
        self.manifest.icon = "cast-audio"
        self.workaround_players: set[str] = set()

    @property
    def base_url(self) -> str:
        """Return the base_url for the streamserver."""
        return self._server.base_url

    async def get_config_entries(
        self,
        action: str | None = None,
        values: dict[str, ConfigValueType] | None = None,
    ) -> tuple[ConfigEntry, ...]:
        """Return all Config Entries for this core module (if any)."""
        default_ip = await get_ip()
        all_ips = await get_ips()
        default_port = await select_free_port(8096, 9200)
        return (
            ConfigEntry(
                key=CONF_BIND_PORT,
                type=ConfigEntryType.INTEGER,
                default_value=default_port,
                label="TCP Port",
                description="The TCP port to run the server. "
                "Make sure that this server can be reached "
                "on the given IP and TCP port by players on the local network.",
            ),
            ConfigEntry(
                key=CONF_PUBLISH_IP,
                type=ConfigEntryType.STRING,
                default_value=default_ip,
                label="Published IP address",
                description="This IP address is communicated to players where to find this server. "
                "Override the default in advanced scenarios, such as multi NIC configurations. \n"
                "Make sure that this server can be reached "
                "on the given IP and TCP port by players on the local network. \n"
                "This is an advanced setting that should normally "
                "not be adjusted in regular setups.",
                advanced=True,
            ),
            ConfigEntry(
                key=CONF_BIND_IP,
                type=ConfigEntryType.STRING,
                default_value="0.0.0.0",
                options=(ConfigValueOption(x, x) for x in {"0.0.0.0", *all_ips}),
                label="Bind to IP/interface",
                description="Start the stream server on this specific interface. \n"
                "Use 0.0.0.0 to bind to all interfaces, which is the default. \n"
                "This is an advanced setting that should normally "
                "not be adjusted in regular setups.",
                advanced=True,
            ),
        )

    async def setup(self, config: CoreConfig) -> None:
        """Async initialize of module."""
        ffmpeg_present, libsoxr_support, version = await check_audio_support()
        if not ffmpeg_present:
            self.logger.error("FFmpeg binary not found on your system, playback will NOT work!.")
        elif not libsoxr_support:
            self.logger.warning(
                "FFmpeg version found without libsoxr support, "
                "highest quality audio not available. "
            )
        self.logger.info(
            "Detected ffmpeg version %s %s",
            version,
            "with libsoxr support" if libsoxr_support else "",
        )
        # start the webserver
        self.publish_port = config.get_value(CONF_BIND_PORT)
        self.publish_ip = config.get_value(CONF_PUBLISH_IP)
        await self._server.setup(
            bind_ip=config.get_value(CONF_BIND_IP),
            bind_port=self.publish_port,
            base_url=f"http://{self.publish_ip}:{self.publish_port}",
            static_routes=[
                (
                    "*",
                    "/{queue_id}/multi/{job_id}/{player_id}/{queue_item_id}.{fmt}",
                    self.serve_multi_subscriber_stream,
                ),
                (
                    "*",
                    "/{queue_id}/flow/{queue_item_id}.{fmt}",
                    self.serve_queue_flow_stream,
                ),
                (
                    "*",
                    "/{queue_id}/single/{queue_item_id}.{fmt}",
                    self.serve_queue_item_stream,
                ),
                (
                    "*",
                    "/{queue_id}/command/{command}.mp3",
                    self.serve_command_request,
                ),
            ],
        )

    async def close(self) -> None:
        """Cleanup on exit."""
        await self._server.close()

    async def resolve_stream_url(
        self,
        queue_item: QueueItem,
        output_codec: ContentType,
        seek_position: int = 0,
        fade_in: bool = False,
        flow_mode: bool = False,
    ) -> str:
        """Resolve the stream URL for the given QueueItem."""
        fmt = output_codec.value
        # handle raw pcm without exact format specifiers
        if output_codec.is_pcm() and ";" not in fmt:
            fmt += f";codec=pcm;rate={44100};bitrate={16};channels={2}"
        query_params = {}
        base_path = "flow" if flow_mode else "single"
        url = f"{self._server.base_url}/{queue_item.queue_id}/{base_path}/{queue_item.queue_item_id}.{fmt}"  # noqa: E501
        if seek_position:
            query_params["seek_position"] = str(seek_position)
        if fade_in:
            query_params["fade_in"] = "1"
        # we add a timestamp as basic checksum
        # most importantly this is to invalidate any caches
        # but also to handle edge cases such as single track repeat
        query_params["ts"] = str(int(time.time()))
        url += "?" + urllib.parse.urlencode(query_params)
        return url

    async def create_multi_client_stream_job(
        self,
        queue_id: str,
        start_queue_item: QueueItem,
        seek_position: int = 0,
        fade_in: bool = False,
        pcm_bit_depth: int = 24,
        pcm_sample_rate: int = 48000,
    ) -> MultiClientStreamJob:
        """Create a MultiClientStreamJob for the given queue..

        This is called by player/sync group implementations to start streaming
        the queue audio to multiple players at once.
        """
        if existing_job := self.multi_client_jobs.pop(queue_id, None):
            # cleanup existing job first
            if not existing_job.finished:
                self.logger.warning("Detected existing (running) stream job for queue %s", queue_id)
                existing_job.stop()
        self.multi_client_jobs[queue_id] = stream_job = MultiClientStreamJob(
            self,
            queue_id=queue_id,
            pcm_format=AudioFormat(
                content_type=ContentType.from_bit_depth(pcm_bit_depth),
                sample_rate=pcm_sample_rate,
                bit_depth=pcm_bit_depth,
                channels=2,
            ),
            start_queue_item=start_queue_item,
            seek_position=seek_position,
            fade_in=fade_in,
        )
        return stream_job

    async def serve_queue_item_stream(self, request: web.Request) -> web.Response:
        """Stream single queueitem audio to a player."""
        self._log_request(request)
        queue_id = request.match_info["queue_id"]
        queue = self.mass.player_queues.get(queue_id)
        if not queue:
            raise web.HTTPNotFound(reason=f"Unknown Queue: {queue_id}")
        queue_player = self.mass.players.get(queue_id)
        queue_item_id = request.match_info["queue_item_id"]
        queue_item = self.mass.player_queues.get_item(queue_id, queue_item_id)
        if not queue_item:
            raise web.HTTPNotFound(reason=f"Unknown Queue item: {queue_item_id}")
        try:
            await set_stream_details(self.mass, queue_item=queue_item)
        except MediaNotFoundError:
            raise web.HTTPNotFound(
                reason=f"Unable to retrieve streamdetails for item: {queue_item}"
            )
        seek_position = int(request.query.get("seek_position", 0))
        queue_item.streamdetails.seconds_skipped = seek_position
        fade_in = bool(request.query.get("fade_in", 0))
        # work out output format/details
        output_format = await self._get_output_format(
            output_format_str=request.match_info["fmt"],
            queue_player=queue_player,
            default_sample_rate=queue_item.streamdetails.audio_format.sample_rate,
            default_bit_depth=queue_item.streamdetails.audio_format.bit_depth,
        )

        # prepare request, add some DLNA/UPNP compatible headers
        headers = {
            **DEFAULT_STREAM_HEADERS,
            "Content-Type": f"audio/{output_format.output_format_str}",
        }
        resp = web.StreamResponse(
            status=200,
            reason="OK",
            headers=headers,
        )
        await resp.prepare(request)

        # return early if this is not a GET request
        if request.method != "GET":
            return resp

        # all checks passed, start streaming!
        self.logger.debug(
            "Start serving audio stream for QueueItem %s to %s",
            queue_item.uri,
            queue.display_name,
        )
        queue.index_in_buffer = self.mass.player_queues.index_by_id(queue_id, queue_item_id)
        # collect player specific ffmpeg args to re-encode the source PCM stream
        pcm_format = AudioFormat(
            content_type=ContentType.from_bit_depth(
                queue_item.streamdetails.audio_format.bit_depth
            ),
            sample_rate=queue_item.streamdetails.audio_format.sample_rate,
            bit_depth=queue_item.streamdetails.audio_format.bit_depth,
        )
        ffmpeg_args = await self._get_player_ffmpeg_args(
            queue_player,
            input_format=pcm_format,
            output_format=output_format,
        )

        async with AsyncProcess(ffmpeg_args, True) as ffmpeg_proc:
            # feed stdin with pcm audio chunks from origin
            async def read_audio() -> None:
                try:
                    async for _, chunk in get_media_stream(
                        self.mass,
                        streamdetails=queue_item.streamdetails,
                        pcm_format=pcm_format,
                        seek_position=seek_position,
                        fade_in=fade_in,
                    ):
                        try:
                            await ffmpeg_proc.write(chunk)
                        except BrokenPipeError:
                            break
                finally:
                    ffmpeg_proc.write_eof()

            ffmpeg_proc.attach_task(read_audio())

            # read final chunks from stdout
            async for chunk in ffmpeg_proc.iter_any(768000):
                try:
                    await resp.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    # race condition
                    break

        return resp

    async def serve_queue_flow_stream(self, request: web.Request) -> web.Response:
        """Stream Queue Flow audio to player."""
        self._log_request(request)
        queue_id = request.match_info["queue_id"]
        queue = self.mass.player_queues.get(queue_id)
        if not queue:
            raise web.HTTPNotFound(reason=f"Unknown Queue: {queue_id}")
        start_queue_item_id = request.match_info["queue_item_id"]
        start_queue_item = self.mass.player_queues.get_item(queue_id, start_queue_item_id)
        if not start_queue_item:
            raise web.HTTPNotFound(reason=f"Unknown Queue item: {start_queue_item_id}")
        seek_position = int(request.query.get("seek_position", 0))
        fade_in = bool(request.query.get("fade_in", 0))
        queue_player = self.mass.players.get(queue_id)
        # work out output format/details
        output_format = await self._get_output_format(
            output_format_str=request.match_info["fmt"],
            queue_player=queue_player,
            default_sample_rate=FLOW_MAX_SAMPLE_RATE,
            default_bit_depth=FLOW_MAX_BIT_DEPTH,
        )
        # prepare request, add some DLNA/UPNP compatible headers
        enable_icy = request.headers.get("Icy-MetaData", "") == "1"
        icy_meta_interval = 16384 * 4 if output_format.content_type.is_lossless() else 16384
        headers = {
            **DEFAULT_STREAM_HEADERS,
            "Content-Type": f"audio/{output_format.output_format_str}",
        }
        if enable_icy:
            headers["icy-metaint"] = str(icy_meta_interval)

        resp = web.StreamResponse(
            status=200,
            reason="OK",
            headers=headers,
        )
        await resp.prepare(request)

        # return early if this is not a GET request
        if request.method != "GET":
            return resp

        # all checks passed, start streaming!
        self.logger.debug("Start serving Queue flow audio stream for %s", queue_player.name)

        # collect player specific ffmpeg args to re-encode the source PCM stream
        pcm_format = AudioFormat(
            content_type=ContentType.from_bit_depth(output_format.bit_depth),
            sample_rate=output_format.sample_rate,
            bit_depth=output_format.bit_depth,
            channels=2,
        )
        ffmpeg_args = await self._get_player_ffmpeg_args(
            queue_player,
            input_format=pcm_format,
            output_format=output_format,
        )

        async with AsyncProcess(ffmpeg_args, True) as ffmpeg_proc:
            # feed stdin with pcm audio chunks from origin
            async def read_audio() -> None:
                try:
                    async for chunk in self.get_flow_stream(
                        queue=queue,
                        start_queue_item=start_queue_item,
                        pcm_format=pcm_format,
                        seek_position=seek_position,
                        fade_in=fade_in,
                    ):
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
                else ffmpeg_proc.iter_any(768000)
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
                if (
                    # use current item here and not buffered item, otherwise
                    # the icy metadata will be too much ahead
                    (current_item := queue.current_item)
                    and current_item.streamdetails
                    and current_item.streamdetails.stream_title
                ):
                    title = current_item.streamdetails.stream_title
                elif queue and current_item and current_item.name:
                    title = current_item.name
                else:
                    title = "Music Assistant"
                metadata = f"StreamTitle='{title}';".encode()
                if current_item and current_item.image:
                    metadata += f"StreamURL='{current_item.image.path}'".encode()
                while len(metadata) % 16 != 0:
                    metadata += b"\x00"
                length = len(metadata)
                length_b = chr(int(length / 16)).encode()
                await resp.write(length_b + metadata)

        return resp

    async def serve_multi_subscriber_stream(self, request: web.Request) -> web.Response:
        """Stream Queue Flow audio to a child player within a multi subscriber setup."""
        self._log_request(request)
        queue_id = request.match_info["queue_id"]
        streamjob = self.multi_client_jobs.get(queue_id)
        if not streamjob:
            raise web.HTTPNotFound(reason=f"Unknown StreamJob for queue: {queue_id}")
        job_id = request.match_info["job_id"]
        if job_id != streamjob.job_id:
            raise web.HTTPNotFound(reason=f"StreamJob ID {job_id} mismatch for queue: {queue_id}")
        child_player_id = request.match_info["player_id"]
        child_player = self.mass.players.get(child_player_id)
        if not child_player:
            raise web.HTTPNotFound(reason=f"Unknown player: {child_player_id}")
        # work out (childplayer specific!) output format/details
        output_format = await self._get_output_format(
            output_format_str=request.match_info["fmt"],
            queue_player=child_player,
            default_sample_rate=streamjob.pcm_format.sample_rate,
            default_bit_depth=streamjob.pcm_format.bit_depth,
        )
        # prepare request, add some DLNA/UPNP compatible headers
        headers = {
            **DEFAULT_STREAM_HEADERS,
            "Content-Type": f"audio/{output_format.output_format_str}",
        }
        resp = web.StreamResponse(
            status=200,
            reason="OK",
            headers=headers,
        )
        await resp.prepare(request)

        # return early if this is not a GET request
        if request.method != "GET":
            return resp

        # some players (e.g. dlna, sonos) misbehave and do multiple GET requests
        # to the stream in an attempt to get the audio details such as duration
        # which is a bit pointless for our duration-less queue stream
        # and it completely messes with the subscription logic
        if child_player_id in streamjob.subscribed_players:
            self.logger.warning(
                "Player %s is making multiple requests "
                "to the same stream, playback may be disturbed!",
                child_player_id,
            )
            self.workaround_players.add(child_player_id)

        # all checks passed, start streaming!
        self.logger.debug(
            "Start serving multi-subscriber Queue flow audio stream for queue %s to player %s",
            streamjob.queue.display_name,
            child_player.display_name,
        )

        # collect player specific ffmpeg args to re-encode the source PCM stream
        ffmpeg_args = await self._get_player_ffmpeg_args(
            child_player,
            input_format=streamjob.pcm_format,
            output_format=output_format,
        )

        async with AsyncProcess(ffmpeg_args, True) as ffmpeg_proc:
            # feed stdin with pcm audio chunks from origin
            async def read_audio() -> None:
                try:
                    async for chunk in streamjob.subscribe(child_player_id):
                        try:
                            await ffmpeg_proc.write(chunk)
                        except BrokenPipeError:
                            break
                finally:
                    ffmpeg_proc.write_eof()

            ffmpeg_proc.attach_task(read_audio())

            # read final chunks from stdout
            async for chunk in ffmpeg_proc.iter_any(768000):
                try:
                    await resp.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    # race condition
                    break

        return resp

    async def serve_command_request(self, request: web.Request) -> web.Response:
        """Handle special 'command' request for a player."""
        self._log_request(request)
        queue_id = request.match_info["queue_id"]
        command = request.match_info["command"]
        if command == "next":
            self.mass.create_task(self.mass.player_queues.next(queue_id))
        return web.FileResponse(SILENCE_FILE)

    def get_command_url(self, player_or_queue_id: str, command: str) -> str:
        """Get the url for the special command stream."""
        return f"{self.base_url}/{player_or_queue_id}/command/{command}.mp3"

    async def get_flow_stream(
        self,
        queue: PlayerQueue,
        start_queue_item: QueueItem,
        pcm_format: AudioFormat,
        seek_position: int = 0,
        fade_in: bool = False,
    ) -> AsyncGenerator[bytes, None]:
        """Get a flow stream of all tracks in the queue as raw PCM audio."""
        # ruff: noqa: PLR0915
        assert pcm_format.content_type.is_pcm()
        queue_track = None
        last_fadeout_part = b""
        total_bytes_written = 0
        queue.flow_mode = True
        use_crossfade = self.mass.config.get_raw_player_config_value(
            queue.queue_id, CONF_CROSSFADE, False
        )
        if start_queue_item.media_type != MediaType.TRACK:
            use_crossfade = False
        pcm_sample_size = int(pcm_format.sample_rate * (pcm_format.bit_depth / 8) * 2)
        self.logger.info(
            "Start Queue Flow stream for Queue %s - crossfade: %s",
            queue.display_name,
            use_crossfade,
        )

        while True:
            # get (next) queue item to stream
            if queue_track is None:
                queue_track = start_queue_item
                await set_stream_details(self.mass, queue_track)
            else:
                seek_position = 0
                fade_in = False
                try:
                    queue_track = await self.mass.player_queues.preload_next_item(queue.queue_id)
                except QueueEmpty:
                    break

            self.logger.debug(
                "Start Streaming queue track: %s (%s) for queue %s",
                queue_track.streamdetails.uri,
                queue_track.name,
                queue.display_name,
            )
            queue.index_in_buffer = self.mass.player_queues.index_by_id(
                queue.queue_id, queue_track.queue_item_id
            )

            # set some basic vars
            pcm_sample_size = int(pcm_format.sample_rate * (pcm_format.bit_depth / 8) * 2)
            crossfade_duration = self.mass.config.get_raw_player_config_value(
                queue.queue_id, CONF_CROSSFADE_DURATION, 8
            )
            crossfade_size = int(pcm_sample_size * crossfade_duration)
            queue_track.streamdetails.seconds_skipped = seek_position
            buffer_size = int(pcm_sample_size * 2)  # 2 seconds
            if use_crossfade:
                buffer_size += crossfade_size
            bytes_written = 0
            buffer = b""
            # handle incoming audio chunks
            async for is_last_chunk, chunk in get_media_stream(
                self.mass,
                queue_track.streamdetails,
                pcm_format=pcm_format,
                seek_position=seek_position,
                fade_in=fade_in,
                # strip silence from begin/end if track is being crossfaded
                strip_silence_begin=use_crossfade,
                strip_silence_end=use_crossfade,
            ):
                # throttle buffer, do not allow more than 30 seconds in player's own buffer
                seconds_buffered = (total_bytes_written + bytes_written) / pcm_sample_size
                player = self.mass.players.get(queue.queue_id)
                if seconds_buffered > 60 and player.corrected_elapsed_time > 30:
                    while (seconds_buffered - player.corrected_elapsed_time) > 30:
                        await asyncio.sleep(1)

                # ALWAYS APPEND CHUNK TO BUFFER
                buffer += chunk
                if not is_last_chunk and len(buffer) < buffer_size:
                    # buffer is not full enough, move on
                    continue

                ####  HANDLE CROSSFADE OF PREVIOUS TRACK AND NEW TRACK
                if not is_last_chunk and last_fadeout_part:
                    # perform crossfade
                    fadein_part = buffer[:crossfade_size]
                    remaining_bytes = buffer[crossfade_size:]
                    crossfade_part = await crossfade_pcm_parts(
                        fadein_part,
                        last_fadeout_part,
                        pcm_format.bit_depth,
                        pcm_format.sample_rate,
                    )
                    # send crossfade_part
                    yield crossfade_part
                    bytes_written += len(crossfade_part)
                    # also write the leftover bytes from the crossfade action
                    if remaining_bytes:
                        yield remaining_bytes
                        bytes_written += len(remaining_bytes)
                        del remaining_bytes
                    # clear vars
                    last_fadeout_part = b""
                    buffer = b""

                #### HANDLE END OF TRACK
                elif is_last_chunk:
                    if use_crossfade:
                        # if crossfade is enabled, save fadeout part to pickup for next track
                        last_fadeout_part = buffer[-crossfade_size:]
                        remaining_bytes = buffer[:-crossfade_size]
                        yield remaining_bytes
                        bytes_written += len(remaining_bytes)
                        del remaining_bytes
                    else:
                        # no crossfade enabled, just yield the (entire) buffer last part
                        yield buffer
                        bytes_written += len(buffer)
                    # clear vars
                    buffer = b""

                #### OTHER: enough data in buffer, feed to output
                else:
                    chunk_size = len(chunk)
                    yield buffer[:chunk_size]
                    bytes_written += chunk_size
                    buffer = buffer[chunk_size:]

            # update duration details based on the actual pcm data we sent
            # this also accounts for crossfade and silence stripping
            queue_track.streamdetails.seconds_streamed = bytes_written / pcm_sample_size
            queue_track.streamdetails.duration = (
                seek_position + queue_track.streamdetails.seconds_streamed
            )
            total_bytes_written += bytes_written
            self.logger.debug(
                "Finished Streaming queue track: %s (%s) on queue %s - seconds streamed: %s",
                queue_track.streamdetails.uri,
                queue_track.name,
                queue.display_name,
                queue_track.streamdetails.seconds_streamed,
            )
        # end of queue flow: make sure we yield the last_fadeout_part
        if last_fadeout_part:
            yield last_fadeout_part
        self.logger.info("Finished Queue Flow stream for Queue %s", queue.display_name)

    async def _get_player_ffmpeg_args(
        self,
        player: Player,
        input_format: AudioFormat,
        output_format: AudioFormat,
    ) -> list[str]:
        """Get player specific arguments for the given (pcm) input and output details."""
        # generic args
        generic_args = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "warning" if self.logger.isEnabledFor(logging.DEBUG) else "quiet",
            "-ignore_unknown",
        ]
        # input args
        input_args = [
            "-f",
            input_format.content_type.value,
            "-ac",
            str(input_format.channels),
            "-channel_layout",
            "mono" if input_format.channels == 1 else "stereo",
            "-ar",
            str(input_format.sample_rate),
            "-i",
            "-",
        ]
        # select output args
        if output_format.content_type == ContentType.FLAC:
            # set compression level to 0 to prevent issues with cast players
            output_args = ["-f", "flac", "-compression_level", "0"]
        elif output_format.content_type == ContentType.AAC:
            output_args = ["-f", "adts", "-c:a", "aac", "-b:a", "320k"]
        elif output_format.content_type == ContentType.MP3:
            output_args = ["-f", "mp3", "-c:a", "mp3", "-b:a", "320k"]
        else:
            output_args = ["-f", output_format.content_type.value]

        # append channels
        output_args += ["-ac", str(output_format.channels)]
        # append sample rate (if codec is lossless)
        if output_format.content_type.is_lossless():
            output_args += ["-ar", str(output_format.sample_rate)]
        # append output = pipe
        output_args += ["-"]

        # collect extra and filter args
        # TODO: add convolution/DSP/roomcorrections here!
        extra_args = []
        filter_params = []

        # the below is a very basic 3-band equalizer,
        # this could be a lot more sophisticated at some point
        if (
            eq_bass := self.mass.config.get_raw_player_config_value(
                player.player_id, CONF_EQ_BASS, 0
            )
        ) != 0:
            filter_params.append(f"equalizer=frequency=100:width=200:width_type=h:gain={eq_bass}")
        if (
            eq_mid := self.mass.config.get_raw_player_config_value(player.player_id, CONF_EQ_MID, 0)
        ) != 0:
            filter_params.append(f"equalizer=frequency=900:width=1800:width_type=h:gain={eq_mid}")
        if (
            eq_treble := self.mass.config.get_raw_player_config_value(
                player.player_id, CONF_EQ_TREBLE, 0
            )
        ) != 0:
            filter_params.append(
                f"equalizer=frequency=9000:width=18000:width_type=h:gain={eq_treble}"
            )
        # handle output mixing only left or right
        conf_channels = self.mass.config.get_raw_player_config_value(
            player.player_id, CONF_OUTPUT_CHANNELS, "stereo"
        )
        if conf_channels == "left":
            filter_params.append("pan=mono|c0=FL")
        elif conf_channels == "right":
            filter_params.append("pan=mono|c0=FR")

        if filter_params:
            extra_args += ["-af", ",".join(filter_params)]

        return generic_args + input_args + extra_args + output_args

    def _log_request(self, request: web.Request) -> None:
        """Log request."""
        if not self.logger.isEnabledFor(logging.DEBUG):
            return
        self.logger.debug(
            "Got %s request to %s from %s\nheaders: %s\n",
            request.method,
            request.path,
            request.remote,
            request.headers,
        )

    async def _get_output_format(
        self,
        output_format_str: str,
        queue_player: Player,
        default_sample_rate: int,
        default_bit_depth: int,
    ) -> AudioFormat:
        """Parse (player specific) output format details for given format string."""
        content_type = ContentType.try_parse(output_format_str)
        if content_type.is_pcm() or content_type == ContentType.WAV:
            # parse pcm details from format string
            output_sample_rate, output_bit_depth, output_channels = parse_pcm_info(
                output_format_str
            )
            if content_type == ContentType.PCM:
                # resolve generic pcm type
                content_type = ContentType.from_bit_depth(output_bit_depth)

        else:
            output_sample_rate = min(default_sample_rate, queue_player.max_sample_rate)
            player_max_bit_depth = 24 if queue_player.supports_24bit else 16
            output_bit_depth = min(default_bit_depth, player_max_bit_depth)
            output_channels_str = self.mass.config.get_raw_player_config_value(
                queue_player.player_id, CONF_OUTPUT_CHANNELS, "stereo"
            )
            output_channels = 1 if output_channels_str != "stereo" else 2
        return AudioFormat(
            content_type=content_type,
            sample_rate=output_sample_rate,
            bit_depth=output_bit_depth,
            channels=output_channels,
            output_format_str=output_format_str,
        )
