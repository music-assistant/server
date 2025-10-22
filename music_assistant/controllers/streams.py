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
import urllib.parse
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import TYPE_CHECKING

from aiofiles.os import wrap
from aiohttp import web
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption, ConfigValueType
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    MediaType,
    PlayerFeature,
    StreamType,
    VolumeNormalizationMode,
)
from music_assistant_models.errors import QueueEmpty
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.player_queue import PlayLogEntry

from music_assistant.constants import (
    ANNOUNCE_ALERT_FILE,
    CONF_BIND_IP,
    CONF_BIND_PORT,
    CONF_CROSSFADE_DURATION,
    CONF_ENTRY_ENABLE_ICY_METADATA,
    CONF_HTTP_PROFILE,
    CONF_OUTPUT_CHANNELS,
    CONF_OUTPUT_CODEC,
    CONF_PUBLISH_IP,
    CONF_SAMPLE_RATES,
    CONF_SMART_FADES_MODE,
    CONF_VOLUME_NORMALIZATION_FIXED_GAIN_RADIO,
    CONF_VOLUME_NORMALIZATION_FIXED_GAIN_TRACKS,
    CONF_VOLUME_NORMALIZATION_RADIO,
    CONF_VOLUME_NORMALIZATION_TRACKS,
    DEFAULT_PCM_FORMAT,
    DEFAULT_STREAM_HEADERS,
    ICY_HEADERS,
    SILENCE_FILE,
    VERBOSE_LOG_LEVEL,
)
from music_assistant.controllers.players.player_controller import AnnounceData
from music_assistant.helpers.audio import LOGGER as AUDIO_LOGGER
from music_assistant.helpers.audio import (
    get_chunksize,
    get_media_stream,
    get_player_filter_params,
    get_silence,
    get_stream_details,
    resample_pcm_audio,
)
from music_assistant.helpers.ffmpeg import LOGGER as FFMPEG_LOGGER
from music_assistant.helpers.ffmpeg import check_ffmpeg_version, get_ffmpeg_stream
from music_assistant.helpers.smart_fades import (
    SMART_CROSSFADE_DURATION,
    SmartFadesMixer,
    SmartFadesMode,
)
from music_assistant.helpers.util import get_ip_addresses, select_free_port
from music_assistant.helpers.webserver import Webserver
from music_assistant.models.core_controller import CoreController
from music_assistant.models.plugin import PluginProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import CoreConfig
    from music_assistant_models.player_queue import PlayerQueue
    from music_assistant_models.queue_item import QueueItem
    from music_assistant_models.streamdetails import StreamDetails

    from music_assistant.mass import MusicAssistant
    from music_assistant.models.player import Player


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


@dataclass
class CrossfadeData:
    """Data class to hold crossfade data."""

    data: bytes
    fade_in_size: int
    pcm_format: AudioFormat
    queue_item_id: str
    session_id: str


