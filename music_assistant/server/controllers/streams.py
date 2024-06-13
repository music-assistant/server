"""
Controller to stream audio to players.

The streams controller hosts a basic, unprotected HTTP-only webserver
purely to stream audio packets to players and some control endpoints such as
the upnp callbacks and json rpc api for slimproto clients.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import urllib.parse
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

from aiofiles.os import wrap
from aiohttp import web

from music_assistant.common.helpers.util import get_ip, select_free_port, try_parse_bool
from music_assistant.common.models.config_entries import (
    ConfigEntry,
    ConfigValueOption,
    ConfigValueType,
)
from music_assistant.common.models.enums import ConfigEntryType, ContentType, MediaType, StreamType
from music_assistant.common.models.errors import QueueEmpty
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
    CONF_SAMPLE_RATES,
    SILENCE_FILE,
)
from music_assistant.server.helpers.audio import LOGGER as AUDIO_LOGGER
from music_assistant.server.helpers.audio import (
    FFMpeg,
    check_audio_support,
    crossfade_pcm_parts,
    get_ffmpeg_stream,
    get_hls_stream,
    get_icy_stream,
    get_player_filter_params,
    get_silence,
    parse_loudnorm,
    strip_silence,
)
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

isfile = wrap(os.path.isfile)


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
        self.register_dynamic_route = self._server.register_dynamic_route
        self.unregister_dynamic_route = self._server.unregister_dynamic_route
        self.manifest.name = "Streamserver"
        self.manifest.description = (
            "Music Assistant's core controller that is responsible for "
            "streaming audio to players on the local network."
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
        major_version = int("".join(char for char in version.split(".")[0] if not char.isalpha()))
        if not ffmpeg_present:
            self.logger.error("FFmpeg binary not found on your system, playback will NOT work!.")
        elif major_version < 6:
            self.logger.error("FFMpeg version is too old, you may run into playback issues.")
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
        queue_item: QueueItem,
        flow_mode: bool = False,
        output_codec: ContentType = ContentType.FLAC,
    ) -> str:
        """Resolve the stream URL for the given QueueItem."""
        fmt = output_codec.value
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
            player=queue_player,
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
            content_type=ContentType.from_bit_depth(output_format.bit_depth),
            sample_rate=queue_item.streamdetails.audio_format.sample_rate,
            bit_depth=queue_item.streamdetails.audio_format.bit_depth,
            channels=2,
        )
        async for chunk in get_ffmpeg_stream(
            audio_input=self.get_media_stream(
                streamdetails=queue_item.streamdetails,
                pcm_format=pcm_format,
            ),
            input_format=pcm_format,
            output_format=output_format,
            filter_params=get_player_filter_params(self.mass, queue_player.player_id),
        ):
            try:
                await resp.write(chunk)
            except (BrokenPipeError, ConnectionResetError, ConnectionError):
                break
        if queue.stream_finished is not None:
            queue.stream_finished = True
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

        # select the highest possible PCM settings for this player
        flow_pcm_format = await self._select_flow_format(queue_player)

        # work out output format/details
        output_format = await self._get_output_format(
            output_format_str=request.match_info["fmt"],
            player=queue_player,
            default_sample_rate=flow_pcm_format.sample_rate,
            default_bit_depth=flow_pcm_format.bit_depth,
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

        async for chunk in get_ffmpeg_stream(
            audio_input=self.get_flow_stream(
                queue=queue, start_queue_item=start_queue_item, pcm_format=flow_pcm_format
            ),
            input_format=flow_pcm_format,
            output_format=output_format,
            filter_params=get_player_filter_params(self.mass, queue_player.player_id),
            chunk_size=icy_meta_interval if enable_icy else None,
        ):
            try:
                await resp.write(chunk)
            except (BrokenPipeError, ConnectionResetError, ConnectionError):
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
        # use stream server to host announcement on local network
        # this ensures playback on all players, including ones that do not
        # like https hosts and it also offers the pre-announce 'bell'
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
        queue.stream_finished = False
        use_crossfade = await self.mass.config.get_player_config_value(
            queue.queue_id, CONF_CROSSFADE
        )
        if start_queue_item.media_type != MediaType.TRACK:
            use_crossfade = False
        pcm_sample_size = int(
            pcm_format.sample_rate * (pcm_format.bit_depth / 8) * pcm_format.channels
        )
        self.logger.info(
            "Start Queue Flow stream for Queue %s - crossfade: %s",
            queue.display_name,
            use_crossfade,
        )
        total_bytes_sent = 0
        started = time.time()

        while True:
            # get (next) queue item to stream
            if queue_track is None:
                queue_track = start_queue_item
            else:
                try:
                    queue_track = await self.mass.player_queues.preload_next_item(
                        queue.queue_id, allow_repeat=False
                    )
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
            crossfade_duration = await self.mass.config.get_player_config_value(
                queue.queue_id, CONF_CROSSFADE_DURATION
            )
            crossfade_size = int(pcm_sample_size * crossfade_duration)
            bytes_written = 0
            buffer = b""
            # handle incoming audio chunks
            async for chunk in self.get_media_stream(
                queue_track.streamdetails,
                pcm_format=pcm_format,
            ):
                # buffer size needs to be big enough to include the crossfade part
                # allow it to be a bit smaller when playback just starts
                if not use_crossfade:
                    req_buffer_size = pcm_sample_size * 2
                elif (time.time() - started) > 120:
                    # additional 5 seconds to strip silence from last part
                    req_buffer_size = crossfade_size + pcm_sample_size * 5
                else:
                    req_buffer_size = crossfade_size

                # ALWAYS APPEND CHUNK TO BUFFER
                buffer += chunk
                del chunk
                if len(buffer) < req_buffer_size:
                    # buffer is not full enough, move on
                    continue

                ####  HANDLE CROSSFADE OF PREVIOUS TRACK AND NEW TRACK
                if last_fadeout_part:
                    # strip silence from last part
                    buffer = await strip_silence(
                        self.mass,
                        buffer,
                        sample_rate=pcm_format.sample_rate,
                        bit_depth=pcm_format.bit_depth,
                        reverse=False,
                    )
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
                while len(buffer) > req_buffer_size:
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
                # strip silence from last part
                buffer = await strip_silence(
                    self.mass,
                    buffer,
                    sample_rate=pcm_format.sample_rate,
                    bit_depth=pcm_format.bit_depth,
                    reverse=True,
                )
                # if crossfade is enabled, save fadeout part to pickup for next track
                last_fadeout_part = buffer[-crossfade_size:]
                remaining_bytes = buffer[:-crossfade_size]
                if remaining_bytes:
                    yield remaining_bytes
                    bytes_written += len(remaining_bytes)
                del remaining_bytes
            elif buffer:
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
            total_bytes_sent += bytes_written
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
        total_bytes_sent += bytes_written
        if queue.stream_finished is not None:
            queue.stream_finished = True
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
                "[1:a][0:a]concat=n=2:v=0:a=1,loudnorm=I=-10:LRA=11:TP=-1.5",
            ]
            filter_params = []
        async for chunk in get_ffmpeg_stream(
            audio_input=announcement_url,
            input_format=audio_format,
            output_format=output_format,
            extra_args=extra_args,
            filter_params=filter_params,
        ):
            yield chunk

    async def get_media_stream(
        self,
        streamdetails: StreamDetails,
        pcm_format: AudioFormat,
    ) -> AsyncGenerator[tuple[bool, bytes], None]:
        """Get the audio stream for the given streamdetails as raw pcm chunks."""
        logger = self.logger.getChild("media_stream")
        is_radio = streamdetails.media_type == MediaType.RADIO or not streamdetails.duration
        if is_radio:
            streamdetails.seek_position = 0
        # collect all arguments for ffmpeg
        filter_params = []
        extra_input_args = []
        # add loudnorm filter: volume normalization
        # more info: https://k.ylo.ph/2016/04/04/loudnorm.html
        if streamdetails.target_loudness is not None:
            if streamdetails.loudness:
                # we have a measurement so we can do linear mode
                target_loudness = streamdetails.target_loudness
                # we must ensure that target loudness does not exceed the measured value
                # otherwise ffmpeg falls back to dynamic again
                # https://github.com/slhck/ffmpeg-normalize/issues/251
                target_loudness = min(
                    streamdetails.target_loudness,
                    streamdetails.loudness.integrated + streamdetails.loudness.lra - 1,
                )
                filter_rule = f"loudnorm=I={target_loudness}:TP=-2.0:LRA=7.0:linear=true"
                filter_rule += f":measured_I={streamdetails.loudness.integrated}"
                filter_rule += f":measured_LRA={streamdetails.loudness.lra}"
                filter_rule += f":measured_tp={streamdetails.loudness.true_peak}"
                filter_rule += f":measured_thresh={streamdetails.loudness.threshold}"
                if streamdetails.loudness.target_offset is not None:
                    filter_rule += f":offset={streamdetails.loudness.target_offset}"
            else:
                # if we have no measurement, we use dynamic mode
                # which also collects the measurement on the fly during playback
                filter_rule = (
                    f"loudnorm=I={streamdetails.target_loudness}:TP=-2.0:LRA=7.0:offset=0.0"
                )
            filter_rule += ":print_format=json"
            filter_params.append(filter_rule)
        if streamdetails.fade_in:
            filter_params.append("afade=type=in:start_time=0:duration=3")
        if streamdetails.stream_type == StreamType.CUSTOM:
            audio_source = self.mass.get_provider(streamdetails.provider).get_audio_stream(
                streamdetails,
                seek_position=streamdetails.seek_position,
            )
        elif streamdetails.stream_type == StreamType.ICY:
            audio_source = get_icy_stream(self.mass, streamdetails.path, streamdetails)
            # pad some silence before the radio stream starts to create some headroom
            # for radio stations that do not provide any look ahead buffer
            # without this, some radio streams jitter a lot
            async for chunk in get_silence(2, pcm_format):
                yield chunk
        elif streamdetails.stream_type == StreamType.HLS:
            audio_source = get_hls_stream(
                self.mass, streamdetails.path, streamdetails, streamdetails.seek_position
            )
        elif streamdetails.stream_type == StreamType.ENCRYPTED_HTTP:
            audio_source = streamdetails.path
            extra_input_args += ["-decryption_key", streamdetails.decryption_key]
        else:
            audio_source = streamdetails.path
            if streamdetails.seek_position:
                extra_input_args += ["-ss", str(int(streamdetails.seek_position))]

        logger.debug("start media stream for: %s", streamdetails.uri)
        bytes_sent = 0
        finished = False
        try:
            async with FFMpeg(
                audio_input=audio_source,
                input_format=streamdetails.audio_format,
                output_format=pcm_format,
                filter_params=filter_params,
                extra_input_args=extra_input_args,
                collect_log_history=True,
                logger=logger,
            ) as ffmpeg_proc:
                async for chunk in ffmpeg_proc.iter_chunked(pcm_format.pcm_sample_size):
                    bytes_sent += len(chunk)
                    yield chunk
                    # del chunk
                finished = True
        finally:
            if finished and not ffmpeg_proc.closed:
                await asyncio.wait_for(ffmpeg_proc.wait(), 60)
            elif not ffmpeg_proc.closed:
                await ffmpeg_proc.close()

            # try to determine how many seconds we've streamed
            seconds_streamed = bytes_sent / pcm_format.pcm_sample_size if bytes_sent else 0
            logger.debug(
                "stream %s (with code %s) for %s - seconds streamed: %s",
                "finished" if finished else "aborted",
                ffmpeg_proc.returncode,
                streamdetails.uri,
                seconds_streamed,
            )
            streamdetails.seconds_streamed = seconds_streamed
            # store accurate duration
            if finished and not streamdetails.seek_position and seconds_streamed:
                streamdetails.duration = seconds_streamed

            # parse loudnorm data if we have that collected
            if loudness_details := parse_loudnorm(" ".join(ffmpeg_proc.log_history)):
                required_seconds = 600 if streamdetails.media_type == MediaType.RADIO else 120
                if finished or (seconds_streamed >= required_seconds):
                    logger.debug(
                        "Loudness measurement for %s: %s",
                        streamdetails.uri,
                        loudness_details,
                    )
                    streamdetails.loudness = loudness_details
                    self.mass.create_task(
                        self.mass.music.set_track_loudness(
                            streamdetails.item_id, streamdetails.provider, loudness_details
                        )
                    )
            # report playback
            if finished or seconds_streamed > 30:
                self.mass.create_task(
                    self.mass.music.mark_item_played(
                        streamdetails.media_type,
                        streamdetails.item_id,
                        streamdetails.provider,
                    )
                )
                if music_prov := self.mass.get_provider(streamdetails.provider):
                    self.mass.create_task(music_prov.on_streamed(streamdetails, seconds_streamed))

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
        player: Player,
        default_sample_rate: int,
        default_bit_depth: int,
    ) -> AudioFormat:
        """Parse (player specific) output format details for given format string."""
        content_type: ContentType = ContentType.try_parse(output_format_str)
        supported_rates_conf = await self.mass.config.get_player_config_value(
            player.player_id, CONF_SAMPLE_RATES
        )
        supported_sample_rates: tuple[int] = tuple(x[0] for x in supported_rates_conf)
        supported_bit_depths: tuple[int] = tuple(x[1] for x in supported_rates_conf)
        player_max_bit_depth = max(supported_bit_depths)
        if content_type.is_pcm() or content_type == ContentType.WAV:
            # parse pcm details from format string
            output_sample_rate, output_bit_depth, output_channels = parse_pcm_info(
                output_format_str
            )
            if content_type == ContentType.PCM:
                # resolve generic pcm type
                content_type = ContentType.from_bit_depth(output_bit_depth)
        else:
            if default_sample_rate in supported_sample_rates:
                output_sample_rate = default_sample_rate
            else:
                output_sample_rate = min(supported_sample_rates)
            output_bit_depth = min(default_bit_depth, player_max_bit_depth)
            output_channels_str = self.mass.config.get_raw_player_config_value(
                player.player_id, CONF_OUTPUT_CHANNELS, "stereo"
            )
            output_channels = 1 if output_channels_str != "stereo" else 2
        return AudioFormat(
            content_type=content_type,
            sample_rate=output_sample_rate,
            bit_depth=output_bit_depth,
            channels=output_channels,
            output_format_str=output_format_str,
        )

    async def _select_flow_format(
        self,
        player: Player,
    ) -> AudioFormat:
        """Parse (player specific) flow stream PCM format."""
        supported_rates_conf = await self.mass.config.get_player_config_value(
            player.player_id, CONF_SAMPLE_RATES
        )
        supported_sample_rates: tuple[int] = tuple(x[0] for x in supported_rates_conf)
        supported_bit_depths: tuple[int] = tuple(x[1] for x in supported_rates_conf)
        player_max_bit_depth = max(supported_bit_depths)
        for sample_rate in (192000, 96000, 48000, 44100):
            if sample_rate in supported_sample_rates:
                output_sample_rate = sample_rate
                break
        output_bit_depth = min(24, player_max_bit_depth)
        return AudioFormat(
            content_type=ContentType.from_bit_depth(output_bit_depth),
            sample_rate=output_sample_rate,
            bit_depth=output_bit_depth,
            channels=2,
        )
