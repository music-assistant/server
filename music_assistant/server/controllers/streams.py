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

from music_assistant.common.helpers.util import (
    empty_queue,
    get_ip,
    select_free_port,
    try_parse_bool,
)
from music_assistant.common.models.config_entries import (
    ConfigEntry,
    ConfigValueOption,
    ConfigValueType,
)
from music_assistant.common.models.enums import ConfigEntryType, ContentType, MediaType
from music_assistant.common.models.errors import QueueEmpty
from music_assistant.common.models.media_items import AudioFormat
from music_assistant.constants import (
    ANNOUNCE_ALERT_FILE,
    CONF_BIND_IP,
    CONF_BIND_PORT,
    CONF_CROSSFADE,
    CONF_CROSSFADE_DURATION,
    CONF_OUTPUT_CHANNELS,
    CONF_PUBLISH_IP,
    SILENCE_FILE,
    UGP_PREFIX,
    VERBOSE_LOG_LEVEL,
)
from music_assistant.server.helpers.audio import LOGGER as AUDIO_LOGGER
from music_assistant.server.helpers.audio import (
    check_audio_support,
    crossfade_pcm_parts,
    get_ffmpeg_args,
    get_ffmpeg_stream,
    get_media_stream,
    get_player_filter_params,
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
    from music_assistant.server import MusicAssistant


DEFAULT_STREAM_HEADERS = {
    "transferMode.dlna.org": "Streaming",
    "contentFeatures.dlna.org": "DLNA.ORG_OP=00;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=0d500000000000000000000000000000",  # noqa: E501
    "Cache-Control": "no-cache,must-revalidate",
    "Pragma": "no-cache",
    "Connection": "close",
    "Accept-Ranges": "none",
    "Icy-Name": "Music Assistant",
    "Icy-Url": "https://music-assistant.io",
}
FLOW_DEFAULT_SAMPLE_RATE = 48000
FLOW_DEFAULT_BIT_DEPTH = 24

# pylint:disable=too-many-locals


class QueueStreamJob:
    """
    Representation of a (multiclient) Audio stream job/task.

    The whole idea here is that the (pcm) audio source can be sent to multiple
    players at once. For example for (slimproto/airplay) syncgroups and universal group.

    All client players receive the exact same PCM audio chunks from the source audio,
    which then can be optionally encoded and/or resampled to the player's preferences.
    In case a stream is restarted (e.g. when seeking),
    a new QueueStreamJob will be created.
    """

    _audio_task: asyncio.Task | None = None

    def __init__(
        self,
        mass: MusicAssistant,
        pcm_audio_source: AsyncGenerator[bytes, None],
        pcm_format: AudioFormat,
        auto_start: bool = False,
    ) -> None:
        """Initialize QueueStreamJob instance."""
        self.mass = mass
        self.pcm_audio_source = pcm_audio_source
        self.pcm_format = pcm_format
        self.expected_players: set[str] = set()
        self.job_id = shortuuid.uuid()
        self.bytes_streamed: int = 0
        self.logger = self.mass.streams.logger
        self.subscribed_players: dict[str, asyncio.Queue[bytes]] = {}
        self._finished = False
        self.allow_start = auto_start
        self._all_clients_connected = asyncio.Event()
        self._audio_task = asyncio.create_task(self._stream_job_runner())

    @property
    def finished(self) -> bool:
        """Return if this StreamJob is finished."""
        return self._finished or (self._audio_task and self._audio_task.done())

    @property
    def pending(self) -> bool:
        """Return if this Job is pending start."""
        return not self.finished and not self._all_clients_connected.is_set()

    @property
    def running(self) -> bool:
        """Return if this Job is running."""
        return (
            self._all_clients_connected.is_set()
            and self._audio_task
            and not self._audio_task.done()
        )

    def start(self) -> None:
        """Start running (send audio chunks to connected players)."""
        if self.finished:
            raise RuntimeError("Task is already finished")
        self.allow_start = True
        if self.expected_players and len(self.subscribed_players) >= len(self.expected_players):
            self._all_clients_connected.set()

    def stop(self) -> None:
        """Stop running this job."""
        if self._audio_task and not self._audio_task.done():
            self._audio_task.cancel()
        if not self._finished:
            # we need to make sure that we close the async generator
            with suppress(StopAsyncIteration):
                task = asyncio.create_task(self.pcm_audio_source.__anext__())
                task.cancel()
        self._finished = True
        for sub_queue in self.subscribed_players.values():
            empty_queue(sub_queue)

    def resolve_stream_url(self, player_id: str, output_codec: ContentType) -> str:
        """Resolve the childplayer specific stream URL to this streamjob."""
        fmt = output_codec.value
        # handle raw pcm
        if output_codec.is_pcm():
            player = self.mass.streams.mass.players.get(player_id)
            player_max_bit_depth = 24 if player.supports_24bit else 16
            output_sample_rate = min(self.pcm_format.sample_rate, player.max_sample_rate)
            output_bit_depth = min(self.pcm_format.bit_depth, player_max_bit_depth)
            output_channels = self.mass.config.get_raw_player_config_value(
                player_id, CONF_OUTPUT_CHANNELS, "stereo"
            )
            channels = 1 if output_channels != "stereo" else 2
            fmt += (
                f";codec=pcm;rate={output_sample_rate};"
                f"bitrate={output_bit_depth};channels={channels}"
            )
        url = f"{self.mass.streams._server.base_url}/flow/{self.job_id}/{player_id}.{fmt}"
        self.expected_players.add(player_id)
        return url

    async def iter_player_audio(
        self, player_id: str, output_format: AudioFormat, chunk_size: int | None = None
    ) -> AsyncGenerator[bytes, None]:
        """Subscribe consumer and iterate player-specific audio."""
        async for chunk in get_ffmpeg_stream(
            audio_input=self.subscribe(player_id),
            input_format=self.pcm_format,
            output_format=output_format,
            filter_params=get_player_filter_params(self.mass, player_id),
            chunk_size=chunk_size,
        ):
            yield chunk

    async def stream_to_custom_output_path(
        self, player_id: str, output_format: AudioFormat, output_path: str | int
    ) -> None:
        """Subscribe consumer and instruct ffmpeg to send the audio to the given output path."""
        custom_file_pointer = isinstance(output_path, int)
        ffmpeg_args = get_ffmpeg_args(
            input_format=self.pcm_format,
            output_format=output_format,
            filter_params=get_player_filter_params(self.mass, player_id),
            extra_args=[],
            input_path="-",
            output_path="-" if custom_file_pointer else output_path,
            loglevel="info" if self.logger.isEnabledFor(VERBOSE_LOG_LEVEL) else "quiet",
        )
        # launch ffmpeg process with player specific settings
        # the stream_job_runner will start pushing pcm chunks to the stdin
        # the ffmpeg process will send the output directly to the given path (e.g. tcp socket)
        async with AsyncProcess(
            ffmpeg_args,
            enable_stdin=True,
            enable_stdout=custom_file_pointer,
            enable_stderr=False,
            custom_stdin=self.subscribe(player_id),
            custom_stdout=output_path if custom_file_pointer else None,
            name="ffmpeg_custom_output_path",
        ) as ffmpeg_proc:
            # we simply wait for the process to exit
            await ffmpeg_proc.wait()

    async def subscribe(self, player_id: str) -> AsyncGenerator[bytes, None]:
        """Subscribe consumer and iterate incoming chunks on the queue."""
        try:
            self.subscribed_players[player_id] = sub_queue = asyncio.Queue(2)

            if self._all_clients_connected.is_set():
                # client subscribes while we're already started
                self.logger.warning(
                    "Client %s is joining while the stream is already started", player_id
                )

            self.logger.debug("Subscribed client %s", player_id)

            if (
                self.expected_players
                and self.allow_start
                and len(self.subscribed_players) == len(self.expected_players)
            ):
                # we reached the number of expected subscribers, set event
                # so that chunks can be pushed
                self._all_clients_connected.set()

            # keep reading audio chunks from the queue until we receive an empty one
            while True:
                chunk = await sub_queue.get()
                if chunk == b"":
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
                self.stop()

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
        async for chunk in self.pcm_audio_source:
            chunk_num += 1
            if chunk_num == 1:
                # wait until all expected clients are connected
                try:
                    async with asyncio.timeout(10):
                        await self._all_clients_connected.wait()
                except TimeoutError:
                    if len(self.subscribed_players) == 0:
                        self.logger.exception(
                            "Abort multi client stream job  %s: "
                            "client(s) did not connect within timeout",
                            self.job_id,
                        )
                        break
                    # not all clients connected but timeout expired, set flag and move on
                    # with all clients that did connect
                    self._all_clients_connected.set()
                else:
                    self.logger.debug(
                        "Starting queue stream job %s with %s (out of %s) connected clients",
                        self.job_id,
                        len(self.subscribed_players),
                        len(self.expected_players),
                    )

            await self._put_chunk(chunk)

        # mark EOF with empty chunk
        await self._put_chunk(b"")


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
        self.stream_jobs: dict[str, QueueStreamJob] = {}
        self.register_dynamic_route = self._server.register_dynamic_route
        self.unregister_dynamic_route = self._server.unregister_dynamic_route
        self.manifest.name = "Streamserver"
        self.manifest.description = (
            "Music Assistant's core controller that is responsible for "
            "streaming audio to players on the local network as well as "
            "some player specific local control callbacks."
        )
        self.manifest.icon = "cast-audio"
        self.announcements: dict[str, str] = {}

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
        default_port = await select_free_port(8097, 9200)
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
        # copy log level to audio module
        AUDIO_LOGGER.setLevel(self.logger.level)
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
                    "/flow/{job_id}/{player_id}.{fmt}",
                    self.serve_queue_flow_stream,
                ),
                (
                    "*",
                    "/single/{queue_id}/{queue_item_id}.{fmt}",
                    self.serve_queue_item_stream,
                ),
                (
                    "*",
                    "/command/{queue_id}/{command}.mp3",
                    self.serve_command_request,
                ),
                (
                    "*",
                    "/announcement/{player_id}.{fmt}",
                    self.serve_announcement_stream,
                ),
            ],
        )

    async def close(self) -> None:
        """Cleanup on exit."""
        await self._server.close()

    def resolve_stream_url(
        self,
        player_id: str,
        queue_item: QueueItem,
        output_codec: ContentType,
        flow_mode: bool = False,
    ) -> str:
        """Resolve the stream URL for the given QueueItem."""
        fmt = output_codec.value
        # handle announcement item
        if queue_item.media_type == MediaType.ANNOUNCEMENT:
            return queue_item.queue_item_id
        # handle request for (multi client) queue flow stream
        if queue_item.queue_id.startswith(UGP_PREFIX):
            # special case: we got forwarded a request from a Universal Group Player
            # use the existing stream job that was already created by UGP
            stream_job = self.mass.streams.stream_jobs[queue_item.queue_id]
            return stream_job.resolve_stream_url(player_id, output_codec)

        if flow_mode:
            # create a new flow mode stream job session
            pcm_format = AudioFormat(
                content_type=ContentType.from_bit_depth(24),
                sample_rate=FLOW_DEFAULT_SAMPLE_RATE,
                bit_depth=FLOW_DEFAULT_BIT_DEPTH,
            )
            stream_job = self.create_stream_job(
                queue_item.queue_id,
                pcm_audio_source=self.get_flow_stream(
                    self.mass.player_queues.get(queue_item.queue_id),
                    start_queue_item=queue_item,
                    pcm_format=pcm_format,
                ),
                pcm_format=pcm_format,
                auto_start=True,
            )

            return stream_job.resolve_stream_url(player_id, output_codec)

        # handle raw pcm without exact format specifiers
        if output_codec.is_pcm() and ";" not in fmt:
            fmt += f";codec=pcm;rate={44100};bitrate={16};channels={2}"
        query_params = {}
        url = (
            f"{self._server.base_url}/single/{queue_item.queue_id}/{queue_item.queue_item_id}.{fmt}"
        )
        # we add a timestamp as basic checksum
        # most importantly this is to invalidate any caches
        # but also to handle edge cases such as single track repeat
        query_params["ts"] = str(int(time.time()))
        url += "?" + urllib.parse.urlencode(query_params)
        return url

    def create_stream_job(
        self,
        queue_id: str,
        pcm_audio_source: AsyncGenerator[bytes, None],
        pcm_format: AudioFormat,
        auto_start: bool = False,
    ) -> QueueStreamJob:
        """
        Create a QueueStreamJob for the given queue..

        This is called by player/sync group implementations to start streaming
        the queue audio to multiple players at once.
        """
        if existing_job := self.stream_jobs.pop(queue_id, None):
            # cleanup existing job first
            existing_job.stop()
        self.stream_jobs[queue_id] = stream_job = QueueStreamJob(
            self.mass,
            pcm_audio_source=pcm_audio_source,
            pcm_format=pcm_format,
            auto_start=auto_start,
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
        if not queue_item.streamdetails:
            raise web.HTTPNotFound(reason=f"No streamdetails for Queue item: {queue_item_id}")
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
        pcm_format = AudioFormat(
            content_type=ContentType.from_bit_depth(
                queue_item.streamdetails.audio_format.bit_depth
            ),
            sample_rate=queue_item.streamdetails.audio_format.sample_rate,
            bit_depth=queue_item.streamdetails.audio_format.bit_depth,
        )
        async for chunk in get_ffmpeg_stream(
            audio_input=get_media_stream(
                self.mass,
                streamdetails=queue_item.streamdetails,
                pcm_format=pcm_format,
            ),
            input_format=pcm_format,
            output_format=output_format,
            filter_params=get_player_filter_params(self.mass, queue_player.player_id),
        ):
            try:
                await resp.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                break

        return resp

    async def serve_queue_flow_stream(self, request: web.Request) -> web.Response:
        """Stream Queue Flow audio to player."""
        self._log_request(request)
        job_id = request.match_info["job_id"]
        for queue_id, stream_job in self.stream_jobs.items():
            if stream_job.job_id == job_id:
                break
        else:
            raise web.HTTPNotFound(reason=f"Unknown StreamJob: {job_id}")
        if stream_job.finished:
            raise web.HTTPNotFound(reason=f"StreamJob {job_id} already finished")
        if not (queue := self.mass.player_queues.get(queue_id)):
            raise web.HTTPNotFound(reason=f"Unknown Queue: {queue_id}")
        player_id = request.match_info["player_id"]
        child_player = self.mass.players.get(player_id)
        if not child_player:
            raise web.HTTPNotFound(reason=f"Unknown player: {player_id}")
        # work out (childplayer specific!) output format/details
        output_format = await self._get_output_format(
            output_format_str=request.match_info["fmt"],
            queue_player=child_player,
            default_sample_rate=stream_job.pcm_format.sample_rate,
            default_bit_depth=stream_job.pcm_format.bit_depth,
        )
        # play it safe: only allow icy metadata for mp3 and aac
        enable_icy = request.headers.get(
            "Icy-MetaData", ""
        ) == "1" and output_format.content_type in (ContentType.MP3, ContentType.AAC)
        icy_meta_interval = 16384 * 4 if output_format.content_type.is_lossless() else 16384

        # prepare request, add some DLNA/UPNP compatible headers
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

        # some players (e.g. dlna, sonos) misbehave and do multiple GET requests
        # to the stream in an attempt to get the audio details such as duration
        # which is a bit pointless for our duration-less queue stream
        # and it completely messes with the subscription logic
        if player_id in stream_job.subscribed_players:
            self.logger.warning(
                "Player %s is making multiple requests "
                "to the same stream, playback may be disturbed!",
                player_id,
            )
        elif "rincon" in player_id.lower():
            await asyncio.sleep(0.1)

        # all checks passed, start streaming!
        self.logger.debug(
            "Start serving Queue flow audio stream for queue %s to player %s",
            queue.display_name,
            child_player.display_name,
        )
        async for chunk in stream_job.iter_player_audio(
            player_id, output_format, chunk_size=icy_meta_interval if enable_icy else None
        ):
            try:
                await resp.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
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

    async def serve_command_request(self, request: web.Request) -> web.Response:
        """Handle special 'command' request for a player."""
        self._log_request(request)
        queue_id = request.match_info["queue_id"]
        command = request.match_info["command"]
        if command == "next":
            self.mass.create_task(self.mass.player_queues.next(queue_id))
        return web.FileResponse(SILENCE_FILE)

    async def serve_announcement_stream(self, request: web.Request) -> web.Response:
        """Stream announcement audio to a player."""
        self._log_request(request)
        player_id = request.match_info["player_id"]
        player = self.mass.player_queues.get(player_id)
        if not player:
            raise web.HTTPNotFound(reason=f"Unknown Player: {player_id}")
        if player_id not in self.announcements:
            raise web.HTTPNotFound(reason=f"No pending announcements for Player: {player_id}")
        announcement = self.announcements[player_id]
        use_pre_announce = try_parse_bool(request.query.get("pre_announce"))

        # work out output format/details
        fmt = request.match_info.get("fmt", announcement.rsplit(".")[-1])
        audio_format = AudioFormat(content_type=ContentType.try_parse(fmt))
        # prepare request, add some DLNA/UPNP compatible headers
        headers = {
            **DEFAULT_STREAM_HEADERS,
            "Content-Type": f"audio/{audio_format.output_format_str}",
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
            "Start serving audio stream for Announcement %s to %s",
            announcement,
            player.display_name,
        )
        extra_args = []
        filter_params = ["loudnorm=I=-10:LRA=7:tp=-2:offset=-0.5"]
        if use_pre_announce:
            extra_args += [
                "-i",
                ANNOUNCE_ALERT_FILE,
                "-filter_complex",
                "[1:a][0:a]concat=n=2:v=0:a=1,loudnorm=I=-10:LRA=7:tp=-2:offset=-0.5",
            ]
            filter_params = []

        async for chunk in get_ffmpeg_stream(
            audio_input=announcement,
            input_format=audio_format,
            output_format=audio_format,
            extra_args=extra_args,
            filter_params=filter_params,
        ):
            try:
                await resp.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                break

        self.logger.debug(
            "Finished serving audio stream for Announcement %s to %s",
            announcement,
            player.display_name,
        )

        return resp

    def get_command_url(self, player_or_queue_id: str, command: str) -> str:
        """Get the url for the special command stream."""
        return f"{self.base_url}/command/{player_or_queue_id}/{command}.mp3"

    def get_announcement_url(
        self,
        player_id: str,
        announcement_url: str,
        use_pre_announce: bool = False,
        content_type: ContentType = ContentType.MP3,
    ) -> str:
        """Get the url for the special announcement stream."""
        self.announcements[player_id] = announcement_url
        return f"{self.base_url}/announcement/{player_id}.{content_type.value}?pre_announce={use_pre_announce}"  # noqa: E501

    async def get_flow_stream(
        self,
        queue: PlayerQueue,
        start_queue_item: QueueItem,
        pcm_format: AudioFormat,
    ) -> AsyncGenerator[bytes, None]:
        """Get a flow stream of all tracks in the queue as raw PCM audio."""
        # ruff: noqa: PLR0915
        assert pcm_format.content_type.is_pcm()
        queue_track = None
        last_fadeout_part = b""
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
            else:
                try:
                    queue_track = await self.mass.player_queues.preload_next_item(queue.queue_id)
                except QueueEmpty:
                    break

            if queue_track.streamdetails is None:
                raise RuntimeError(
                    "No Streamdetails known for queue item %s", queue_track.queue_item_id
                )

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
            buffer_size = int(pcm_sample_size * 2)  # 2 seconds
            if use_crossfade:
                buffer_size += crossfade_size
            bytes_written = 0
            buffer = b""
            # handle incoming audio chunks
            async for chunk in get_media_stream(
                self.mass,
                queue_track.streamdetails,
                pcm_format=pcm_format,
                # strip silence from begin/end if track is being crossfaded
                strip_silence_begin=use_crossfade,
                strip_silence_end=use_crossfade,
            ):
                # ALWAYS APPEND CHUNK TO BUFFER
                buffer += chunk
                del chunk
                if len(buffer) < buffer_size:
                    # buffer is not full enough, move on
                    continue

                ####  HANDLE CROSSFADE OF PREVIOUS TRACK AND NEW TRACK
                if last_fadeout_part:
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

                #### OTHER: enough data in buffer, feed to output
                else:
                    yield buffer[:pcm_sample_size]
                    bytes_written += pcm_sample_size
                    buffer = buffer[pcm_sample_size:]

            #### HANDLE END OF TRACK
            if last_fadeout_part:
                # edge case: we did not get enough data to make the crossfade
                yield last_fadeout_part
                bytes_written += len(last_fadeout_part)
                last_fadeout_part = b""
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

            # update duration details based on the actual pcm data we sent
            # this also accounts for crossfade and silence stripping
            seconds_streamed = (bytes_written + len(last_fadeout_part)) / pcm_sample_size
            queue_track.streamdetails.seconds_streamed = seconds_streamed
            queue_track.streamdetails.duration = (
                queue_track.streamdetails.seek_position + seconds_streamed
            )
            self.logger.debug(
                "Finished Streaming queue track: %s (%s) on queue %s - seconds streamed: %s",
                queue_track.streamdetails.uri,
                queue_track.name,
                queue.display_name,
                seconds_streamed,
            )

        # end of queue flow: make sure we yield the last_fadeout_part
        if last_fadeout_part:
            yield last_fadeout_part
            del last_fadeout_part
        del buffer
        self.logger.info("Finished Queue Flow stream for Queue %s", queue.display_name)

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