class StreamsController(CoreController):
    """Webserver Controller to stream audio to players."""

    domain: str = "streams"

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize instance."""
        super().__init__(mass)
        self._server = Webserver(self.logger, enable_dynamic_routes=True)
        self.register_dynamic_route = self._server.register_dynamic_route
        self.unregister_dynamic_route = self._server.unregister_dynamic_route
        self.manifest.name = "Streamserver"
        self.manifest.description = (
            "Music Assistant's core controller that is responsible for "
            "streaming audio to players on the local network."
        )
        self.manifest.icon = "cast-audio"
        self.announcements: dict[str, AnnounceData] = {}
        self._crossfade_data: dict[str, CrossfadeData] = {}
        self._bind_ip: str = "0.0.0.0"
        self._smart_fades_mixer = SmartFadesMixer(self.mass)

    @property
    def base_url(self) -> str:
        """Return the base_url for the streamserver."""
        return self._server.base_url

    @property
    def bind_ip(self) -> str:
        """Return the IP address this streamserver is bound to."""
        return self._bind_ip

    async def get_config_entries(
        self,
        action: str | None = None,
        values: dict[str, ConfigValueType] | None = None,
    ) -> tuple[ConfigEntry, ...]:
        """Return all Config Entries for this core module (if any)."""
        ip_addresses = await get_ip_addresses()
        default_port = await select_free_port(8097, 9200)
        return (
            ConfigEntry(
                key=CONF_PUBLISH_IP,
                type=ConfigEntryType.STRING,
                default_value=ip_addresses[0],
                label="Published IP address",
                description="This IP address is communicated to players where to find this server."
                "\nMake sure that this IP can be reached by players on the local network, "
                "otherwise audio streaming will not work.",
                required=False,
            ),
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
                key=CONF_VOLUME_NORMALIZATION_RADIO,
                type=ConfigEntryType.STRING,
                default_value=VolumeNormalizationMode.FALLBACK_DYNAMIC,
                label="Volume normalization method for radio streams",
                options=[
                    ConfigValueOption(x.value.replace("_", " ").title(), x.value)
                    for x in VolumeNormalizationMode
                ],
                category="audio",
            ),
            ConfigEntry(
                key=CONF_VOLUME_NORMALIZATION_TRACKS,
                type=ConfigEntryType.STRING,
                default_value=VolumeNormalizationMode.FALLBACK_DYNAMIC,
                label="Volume normalization method for tracks",
                options=[
                    ConfigValueOption(x.value.replace("_", " ").title(), x.value)
                    for x in VolumeNormalizationMode
                ],
                category="audio",
            ),
            ConfigEntry(
                key=CONF_VOLUME_NORMALIZATION_FIXED_GAIN_RADIO,
                type=ConfigEntryType.FLOAT,
                range=(-20, 10),
                default_value=-6,
                label="Fixed/fallback gain adjustment for radio streams",
                category="audio",
            ),
            ConfigEntry(
                key=CONF_VOLUME_NORMALIZATION_FIXED_GAIN_TRACKS,
                type=ConfigEntryType.FLOAT,
                range=(-20, 10),
                default_value=-6,
                label="Fixed/fallback gain adjustment for tracks",
                category="audio",
            ),
            ConfigEntry(
                key=CONF_BIND_IP,
                type=ConfigEntryType.STRING,
                default_value="0.0.0.0",
                options=[ConfigValueOption(x, x) for x in {"0.0.0.0", *ip_addresses}],
                label="Bind to IP/interface",
                description="Start the stream server on this specific interface. \n"
                "Use 0.0.0.0 to bind to all interfaces, which is the default. \n"
                "This is an advanced setting that should normally "
                "not be adjusted in regular setups.",
                category="advanced",
                required=False,
            ),
        )

    async def setup(self, config: CoreConfig) -> None:
        """Async initialize of module."""
        # copy log level to audio/ffmpeg loggers
        AUDIO_LOGGER.setLevel(self.logger.level)
        FFMPEG_LOGGER.setLevel(self.logger.level)
        # perform check for ffmpeg version
        await check_ffmpeg_version()
        # start the webserver
        self.publish_port = config.get_value(CONF_BIND_PORT)
        self.publish_ip = config.get_value(CONF_PUBLISH_IP)
        self._bind_ip = bind_ip = str(config.get_value(CONF_BIND_IP))
        # print a big fat message in the log where the streamserver is running
        # because this is a common source of issues for people with more complex setups
        self.logger.log(
            logging.INFO if self.mass.config.onboard_done else logging.WARNING,
            "\n\n################################################################################\n"
            "Starting streamserver on  %s:%s\n"
            "This is the IP address that is communicated to players.\n"
            "If this is incorrect, audio will not play!\n"
            "See the documentation how to configure the publish IP for the Streamserver\n"
            "in Settings --> Core modules --> Streamserver\n"
            "################################################################################\n",
            self.publish_ip,
            self.publish_port,
        )
        await self._server.setup(
            bind_ip=bind_ip,
            bind_port=self.publish_port,
            base_url=f"http://{self.publish_ip}:{self.publish_port}",
            static_routes=[
                (
                    "*",
                    "/flow/{session_id}/{queue_id}/{queue_item_id}.{fmt}",
                    self.serve_queue_flow_stream,
                ),
                (
                    "*",
                    "/single/{session_id}/{queue_id}/{queue_item_id}.{fmt}",
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
                (
                    "*",
                    "/pluginsource/{plugin_source}/{player_id}.{fmt}",
                    self.serve_plugin_source_stream,
                ),
            ],
        )

    async def close(self) -> None:
        """Cleanup on exit."""
        await self._server.close()

    async def resolve_stream_url(
        self,
        session_id: str,
        queue_item: QueueItem,
        flow_mode: bool = False,
        player_id: str | None = None,
    ) -> str:
        """Resolve the stream URL for the given QueueItem."""
        if not player_id:
            player_id = queue_item.queue_id
        try:
            conf_output_codec = await self.mass.config.get_player_config_value(
                player_id, CONF_OUTPUT_CODEC
            )
        except KeyError:
            conf_output_codec = "flac"
        output_codec = ContentType.try_parse(conf_output_codec)
        fmt = output_codec.value
        # handle raw pcm without exact format specifiers
        if output_codec.is_pcm() and ";" not in fmt:
            fmt += f";codec=pcm;rate={44100};bitrate={16};channels={2}"
        base_path = "flow" if flow_mode else "single"
        return f"{self._server.base_url}/{base_path}/{session_id}/{queue_item.queue_id}/{queue_item.queue_item_id}.{fmt}"  # noqa: E501

    async def get_plugin_source_url(
        self,
        plugin_source: str,
        player_id: str,
    ) -> str:
        """Get the url for the Plugin Source stream/proxy."""
        output_codec = ContentType.try_parse(
            await self.mass.config.get_player_config_value(player_id, CONF_OUTPUT_CODEC)
        )
        fmt = output_codec.value
        # handle raw pcm without exact format specifiers
        if output_codec.is_pcm() and ";" not in fmt:
            fmt += f";codec=pcm;rate={44100};bitrate={16};channels={2}"
        return f"{self._server.base_url}/pluginsource/{plugin_source}/{player_id}.{fmt}"

    async def serve_queue_item_stream(self, request: web.Request) -> web.Response:
        """Stream single queueitem audio to a player."""
        self._log_request(request)
        queue_id = request.match_info["queue_id"]
        queue = self.mass.player_queues.get(queue_id)
        if not queue:
            raise web.HTTPNotFound(reason=f"Unknown Queue: {queue_id}")
        session_id = request.match_info["session_id"]
        if queue.session_id and session_id != queue.session_id:
            raise web.HTTPNotFound(reason=f"Unknown (or invalid) session: {session_id}")
        queue_player = self.mass.players.get(queue_id)
        queue_item_id = request.match_info["queue_item_id"]
        queue_item = self.mass.player_queues.get_item(queue_id, queue_item_id)
        if not queue_item:
            raise web.HTTPNotFound(reason=f"Unknown Queue item: {queue_item_id}")
        if not queue_item.streamdetails:
            try:
                queue_item.streamdetails = await get_stream_details(
                    mass=self.mass, queue_item=queue_item
                )
            except Exception as e:
                self.logger.error(
                    "Failed to get streamdetails for QueueItem %s: %s", queue_item_id, e
                )
                queue_item.available = False
                raise web.HTTPNotFound(reason=f"No streamdetails for Queue item: {queue_item_id}")

        # pick output format based on the streamdetails and player capabilities
        output_format = await self.get_output_format(
            output_format_str=request.match_info["fmt"],
            player=queue_player,
            content_sample_rate=queue_item.streamdetails.audio_format.sample_rate,
            # always use f32 internally for extra headroom for filters etc
            content_bit_depth=DEFAULT_PCM_FORMAT.bit_depth,
        )

        # prepare request, add some DLNA/UPNP compatible headers
        headers = {
            **DEFAULT_STREAM_HEADERS,
            "icy-name": queue_item.name,
            "Accept-Ranges": "none",
            "Content-Type": f"audio/{output_format.output_format_str}",
        }
        resp = web.StreamResponse(
            status=200,
            reason="OK",
            headers=headers,
        )
        resp.content_type = f"audio/{output_format.output_format_str}"
        http_profile: str = await self.mass.config.get_player_config_value(
            queue_id, CONF_HTTP_PROFILE
        )
        if http_profile == "forced_content_length" and not queue_item.duration:
            # just set an insane high content length to make sure the player keeps playing
            resp.content_length = get_chunksize(output_format, 12 * 3600)
        elif http_profile == "forced_content_length":
            # guess content length based on duration
            resp.content_length = get_chunksize(output_format, queue_item.duration)
        elif http_profile == "chunked":
            resp.enable_chunked_encoding()

        await resp.prepare(request)

        # return early if this is not a GET request
        if request.method != "GET":
            return resp

        if queue_item.media_type != MediaType.TRACK:
            # no crossfade on non-tracks
            smart_fades_mode = SmartFadesMode.DISABLED
        else:
            smart_fades_mode = await self.mass.config.get_player_config_value(
                queue.queue_id, CONF_SMART_FADES_MODE
            )
            standard_crossfade_duration = self.mass.config.get_raw_player_config_value(
                queue.queue_id, CONF_CROSSFADE_DURATION, 10
            )
        if (
            smart_fades_mode != SmartFadesMode.DISABLED
            and PlayerFeature.GAPLESS_PLAYBACK not in queue_player.supported_features
        ):
            # crossfade is not supported on this player due to missing gapless playback
            self.logger.warning(
                "Crossfade disabled: Player %s does not support gapless playback",
                queue_player.display_name if queue_player else "Unknown Player",
            )
            smart_fades_mode = SmartFadesMode.DISABLED

        # work out pcm format based on output format
        pcm_format = AudioFormat(
            sample_rate=output_format.sample_rate,
            # always use f32 internally for extra headroom for filters etc
            content_type=ContentType.PCM_F32LE,
            bit_depth=DEFAULT_PCM_FORMAT.bit_depth,
            channels=2,
        )
        if smart_fades_mode != SmartFadesMode.DISABLED:
            # crossfade is enabled, use special crossfaded single item stream
            # where the crossfade of the next track is present in the stream of
            # a single track. This only works if the player supports gapless playback.

            audio_input = self.get_queue_item_stream_with_smartfade(
                queue_item=queue_item,
                pcm_format=pcm_format,
                session_id=session_id,
                smart_fades_mode=smart_fades_mode,
                standard_crossfade_duration=standard_crossfade_duration,
            )
        else:
            # no crossfade, just a regular single item stream
            audio_input = self.get_queue_item_stream(
                queue_item=queue_item,
                pcm_format=pcm_format,
            )
        # stream the audio
        # this final ffmpeg process in the chain will convert the raw, lossless PCM audio into
        # the desired output format for the player including any player specific filter params
        # such as channels mixing, DSP, resampling and, only if needed, encoding to lossy formats
        async for chunk in get_ffmpeg_stream(
            audio_input=audio_input,
            input_format=pcm_format,
            output_format=output_format,
            filter_params=get_player_filter_params(
                self.mass,
                player_id=queue_player.player_id,
                input_format=pcm_format,
                output_format=output_format,
            ),
            chunk_size=get_chunksize(output_format),
        ):
            try:
                await resp.write(chunk)
            except (BrokenPipeError, ConnectionResetError, ConnectionError):
                break
        if queue_item.streamdetails.stream_error:
            self.logger.error(
                "Error streaming QueueItem %s (%s) to %s",
                queue_item.name,
                queue_item.uri,
                queue.display_name,
            )
            # some players do not like it when we dont return anything after an error
            # so we send some silence so they move on to the next track on their own (hopefully)
            async for chunk in get_silence(10, output_format):
                try:
                    await resp.write(chunk)
                except (BrokenPipeError, ConnectionResetError, ConnectionError):
                    break
        return resp

    async def serve_queue_flow_stream(self, request: web.Request) -> web.Response:
        """Stream Queue Flow audio to player."""
        self._log_request(request)
        queue_id = request.match_info["queue_id"]
        queue = self.mass.player_queues.get(queue_id)
        if not queue:
            raise web.HTTPNotFound(reason=f"Unknown Queue: {queue_id}")
        session_id = request.match_info["session_id"]
        if session_id != queue.session_id:
            raise web.HTTPNotFound(reason=f"Unknown (or invalid) session: {session_id}")
        if not (queue_player := self.mass.players.get(queue_id)):
            raise web.HTTPNotFound(reason=f"Unknown Player: {queue_id}")
        start_queue_item_id = request.match_info["queue_item_id"]
        start_queue_item = self.mass.player_queues.get_item(queue_id, start_queue_item_id)
        if not start_queue_item:
            raise web.HTTPNotFound(reason=f"Unknown Queue item: {start_queue_item_id}")

        # select the highest possible PCM settings for this player
        flow_pcm_format = await self._select_flow_format(queue_player)

        # work out output format/details
        output_format = await self.get_output_format(
            output_format_str=request.match_info["fmt"],
            player=queue_player,
            content_sample_rate=flow_pcm_format.sample_rate,
            content_bit_depth=flow_pcm_format.bit_depth,
        )
        # work out ICY metadata support
        icy_preference = self.mass.config.get_raw_player_config_value(
            queue_id,
            CONF_ENTRY_ENABLE_ICY_METADATA.key,
            CONF_ENTRY_ENABLE_ICY_METADATA.default_value,
        )
        enable_icy = request.headers.get("Icy-MetaData", "") == "1" and icy_preference != "disabled"
        icy_meta_interval = 256000 if icy_preference == "full" else 16384

        # prepare request, add some DLNA/UPNP compatible headers
        headers = {
            **DEFAULT_STREAM_HEADERS,
            **ICY_HEADERS,
            "Accept-Ranges": "none",
            "Content-Type": f"audio/{output_format.output_format_str}",
        }
        if enable_icy:
            headers["icy-metaint"] = str(icy_meta_interval)

        resp = web.StreamResponse(
            status=200,
            reason="OK",
            headers=headers,
        )
        http_profile_value = await self.mass.config.get_player_config_value(
            queue_id, CONF_HTTP_PROFILE
        )
        http_profile = str(http_profile_value) if http_profile_value is not None else "default"
        if http_profile == "forced_content_length":
            # just set an insane high content length to make sure the player keeps playing
            resp.content_length = get_chunksize(output_format, 12 * 3600)
        elif http_profile == "chunked":
            resp.enable_chunked_encoding()

        await resp.prepare(request)

        # return early if this is not a GET request
        if request.method != "GET":
            return resp

        # all checks passed, start streaming!
        # this final ffmpeg process in the chain will convert the raw, lossless PCM audio into
        # the desired output format for the player including any player specific filter params
        # such as channels mixing, DSP, resampling and, only if needed, encoding to lossy formats
        self.logger.debug("Start serving Queue flow audio stream for %s", queue.display_name)

        async for chunk in get_ffmpeg_stream(
            audio_input=self.get_queue_flow_stream(
                queue=queue,
                start_queue_item=start_queue_item,
                pcm_format=flow_pcm_format,
            ),
            input_format=flow_pcm_format,
            output_format=output_format,
            filter_params=get_player_filter_params(
                self.mass, queue_player.player_id, flow_pcm_format, output_format
            ),
            chunk_size=icy_meta_interval if enable_icy else get_chunksize(output_format),
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
            if icy_preference == "full" and current_item and current_item.image:
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
        return web.FileResponse(SILENCE_FILE, headers={"icy-name": "Music Assistant"})

    async def serve_announcement_stream(self, request: web.Request) -> web.Response:
        """Stream announcement audio to a player."""
        self._log_request(request)
        player_id = request.match_info["player_id"]
        player = self.mass.player_queues.get(player_id)
        if not player:
            raise web.HTTPNotFound(reason=f"Unknown Player: {player_id}")
        if not (announce_data := self.announcements.get(player_id)):
            raise web.HTTPNotFound(reason=f"No pending announcements for Player: {player_id}")

        # work out output format/details
        fmt = request.match_info["fmt"]
        audio_format = AudioFormat(content_type=ContentType.try_parse(fmt))

        http_profile_value = await self.mass.config.get_player_config_value(
            player_id, CONF_HTTP_PROFILE
        )
        http_profile = str(http_profile_value) if http_profile_value is not None else "default"
        if http_profile == "forced_content_length":
            # given the fact that an announcement is just a short audio clip,
            # just send it over completely at once so we have a fixed content length
            data = b""
            async for chunk in self.get_announcement_stream(
                announcement_url=announce_data["announcement_url"],
                output_format=audio_format,
                pre_announce=announce_data["pre_announce"],
                pre_announce_url=announce_data["pre_announce_url"],
            ):
                data += chunk
            return web.Response(
                body=data,
                content_type=f"audio/{audio_format.output_format_str}",
                headers=DEFAULT_STREAM_HEADERS,
            )

        resp = web.StreamResponse(
            status=200,
            reason="OK",
            headers=DEFAULT_STREAM_HEADERS,
        )
        resp.content_type = f"audio/{audio_format.output_format_str}"
        if http_profile == "chunked":
            resp.enable_chunked_encoding()

        await resp.prepare(request)

        # return early if this is not a GET request
        if request.method != "GET":
            return resp

        # all checks passed, start streaming!
        self.logger.debug(
            "Start serving audio stream for Announcement %s to %s",
            announce_data["announcement_url"],
            player.display_name,
        )
        async for chunk in self.get_announcement_stream(
            announcement_url=announce_data["announcement_url"],
            output_format=audio_format,
            pre_announce=announce_data["pre_announce"],
            pre_announce_url=announce_data["pre_announce_url"],
        ):
            try:
                await resp.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                break

        self.logger.debug(
            "Finished serving audio stream for Announcement %s to %s",
            announce_data["announcement_url"],
            player.display_name,
        )

        return resp

    async def serve_plugin_source_stream(self, request: web.Request) -> web.Response:
        """Stream PluginSource audio to a player."""
        self._log_request(request)
        plugin_source_id = request.match_info["plugin_source"]
        provider: PluginProvider | None
        if not (provider := self.mass.get_provider(plugin_source_id)):
            raise web.HTTPNotFound(reason=f"Unknown PluginSource: {plugin_source_id}")
        # work out output format/details
        player_id = request.match_info["player_id"]
        player = self.mass.players.get(player_id)
        if not player:
            raise web.HTTPNotFound(reason=f"Unknown Player: {player_id}")
        plugin_source = provider.get_source()
        output_format = await self.get_output_format(
            output_format_str=request.match_info["fmt"],
            player=player,
            content_sample_rate=plugin_source.audio_format.sample_rate
            if plugin_source.audio_format
            else 44100,
            content_bit_depth=plugin_source.audio_format.bit_depth
            if plugin_source.audio_format
            else 16,
        )
        headers = {
            **DEFAULT_STREAM_HEADERS,
            "icy-name": plugin_source.name,
            "Accept-Ranges": "none",
            "Content-Type": f"audio/{output_format.output_format_str}",
        }

        resp = web.StreamResponse(
            status=200,
            reason="OK",
            headers=headers,
        )
        resp.content_type = f"audio/{output_format.output_format_str}"
        http_profile_value = await self.mass.config.get_player_config_value(
            player_id, CONF_HTTP_PROFILE
        )
        http_profile = str(http_profile_value) if http_profile_value is not None else "default"
        if http_profile == "forced_content_length":
            # guess content length based on duration
            resp.content_length = get_chunksize(output_format, 12 * 3600)
        elif http_profile == "chunked":
            resp.enable_chunked_encoding()

        await resp.prepare(request)

        # return early if this is not a GET request
        if request.method != "GET":
            return resp

        # all checks passed, start streaming!
        async for chunk in self.get_plugin_source_stream(
            plugin_source_id=plugin_source_id,
            output_format=output_format,
            player_id=player_id,
            player_filter_params=get_player_filter_params(
                self.mass, player_id, plugin_source.audio_format, output_format
            ),
        ):
            try:
                await resp.write(chunk)
            except (BrokenPipeError, ConnectionResetError, ConnectionError):
                break
        return resp

    def get_command_url(self, player_or_queue_id: str, command: str) -> str:
        """Get the url for the special command stream."""
        return f"{self.base_url}/command/{player_or_queue_id}/{command}.mp3"

    def get_announcement_url(
        self,
        player_id: str,
        announce_data: AnnounceData,
        content_type: ContentType = ContentType.MP3,
    ) -> str:
        """Get the url for the special announcement stream."""
        self.announcements[player_id] = announce_data
        # use stream server to host announcement on local network
        # this ensures playback on all players, including ones that do not
        # like https hosts and it also offers the pre-announce 'bell'
        return f"{self.base_url}/announcement/{player_id}.{content_type.value}"

    async def get_queue_flow_stream(
        self,
        queue: PlayerQueue,
        start_queue_item: QueueItem,
        pcm_format: AudioFormat,
    ) -> AsyncGenerator[bytes, None]:
        """Get a flow stream of all tracks in the queue as raw PCM audio."""
        # ruff: noqa: PLR0915
        assert pcm_format.content_type.is_pcm()
        queue_track = None
        last_fadeout_part: bytes = b""
        last_streamdetails: StreamDetails | None = None
        last_play_log_entry: PlayLogEntry | None = None
        queue.flow_mode = True
        if not start_queue_item:
            # this can happen in some (edge case) race conditions
            return
        pcm_sample_size = int(
            pcm_format.sample_rate * (pcm_format.bit_depth / 8) * pcm_format.channels
        )
        if start_queue_item.media_type != MediaType.TRACK:
            # no crossfade on non-tracks
            # NOTE that we shouldn't be using flow mode for non-tracks at all,
            # but just to be sure, we specifically disable crossfade here
            smart_fades_mode = SmartFadesMode.DISABLED
            standard_crossfade_duration = 0
        else:
            smart_fades_mode = await self.mass.config.get_player_config_value(
                queue.queue_id, CONF_SMART_FADES_MODE
            )
            standard_crossfade_duration = self.mass.config.get_raw_player_config_value(
                queue.queue_id, CONF_CROSSFADE_DURATION, 10
            )
        self.logger.info(
            "Start Queue Flow stream for Queue %s - crossfade: %s %s",
            queue.display_name,
            smart_fades_mode,
            f"({standard_crossfade_duration}s)"
            if smart_fades_mode == SmartFadesMode.STANDARD_CROSSFADE
            else "",
        )
        total_bytes_sent = 0

        while True:
            # get (next) queue item to stream
            if queue_track is None:
                queue_track = start_queue_item
            else:
                try:
                    queue_track = await self.mass.player_queues.load_next_queue_item(
                        queue.queue_id, queue_track.queue_item_id
                    )
                except QueueEmpty:
                    break

            if queue_track.streamdetails is None:
                raise RuntimeError(
                    "No Streamdetails known for queue item %s",
                    queue_track.queue_item_id,
                )

            self.logger.debug(
                "Start Streaming queue track: %s (%s) for queue %s",
                queue_track.streamdetails.uri,
                queue_track.name,
                queue.display_name,
            )
            # append to play log so the queue controller can work out which track is playing
            play_log_entry = PlayLogEntry(queue_track.queue_item_id)
            queue.flow_mode_stream_log.append(play_log_entry)
            if smart_fades_mode == SmartFadesMode.SMART_FADES:
                crossfade_size = int(pcm_format.pcm_sample_size * SMART_CROSSFADE_DURATION)
            else:
                crossfade_size = int(pcm_format.pcm_sample_size * standard_crossfade_duration + 4)
            bytes_written = 0
            buffer = b""
            # handle incoming audio chunks
            async for chunk in self.get_queue_item_stream(
                queue_track,
                pcm_format=pcm_format,
            ):
                # buffer size needs to be big enough to include the crossfade part
                req_buffer_size = (
                    pcm_sample_size
                    if smart_fades_mode == SmartFadesMode.DISABLED
                    else crossfade_size
                )

                # ALWAYS APPEND CHUNK TO BUFFER
                buffer += chunk
                del chunk
                if len(buffer) < req_buffer_size:
                    # buffer is not full enough, move on
                    continue

                ####  HANDLE CROSSFADE OF PREVIOUS TRACK AND NEW TRACK
                if last_fadeout_part and last_streamdetails:
                    # perform crossfade
                    fadein_part = buffer[:crossfade_size]
                    remaining_bytes = buffer[crossfade_size:]
                    # Use the mixer to handle all crossfade logic
                    crossfade_part = await self._smart_fades_mixer.mix(
                        fade_in_part=fadein_part,
                        fade_out_part=last_fadeout_part,
                        fade_in_streamdetails=queue_track.streamdetails,
                        fade_out_streamdetails=last_streamdetails,
                        pcm_format=pcm_format,
                        standard_crossfade_duration=standard_crossfade_duration,
                        mode=smart_fades_mode,
                    )
                    # because the crossfade exists of both the fadein and fadeout part
                    # we need to correct the bytes_written accordingly so the duration
                    # calculations at the end of the track are correct
                    crossfade_part_len = len(crossfade_part)
                    bytes_written += crossfade_part_len / 2
                    if last_play_log_entry:
                        last_play_log_entry.seconds_streamed += (
                            crossfade_part_len / 2 / pcm_sample_size
                        )
                    # send crossfade_part (as one big chunk)
                    yield crossfade_part
                    del crossfade_part
                    # also write the leftover bytes from the crossfade action
                    if remaining_bytes:
                        yield remaining_bytes
                        bytes_written += len(remaining_bytes)
                        del remaining_bytes
                    # clear vars
                    last_fadeout_part = b""
                    last_streamdetails = None
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
            if self._crossfade_allowed(
                queue_track, smart_fades_mode=smart_fades_mode, flow_mode=True
            ):
                # if crossfade is enabled, save fadeout part to pickup for next track
                last_fadeout_part = buffer[-crossfade_size:]
                last_streamdetails = queue_track.streamdetails
                last_play_log_entry = play_log_entry
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
            play_log_entry.seconds_streamed = seconds_streamed
            play_log_entry.duration = queue_track.streamdetails.duration
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
            last_fadeout_part = b""
        total_bytes_sent += bytes_written
        self.logger.info("Finished Queue Flow stream for Queue %s", queue.display_name)

    async def get_announcement_stream(
        self,
        announcement_url: str,
        output_format: AudioFormat,
        pre_announce: bool | str = False,
        pre_announce_url: str = ANNOUNCE_ALERT_FILE,
    ) -> AsyncGenerator[bytes, None]:
        """Get the special announcement stream."""
        filter_params = ["loudnorm=I=-10:LRA=11:TP=-2"]

        if pre_announce:
            # Note: TTS URLs might take a while to load cause the actual data are often generated
            # asynchronously by the TTS provider. If we ask ffmpeg to mix the pre-announce, it will
            # wait until it reads the TTS data, so the whole stream will be delayed. It is much
            # faster to first play the pre-announce using a separate ffmpeg stream, and only
            # afterwards play the TTS itself.
            #
            # For this to be effective the player itself needs to be able to start playback fast.
            # Finally, if the output_format is non-PCM, raw concatenation can be problematic.
            # So far players seem to tolerate this, but it might break some player in the future.

            async for chunk in get_ffmpeg_stream(
                audio_input=pre_announce_url,
                input_format=AudioFormat(content_type=ContentType.try_parse(pre_announce_url)),
                output_format=output_format,
                filter_params=filter_params,
            ):
                yield chunk

        # work out output format/details
        fmt = announcement_url.rsplit(".")[-1]
        audio_format = AudioFormat(content_type=ContentType.try_parse(fmt))
        async for chunk in get_ffmpeg_stream(
            audio_input=announcement_url,
            input_format=audio_format,
            output_format=output_format,
            filter_params=filter_params,
        ):
            yield chunk

    async def get_plugin_source_stream(
        self,
        plugin_source_id: str,
        output_format: AudioFormat,
        player_id: str,
        player_filter_params: list[str] | None = None,
    ) -> AsyncGenerator[bytes, None]:
        """Get the special plugin source stream."""
        player = self.mass.players.get(player_id)
        plugin_prov: PluginProvider = self.mass.get_provider(plugin_source_id)
        plugin_source = plugin_prov.get_source()
        if plugin_source.in_use_by and plugin_source.in_use_by != player_id:
            raise RuntimeError(
                f"PluginSource plugin_source.name is already in use by {plugin_source.in_use_by}"
            )
        self.logger.debug("Start streaming PluginSource %s to %s", plugin_source_id, player_id)
        audio_input = (
            plugin_prov.get_audio_stream(player_id)
            if plugin_source.stream_type == StreamType.CUSTOM
            else plugin_source.path
        )
        player.state.active_source = plugin_source_id
        plugin_source.in_use_by = player_id
        try:
            async for chunk in get_ffmpeg_stream(
                audio_input=audio_input,
                input_format=plugin_source.audio_format,
                output_format=output_format,
                filter_params=player_filter_params,
                extra_input_args=["-re"],
                chunk_size=int(get_chunksize(output_format) / 10),
            ):
                yield chunk
        finally:
            self.logger.debug(
                "Finished streaming PluginSource %s to %s", plugin_source_id, player_id
            )
            await asyncio.sleep(0.5)
            player.state.active_source = player.player_id
            plugin_source.in_use_by = None

    async def get_queue_item_stream(
        self,
        queue_item: QueueItem,
        pcm_format: AudioFormat,
    ) -> AsyncGenerator[bytes, None]:
        """Get the (PCM) audio stream for a single queue item."""
        # collect all arguments for ffmpeg
        streamdetails = queue_item.streamdetails
        assert streamdetails
        filter_params: list[str] = []

        # handle volume normalization
        gain_correct: float | None = None
        if streamdetails.volume_normalization_mode == VolumeNormalizationMode.DYNAMIC:
            # volume normalization using loudnorm filter (in dynamic mode)
            # which also collects the measurement on the fly during playback
            # more info: https://k.ylo.ph/2016/04/04/loudnorm.html
            filter_rule = f"loudnorm=I={streamdetails.target_loudness}:TP=-2.0:LRA=10.0:offset=0.0"
            filter_rule += ":print_format=json"
            filter_params.append(filter_rule)
        elif streamdetails.volume_normalization_mode == VolumeNormalizationMode.FIXED_GAIN:
            # apply used defined fixed volume/gain correction
            gain_correct: float = await self.mass.config.get_core_config_value(
                self.domain,
                CONF_VOLUME_NORMALIZATION_FIXED_GAIN_TRACKS
                if streamdetails.media_type == MediaType.TRACK
                else CONF_VOLUME_NORMALIZATION_FIXED_GAIN_RADIO,
            )
            gain_correct = round(gain_correct, 2)
            filter_params.append(f"volume={gain_correct}dB")
        elif streamdetails.volume_normalization_mode == VolumeNormalizationMode.MEASUREMENT_ONLY:
            # volume normalization with known loudness measurement
            # apply volume/gain correction
            if streamdetails.prefer_album_loudness and streamdetails.loudness_album is not None:
                gain_correct = streamdetails.target_loudness - streamdetails.loudness_album
            else:
                gain_correct = streamdetails.target_loudness - streamdetails.loudness
            gain_correct = round(gain_correct, 2)
            filter_params.append(f"volume={gain_correct}dB")
        streamdetails.volume_normalization_gain_correct = gain_correct

        first_chunk_received = False
        async for chunk in get_media_stream(
            self.mass,
            streamdetails=streamdetails,
            pcm_format=pcm_format,
            filter_params=filter_params,
        ):
            if not first_chunk_received:
                first_chunk_received = True
                # inform the queue that the track is now loaded in the buffer
                # so for example the next track can be enqueued
                self.mass.player_queues.track_loaded_in_buffer(
                    queue_item.queue_id, queue_item.queue_item_id
                )
            yield chunk
            del chunk

    async def get_queue_item_stream_with_smartfade(
        self,
        queue_item: QueueItem,
        pcm_format: AudioFormat,
        session_id: str | None = None,
        smart_fades_mode: SmartFadesMode = SmartFadesMode.SMART_FADES,
        standard_crossfade_duration: int = 10,
    ) -> AsyncGenerator[bytes, None]:
        """Get the audio stream for a single queue item with crossfade to the next item."""
        queue = self.mass.player_queues.get(queue_item.queue_id)
        if not queue:
            raise RuntimeError(f"Queue {queue_item.queue_id} not found")

        streamdetails = queue_item.streamdetails
        assert streamdetails
        crossfade_data = self._crossfade_data.get(queue.queue_id)

        self.logger.debug(
            "Start Streaming queue track: %s (%s) for queue %s - crossfade: %s",
            queue_item.streamdetails.uri if queue_item.streamdetails else "Unknown URI",
            queue_item.name,
            queue.display_name,
            smart_fades_mode,
        )

        if crossfade_data and crossfade_data.session_id != session_id:
            # invalidate expired crossfade data
            crossfade_data = None

        buffer = b""
        bytes_written = 0
        if smart_fades_mode == SmartFadesMode.SMART_FADES:
            crossfade_size = int(pcm_format.pcm_sample_size * SMART_CROSSFADE_DURATION)
        else:
            crossfade_size = int(pcm_format.pcm_sample_size * standard_crossfade_duration + 4)
        fade_out_data: bytes | None = None

        async for chunk in self.get_queue_item_stream(queue_item, pcm_format):
            # ALWAYS APPEND CHUNK TO BUFFER
            buffer += chunk
            del chunk
            if len(buffer) < crossfade_size:
                # buffer is not full enough, move on
                continue

            ####  HANDLE CROSSFADE DATA FROM PREVIOUS TRACK
            if crossfade_data:
                # discard the fade_in_part from the crossfade data
                buffer = buffer[crossfade_data.fade_in_size :]
                # send the (second half of the) crossfade data
                if crossfade_data.pcm_format != pcm_format:
                    # pcm format mismatch, we need to resample the crossfade data
                    async for _crossfade_chunk in resample_pcm_audio(
                        crossfade_data.data, crossfade_data.pcm_format, pcm_format
                    ):
                        yield _crossfade_chunk
                        bytes_written += len(_crossfade_chunk)
                        del _crossfade_chunk
                else:
                    yield crossfade_data.data
                    bytes_written += len(crossfade_data.data)
                # clear vars
                crossfade_data = None

            #### OTHER: enough data in buffer, feed to output
            while len(buffer) > crossfade_size:
                yield buffer[: pcm_format.pcm_sample_size]
                bytes_written += pcm_format.pcm_sample_size
                buffer = buffer[pcm_format.pcm_sample_size :]

        #### HANDLE END OF TRACK

        if not self._crossfade_allowed(
            queue_item, smart_fades_mode=smart_fades_mode, flow_mode=False
        ):
            # no crossfade enabled/allowed, just yield the buffer last part
            bytes_written += len(buffer)
            yield buffer
        else:
            # if crossfade is enabled, save fadeout part to pickup for next track
            fade_out_data = buffer[-crossfade_size:]
            remaining_bytes = buffer[:-crossfade_size]
            if remaining_bytes:
                yield remaining_bytes
                bytes_written += len(remaining_bytes)
            del remaining_bytes
            buffer = b""
            # get next track for crossfade
            try:
                next_queue_item = await self.mass.player_queues.load_next_queue_item(
                    queue.queue_id, queue_item.queue_item_id
                )
                async for chunk in self.get_queue_item_stream(next_queue_item, pcm_format):
                    # ALWAYS APPEND CHUNK TO BUFFER
                    buffer += chunk
                    del chunk
                    if len(buffer) < crossfade_size:
                        # buffer is not full enough, move on
                        continue
                    ####  HANDLE CROSSFADE OF PREVIOUS TRACK AND NEW TRACK
                    crossfade_data = await self._smart_fades_mixer.mix(
                        fade_in_part=buffer,
                        fade_out_part=fade_out_data,
                        fade_in_streamdetails=next_queue_item.streamdetails,
                        fade_out_streamdetails=queue_item.streamdetails,
                        pcm_format=pcm_format,
                        standard_crossfade_duration=standard_crossfade_duration,
                        mode=smart_fades_mode,
                    )
                    # send half of the crossfade_part (= approx the fadeout part)
                    crossfade_first, crossfade_second = (
                        crossfade_data[: len(crossfade_data) // 2 + len(crossfade_data) % 2],
                        crossfade_data[len(crossfade_data) // 2 + len(crossfade_data) % 2 :],
                    )
                    bytes_written += len(crossfade_first)
                    yield crossfade_first
                    del crossfade_first
                    # store the other half for the next track
                    self._crossfade_data[queue_item.queue_id] = CrossfadeData(
                        data=crossfade_second,
                        fade_in_size=len(buffer),
                        pcm_format=pcm_format,
                        queue_item_id=next_queue_item.queue_item_id,
                        session_id=session_id,
                    )
                    # clear vars and break out of loop
                    del crossfade_data
                    break
            except QueueEmpty:
                # end of queue reached or crossfade failed - no crossfade possible
                yield fade_out_data
                bytes_written += len(fade_out_data)
                del fade_out_data
        # make sure the buffer gets cleaned up
        del buffer
        # update duration details based on the actual pcm data we sent
        # this also accounts for crossfade and silence stripping
        seconds_streamed = bytes_written / pcm_format.pcm_sample_size
        streamdetails.seconds_streamed = seconds_streamed
        streamdetails.duration = streamdetails.seek_position + seconds_streamed
        self.logger.debug(
            "Finished Streaming queue track: %s (%s) on queue %s",
            queue_item.streamdetails.uri,
            queue_item.name,
            queue.display_name,
        )

    def _log_request(self, request: web.Request) -> None:
        """Log request."""
        if not self.logger.isEnabledFor(VERBOSE_LOG_LEVEL):
            return
        self.logger.log(
            VERBOSE_LOG_LEVEL,
            "Got %s request to %s from %s\nheaders: %s\n",
            request.method,
            request.path,
            request.remote,
            request.headers,
        )

    async def get_output_format(
        self,
        output_format_str: str,
        player: Player,
        content_sample_rate: int,
        content_bit_depth: int,
    ) -> AudioFormat:
        """Parse (player specific) output format details for given format string."""
        content_type: ContentType = ContentType.try_parse(output_format_str)
        supported_rates_conf: list[
            tuple[str, str]
        ] = await self.mass.config.get_player_config_value(
            player.player_id, CONF_SAMPLE_RATES, unpack_splitted_values=True
        )
        output_channels_str = self.mass.config.get_raw_player_config_value(
            player.player_id, CONF_OUTPUT_CHANNELS, "stereo"
        )
        supported_sample_rates: tuple[int] = tuple(int(x[0]) for x in supported_rates_conf)
        supported_bit_depths: tuple[int] = tuple(int(x[1]) for x in supported_rates_conf)

        player_max_bit_depth = max(supported_bit_depths)
        output_bit_depth = min(content_bit_depth, player_max_bit_depth)
        if content_sample_rate in supported_sample_rates:
            output_sample_rate = content_sample_rate
        else:
            output_sample_rate = max(supported_sample_rates)

        if not content_type.is_lossless():
            # no point in having a higher bit depth for lossy formats
            output_bit_depth = 16
            output_sample_rate = min(48000, output_sample_rate)
        if content_type == ContentType.WAV and output_bit_depth > 16:
            # WAV 24bit is not widely supported, fallback to 16bit
            output_bit_depth = 16
        if output_format_str == "pcm":
            content_type = ContentType.from_bit_depth(output_bit_depth)
        return AudioFormat(
            content_type=content_type,
            sample_rate=output_sample_rate,
            bit_depth=output_bit_depth,
            channels=1 if output_channels_str != "stereo" else 2,
        )

    async def _select_flow_format(
        self,
        player: Player,
    ) -> AudioFormat:
        """Parse (player specific) flow stream PCM format."""
        supported_rates_conf: list[
            tuple[str, str]
        ] = await self.mass.config.get_player_config_value(
            player.player_id, CONF_SAMPLE_RATES, unpack_splitted_values=True
        )
        supported_sample_rates: tuple[int] = tuple(int(x[0]) for x in supported_rates_conf)
        output_sample_rate = DEFAULT_PCM_FORMAT.sample_rate
        for sample_rate in (192000, 96000, 48000, 44100):
            if sample_rate in supported_sample_rates:
                output_sample_rate = sample_rate
                break
        return AudioFormat(
            content_type=DEFAULT_PCM_FORMAT.content_type,
            sample_rate=output_sample_rate,
            bit_depth=DEFAULT_PCM_FORMAT.bit_depth,
            channels=2,
        )

    def _crossfade_allowed(
        self, queue_item: QueueItem, smart_fades_mode: SmartFadesMode, flow_mode: bool = False
    ) -> bool:
        """Get the crossfade config for a queue item."""
        if smart_fades_mode == SmartFadesMode.DISABLED:
            return False
        if not (queue_player := self.mass.players.get(queue_item.queue_id)):
            return False  # just a guard
        if queue_item.media_type != MediaType.TRACK:
            self.logger.debug("Skipping crossfade: current item is not a track")
            return False
        # check if the next item is part of the same album
        next_item = self.mass.player_queues.get_next_item(
            queue_item.queue_id, queue_item.queue_item_id
        )
        if not next_item:
            return False
        # check if next item is a track
        if next_item.media_type != MediaType.TRACK:
            self.logger.debug("Skipping crossfade: next item is not a track")
            return False
        if (
            queue_item.media_type == MediaType.TRACK
            and next_item.media_type == MediaType.TRACK
            and queue_item.media_item
            and queue_item.media_item.album
            and next_item.media_item
            and next_item.media_item.album
            and queue_item.media_item.album == next_item.media_item.album
        ):
            # in general, crossfade is not desired for tracks of the same (gapless) album
            # because we have no accurate way to determine if the album is gapless or not,
            # for now we just never crossfade between tracks of the same album
            self.logger.debug("Skipping crossfade: next item is part of the same album")
            return False

        # check if next item sample rate matches
        if (
            not flow_mode
            and next_item.streamdetails
            and (
                queue_item.streamdetails.audio_format.sample_rate
                != next_item.streamdetails.audio_format.sample_rate
            )
            and (queue_player := self.mass.players.get(queue_item.queue_id))
            and PlayerFeature.GAPLESS_DIFFERENT_SAMPLERATE not in queue_player.supported_features
        ):
            self.logger.debug("Skipping crossfade: sample rate mismatch")
            return 0
        # all checks passed, crossfade is enabled/allowed
        return True
