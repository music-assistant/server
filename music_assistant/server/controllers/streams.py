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
from typing import TYPE_CHECKING, Any

import shortuuid
from aiohttp import web

from music_assistant.common.helpers.util import get_ip, select_free_port, try_parse_bool
from music_assistant.common.models.config_entries import (
    ConfigEntry,
    ConfigValueOption,
    ConfigValueType,
)
from music_assistant.common.models.enums import ConfigEntryType, ContentType, MediaType
from music_assistant.common.models.errors import AudioError, QueueEmpty
from music_assistant.common.models.media_items import AudioFormat
from music_assistant.common.models.streamdetails import StreamDetails
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
    get_player_filter_params,
    get_radio_stream,
    parse_loudnorm,
    strip_silence,
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


class MultiClientStreamJob:
    """
    Representation of a (multiclient) Audio Queue stream job/task.

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
    ) -> None:
        """Initialize MultiClientStreamJob instance."""
        self.stream_controller = stream_controller
        self.queue_id = queue_id
        self.queue = self.stream_controller.mass.player_queues.get(queue_id)
        assert self.queue  # just in case
        self.pcm_format = pcm_format
        self.start_queue_item = start_queue_item
        self.job_id = shortuuid.uuid()
        self.expected_players: set[str] = set()
        self.subscribed_players: dict[str, asyncio.Queue[bytes]] = {}
        self.bytes_streamed: int = 0
        self._all_clients_connected = asyncio.Event()
        self.logger = stream_controller.logger.getChild("streamjob")
        self._finished: bool = False
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
                sub_queue.put_nowait(b"")

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
        url = f"{self.stream_controller._server.base_url}/multi/{self.queue_id}/{self.job_id}/{child_player_id}/{self.start_queue_item.queue_item_id}.{fmt}"  # noqa: E501
        self.expected_players.add(child_player_id)
        return url

    async def subscribe(self, player_id: str) -> AsyncGenerator[bytes, None]:
        """Subscribe consumer and iterate incoming chunks on the queue."""
        try:
            # some players (e.g. dlna, sonos) misbehave and do multiple GET requests
            # to the stream in an attempt to get the audio details such as duration
            # which is a bit pointless for our duration-less queue stream
            # and it completely messes with the subscription logic
            if player_id in self.subscribed_players:
                self.logger.warning(
                    "Player %s is making multiple requests "
                    "to the same stream, playback may be disturbed!",
                    player_id,
                )
                player_id = f"{player_id}_{shortuuid.random(4)}"
            elif self._all_clients_connected.is_set():
                # client subscribes while we're already started - that is going to be messy for sure
                self.logger.warning(
                    "Player %s is is joining while the stream is already started, "
                    "playback may be disturbed!",
                    player_id,
                )

            self.subscribed_players[player_id] = sub_queue = asyncio.Queue(2)

            if self._all_clients_connected.is_set():
                # client subscribes while we're already started - we dont support that (for now?)
                msg = f"Client {player_id} is joining while the stream is already started"
                raise RuntimeError(msg)
            self.logger.debug("Subscribed client %s", player_id)

            if len(self.subscribed_players) == len(self.expected_players):
                # we reached the number of expected subscribers, set event
                # so that chunks can be pushed
                await asyncio.sleep(0.2)
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
        self.multi_client_jobs: dict[str, MultiClientStreamJob] = {}
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
                category="advanced",
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
                category="advanced",
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
                    "/flow/{queue_id}/{queue_item_id}.{fmt}",
                    self.serve_queue_flow_stream,
                ),
                (
                    "*",
                    "/single/{queue_id}/{queue_item_id}.{fmt}",
                    self.serve_queue_item_stream,
                ),
                (
                    "*",
                    "/multi/{queue_id}/{job_id}/{player_id}/{queue_item_id}.{fmt}",
                    self.serve_multi_subscriber_stream,
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
        # handle special stream created by UGP
        if queue_item.queue_id.startswith(UGP_PREFIX):
            return self.multi_client_jobs[queue_item.queue_id].resolve_stream_url(
                player_id, output_codec
            )
        # handle announcement item
        if queue_item.media_type == MediaType.ANNOUNCEMENT:
            return self.get_announcement_url(
                player_id=queue_item.queue_id,
                announcement_url=queue_item.streamdetails.data["url"],
                use_pre_announce=queue_item.streamdetails.data["use_pre_announce"],
                content_type=output_codec,
            )
        # handle raw pcm without exact format specifiers
        if output_codec.is_pcm() and ";" not in fmt:
            fmt += f";codec=pcm;rate={44100};bitrate={16};channels={2}"
        query_params = {}
        base_path = "flow" if flow_mode else "single"
        url = f"{self._server.base_url}/{base_path}/{queue_item.queue_id}/{queue_item.queue_item_id}.{fmt}"  # noqa: E501
        # we add a timestamp as basic checksum
        # most importantly this is to invalidate any caches
        # but also to handle edge cases such as single track repeat
        query_params["ts"] = str(int(time.time()))
        url += "?" + urllib.parse.urlencode(query_params)
        return url

    def create_multi_client_stream_job(
        self,
        queue_id: str,
        start_queue_item: QueueItem,
        pcm_bit_depth: int = FLOW_DEFAULT_BIT_DEPTH,
        pcm_sample_rate: int = FLOW_DEFAULT_SAMPLE_RATE,
    ) -> MultiClientStreamJob:
        """Create a MultiClientStreamJob for the given queue..

        This is called by player/sync group implementations to start streaming
        the queue audio to multiple players at once.
        """
        if existing_job := self.multi_client_jobs.pop(queue_id, None):
            if (
                queue_id.startswith(UGP_PREFIX)
                and existing_job.job_id == start_queue_item.queue_item_id
            ):
                return existing_job
            # cleanup existing job first
            if not existing_job.finished:
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
            audio_input=self._get_media_stream(
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
        queue_id = request.match_info["queue_id"]
        queue = self.mass.player_queues.get(queue_id)
        if not queue:
            raise web.HTTPNotFound(reason=f"Unknown Queue: {queue_id}")
        if not (queue_player := self.mass.players.get(queue_id)):
            raise web.HTTPNotFound(reason=f"Unknown Player: {queue_id}")
        start_queue_item_id = request.match_info["queue_item_id"]
        start_queue_item = self.mass.player_queues.get_item(queue_id, start_queue_item_id)
        if not start_queue_item:
            raise web.HTTPNotFound(reason=f"Unknown Queue item: {start_queue_item_id}")
        # work out output format/details
        output_format = await self._get_output_format(
            output_format_str=request.match_info["fmt"],
            queue_player=queue_player,
            default_sample_rate=FLOW_DEFAULT_SAMPLE_RATE,
            default_bit_depth=FLOW_DEFAULT_BIT_DEPTH,
        )
        # play it safe: only allow icy metadata for mp3 and aac
        enable_icy = request.headers.get(
            "Icy-MetaData", ""
        ) == "1" and output_format.content_type in (ContentType.MP3, ContentType.AAC)
        icy_meta_interval = 16384

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

        # all checks passed, start streaming!
        self.logger.debug("Start serving Queue flow audio stream for %s", queue.display_name)

        # collect player specific ffmpeg args to re-encode the source PCM stream
        pcm_format = AudioFormat(
            content_type=ContentType.from_bit_depth(output_format.bit_depth),
            sample_rate=output_format.sample_rate,
            bit_depth=output_format.bit_depth,
            channels=2,
        )
        async for chunk in get_ffmpeg_stream(
            audio_input=self.get_flow_stream(
                queue=queue, start_queue_item=start_queue_item, pcm_format=pcm_format
            ),
            input_format=pcm_format,
            output_format=output_format,
            filter_params=get_player_filter_params(self.mass, queue_player.player_id),
            chunk_size=icy_meta_interval if enable_icy else None,
        ):
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

        # all checks passed, start streaming!
        self.logger.debug(
            "Start serving multi-subscriber Queue flow audio stream for queue %s to player %s",
            streamjob.queue.display_name,
            child_player.display_name,
        )

        async for chunk in get_ffmpeg_stream(
            audio_input=streamjob.subscribe(child_player_id),
            input_format=streamjob.pcm_format,
            output_format=output_format,
            filter_params=get_player_filter_params(self.mass, child_player_id),
        ):
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

    async def serve_announcement_stream(self, request: web.Request) -> web.Response:
        """Stream announcement audio to a player."""
        self._log_request(request)
        player_id = request.match_info["player_id"]
        player = self.mass.player_queues.get(player_id)
        if not player:
            raise web.HTTPNotFound(reason=f"Unknown Player: {player_id}")
        if player_id not in self.announcements:
            raise web.HTTPNotFound(reason=f"No pending announcements for Player: {player_id}")
        announcement_url = self.announcements[player_id]
        use_pre_announce = try_parse_bool(request.query.get("pre_announce"))

        # work out output format/details
        fmt = request.match_info.get("fmt", announcement_url.rsplit(".")[-1])
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
            announcement_url,
            player.display_name,
        )
        async for chunk in self.get_announcement_stream(
            announcement_url=announcement_url,
            output_format=audio_format,
            use_pre_announce=use_pre_announce,
        ):
            try:
                await resp.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                break

        self.logger.debug(
            "Finished serving audio stream for Announcement %s to %s",
            announcement_url,
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
            bytes_written = 0
            buffer = b""
            # handle incoming audio chunks
            async for chunk in self._get_media_stream(
                queue_track.streamdetails,
                pcm_format=pcm_format,
                # strip silence from begin/end if track is being crossfaded
                strip_silence_begin=use_crossfade and bytes_written > 0,
                strip_silence_end=use_crossfade,
            ):
                # required buffer size is a bit dynamic,
                # it needs to be small when the flow stream starts
                seconds_streamed = int(bytes_written / pcm_sample_size)
                if not use_crossfade or seconds_streamed < 5:
                    buffer_size = pcm_sample_size
                elif seconds_streamed < 10:
                    buffer_size = pcm_sample_size * 2
                elif use_crossfade and seconds_streamed < 20:
                    buffer_size = pcm_sample_size * 5
                else:
                    buffer_size = crossfade_size + pcm_sample_size * 2
                    # buffer size needs to be big enough to include the crossfade part

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
                    # send crossfade_part (as one big chunk)
                    bytes_written += len(crossfade_part)
                    yield crossfade_part

                    # also write the leftover bytes from the crossfade action
                    if remaining_bytes:
                        yield remaining_bytes
                        bytes_written += len(remaining_bytes)
                        del remaining_bytes
                    # clear vars
                    last_fadeout_part = b""
                    buffer = b""

                #### OTHER: enough data in buffer, feed to output
                while len(buffer) > buffer_size:
                    subchunk = buffer[:pcm_sample_size]
                    buffer = buffer[pcm_sample_size:]
                    bytes_written += len(subchunk)
                    yield subchunk
                    del subchunk

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
                # no crossfade enabled, just yield the buffer last part
                bytes_written += len(buffer)
                yield buffer
            # make sure the buffer gets cleaned up
            del buffer

            # update duration details based on the actual pcm data we sent
            # this also accounts for crossfade and silence stripping
            seconds_streamed = bytes_written / pcm_sample_size
            queue_track.streamdetails.seconds_streamed = seconds_streamed
            queue_track.streamdetails.duration = (
                queue_track.streamdetails.seek_position + seconds_streamed
            )
            self.logger.debug(
                "Finished Streaming queue track: %s (%s) on queue %s",
                queue_track.streamdetails.uri,
                queue_track.name,
                queue.display_name,
            )
        #### HANDLE END OF QUEUE FLOW STREAM
        # end of queue flow: make sure we yield the last_fadeout_part
        if last_fadeout_part:
            yield last_fadeout_part
            # correct seconds streamed/duration
            last_part_seconds = len(last_fadeout_part) / pcm_sample_size
            queue_track.streamdetails.seconds_streamed += last_part_seconds
            queue_track.streamdetails.duration += last_part_seconds
            del last_fadeout_part

        self.logger.info("Finished Queue Flow stream for Queue %s", queue.display_name)

    async def get_announcement_stream(
        self, announcement_url: str, output_format: AudioFormat, use_pre_announce: bool = False
    ) -> AsyncGenerator[bytes, None]:
        """Get the special announcement stream."""
        # work out output format/details
        fmt = announcement_url.rsplit(".")[-1]
        audio_format = AudioFormat(content_type=ContentType.try_parse(fmt))
        extra_args = []
        filter_params = ["loudnorm=I=-10:LRA=11:TP=-2"]
        if use_pre_announce:
            extra_args += [
                "-i",
                ANNOUNCE_ALERT_FILE,
                "-filter_complex",
                "[1:a][0:a]concat=n=2:v=0:a=1,loudnorm=I=-10:LRA=11:TP=-2",
            ]
            filter_params = []
        async for chunk in get_ffmpeg_stream(
            audio_input=announcement_url,
            input_format=audio_format,
            output_format=output_format,
            extra_args=extra_args,
            filter_params=filter_params,
            loglevel="info",
        ):
            yield chunk

    async def _get_media_stream(
        self,
        streamdetails: StreamDetails,
        pcm_format: AudioFormat,
        strip_silence_begin: bool = False,
        strip_silence_end: bool = False,
    ) -> AsyncGenerator[tuple[bool, bytes], None]:
        """
        Get the (raw PCM) audio stream for the given streamdetails.

        Other than stripping silence at end and beginning and optional
        volume normalization this is the pure, unaltered audio data as PCM chunks.
        """
        logger = self.logger.getChild("media_stream")
        is_radio = streamdetails.media_type == MediaType.RADIO or not streamdetails.duration
        if is_radio or streamdetails.seek_position:
            strip_silence_begin = False
        if is_radio or streamdetails.duration < 30:
            strip_silence_end = False
        # pcm_sample_size = chunk size = 1 second of pcm audio
        pcm_sample_size = pcm_format.pcm_sample_size
        buffer_size_begin = pcm_sample_size * 2 if strip_silence_begin else pcm_sample_size
        buffer_size_end = pcm_sample_size * 5 if strip_silence_end else pcm_sample_size

        # collect all arguments for ffmpeg
        filter_params = []
        extra_args = []
        seek_pos = (
            streamdetails.seek_position
            if (streamdetails.direct or not streamdetails.can_seek)
            else 0
        )
        if seek_pos:
            # only use ffmpeg seeking if the provider stream does not support seeking
            extra_args += ["-ss", str(seek_pos)]
        if streamdetails.target_loudness is not None:
            # add loudnorm filters
            filter_rule = f"loudnorm=I={streamdetails.target_loudness}:LRA=11:TP=-2"
            if streamdetails.loudness:
                filter_rule += f":measured_I={streamdetails.loudness.integrated}"
                filter_rule += f":measured_LRA={streamdetails.loudness.lra}"
                filter_rule += f":measured_tp={streamdetails.loudness.true_peak}"
                filter_rule += f":measured_thresh={streamdetails.loudness.threshold}"
            filter_rule += ":print_format=json"
            filter_params.append(filter_rule)
        if streamdetails.fade_in:
            filter_params.append("afade=type=in:start_time=0:duration=3")

        if is_radio and streamdetails.direct and streamdetails.direct.startswith("http"):
            # ensure we use the radio streamer for radio items
            audio_source_iterator = get_radio_stream(self.mass, streamdetails.direct, streamdetails)
            input_path = "-"
        elif streamdetails.direct:
            audio_source_iterator = None
            input_path = streamdetails.direct
        else:
            audio_source_iterator = self.mass.get_provider(streamdetails.provider).get_audio_stream(
                streamdetails,
                seek_position=streamdetails.seek_position if streamdetails.can_seek else 0,
            )
            input_path = "-"

        ffmpeg_args = get_ffmpeg_args(
            input_format=streamdetails.audio_format,
            output_format=pcm_format,
            filter_params=filter_params,
            extra_args=extra_args,
            input_path=input_path,
            # loglevel info is needed for loudness measurement
            loglevel="info",
            extra_input_args=["-filter_threads", "1"],
        )

        async def log_reader(ffmpeg_proc: AsyncProcess, state_data: dict[str, Any]):
            # To prevent stderr locking up, we must keep reading it
            stderr_data = ""
            async for line in ffmpeg_proc.iter_stderr():
                line = line.decode().strip()  # noqa: PLW2901
                # if streamdetails contenttype is uinknown, try pars eit from the ffmpeg log output
                # this has no actual usecase, other than displaying the correct codec in the UI
                if (
                    streamdetails.audio_format.content_type == ContentType.UNKNOWN
                    and line.startswith("Stream #0:0: Audio: ")
                ):
                    streamdetails.audio_format.content_type = ContentType.try_parse(
                        line.split("Stream #0:0: Audio: ")[1].split(" ")[0]
                    )
                if stderr_data or "loudnorm" in line:
                    stderr_data += line
                elif "HTTP error" in line:
                    logger.warning(line)
                elif line:
                    logger.log(VERBOSE_LOG_LEVEL, line)
                del line

            # if we reach this point, the process is finished (finish or aborted)
            if ffmpeg_proc.returncode == 0:
                await state_data["finished"].wait()
            finished = ffmpeg_proc.returncode == 0 and state_data["finished"].is_set()
            bytes_sent = state_data["bytes_sent"]
            seconds_streamed = bytes_sent / pcm_format.pcm_sample_size if bytes_sent else 0
            streamdetails.seconds_streamed = seconds_streamed
            state_str = "finished" if finished else "aborted"
            logger.debug(
                "stream %s for: %s (%s seconds streamed, exitcode %s)",
                state_str,
                streamdetails.uri,
                seconds_streamed,
                ffmpeg_proc.returncode,
            )
            # store accurate duration
            if finished:
                streamdetails.duration = streamdetails.seek_position + seconds_streamed

            # parse loudnorm data if we have that collected
            if stderr_data and (loudness_details := parse_loudnorm(stderr_data)):
                required_seconds = 600 if streamdetails.media_type == MediaType.RADIO else 120
                if finished or (seconds_streamed >= required_seconds):
                    logger.debug(
                        "Loudness measurement for %s: %s", streamdetails.uri, loudness_details
                    )
                    streamdetails.loudness = loudness_details
                    await self.mass.music.set_track_loudness(
                        streamdetails.item_id, streamdetails.provider, loudness_details
                    )

            # report playback
            if finished or seconds_streamed > 30:
                self.mass.create_task(
                    self.mass.music.mark_item_played(
                        streamdetails.media_type, streamdetails.item_id, streamdetails.provider
                    )
                )
                if music_prov := self.mass.get_provider(streamdetails.provider):
                    self.mass.create_task(music_prov.on_streamed(streamdetails, seconds_streamed))
            # cleanup
            del stderr_data

        async with AsyncProcess(
            ffmpeg_args,
            enable_stdin=audio_source_iterator is not None,
            enable_stderr=True,
            custom_stdin=audio_source_iterator,
            name="ffmpeg_media_stream",
        ) as ffmpeg_proc:
            state_data = {"finished": asyncio.Event(), "bytes_sent": 0}
            logger.debug("start media stream for: %s", streamdetails.uri)

            self.mass.create_task(log_reader(ffmpeg_proc, state_data))

            # get pcm chunks from stdout
            # we always stay buffer_size of bytes behind
            # so we can strip silence at the beginning and end of a track
            buffer = b""
            chunk_num = 0
            async for chunk in ffmpeg_proc.iter_chunked(pcm_sample_size):
                chunk_num += 1
                required_buffer = buffer_size_begin if chunk_num < 10 else buffer_size_end
                buffer += chunk
                del chunk

                if len(buffer) < required_buffer:
                    # buffer is not full enough, move on
                    continue

                if strip_silence_begin and chunk_num == 2:
                    # first 2 chunks received, strip silence of beginning
                    stripped_audio = await strip_silence(
                        self.mass,
                        buffer,
                        sample_rate=pcm_format.sample_rate,
                        bit_depth=pcm_format.bit_depth,
                    )
                    yield stripped_audio
                    state_data["bytes_sent"] += len(stripped_audio)
                    buffer = b""
                    del stripped_audio
                    continue

                #### OTHER: enough data in buffer, feed to output
                while len(buffer) > required_buffer:
                    subchunk = buffer[:pcm_sample_size]
                    buffer = buffer[pcm_sample_size:]
                    state_data["bytes_sent"] += len(subchunk)
                    yield subchunk
                    del subchunk

            # if we did not receive any data, something went (terribly) wrong
            # raise here to prevent an (endless) loop elsewhere
            if state_data["bytes_sent"] == 0:
                raise AudioError(f"stream error on {streamdetails.uri}")

            # all chunks received, strip silence of last part if needed and yield remaining bytes
            if strip_silence_end:
                final_chunk = await strip_silence(
                    self.mass,
                    buffer,
                    sample_rate=pcm_format.sample_rate,
                    bit_depth=pcm_format.bit_depth,
                    reverse=True,
                )
            else:
                final_chunk = buffer

            # yield final chunk to output (as one big chunk)
            yield final_chunk
            state_data["bytes_sent"] += len(final_chunk)
            state_data["finished"].set()
            del final_chunk
            del buffer

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
