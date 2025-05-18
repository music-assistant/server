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
import shutil
import urllib.parse
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
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
    CONF_ALLOW_AUDIO_CACHE,
    CONF_BIND_IP,
    CONF_BIND_PORT,
    CONF_CROSSFADE,
    CONF_CROSSFADE_DURATION,
    CONF_ENTRY_ENABLE_ICY_METADATA,
    CONF_HTTP_PROFILE,
    CONF_OUTPUT_CHANNELS,
    CONF_OUTPUT_CODEC,
    CONF_PUBLISH_IP,
    CONF_SAMPLE_RATES,
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
from music_assistant.helpers.audio import (
    CACHE_FILES_IN_USE,
    crossfade_pcm_parts,
    get_chunksize,
    get_media_stream,
    get_player_filter_params,
    get_silence,
    get_stream_details,
)
from music_assistant.helpers.audio import LOGGER as AUDIO_LOGGER
from music_assistant.helpers.ffmpeg import LOGGER as FFMPEG_LOGGER
from music_assistant.helpers.ffmpeg import check_ffmpeg_version, get_ffmpeg_stream
from music_assistant.helpers.util import (
    get_folder_size,
    get_free_space,
    get_free_space_percentage,
    get_ip_addresses,
    select_free_port,
    try_parse_bool,
)
from music_assistant.helpers.webserver import Webserver
from music_assistant.models.core_controller import CoreController
from music_assistant.models.plugin import PluginProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import CoreConfig
    from music_assistant_models.player import Player
    from music_assistant_models.player_queue import PlayerQueue
    from music_assistant_models.queue_item import QueueItem


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

    fadeout_part: bytes = b""
    pcm_format: AudioFormat = field(default_factory=AudioFormat)
    queue_item_id: str | None = None
    session_id: str | None = None


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
        # TEMP: remove old cache dir
        # remove after 2.5.0b15 or b16
        prev_cache_dir = os.path.join(self.mass.cache_path, ".audio")
        if os.path.isdir(prev_cache_dir):
            shutil.rmtree(prev_cache_dir)
        # prefer /tmp/.audio as audio cache dir
        self._audio_cache_dir = os.path.join("/tmp/.audio")  # noqa: S108
        self.allow_cache_default = "auto"
        self._crossfade_data: dict[str, CrossfadeData] = {}

    @property
    def base_url(self) -> str:
        """Return the base_url for the streamserver."""
        return self._server.base_url

    @property
    def audio_cache_dir(self) -> str:
        """Return the directory where (temporary) audio cache files are stored."""
        return self._audio_cache_dir

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
            ConfigEntry(
                key=CONF_ALLOW_AUDIO_CACHE,
                type=ConfigEntryType.STRING,
                default_value=self.allow_cache_default,
                options=[
                    ConfigValueOption("Always", "always"),
                    ConfigValueOption("Disabled", "disabled"),
                    ConfigValueOption("Auto", "auto"),
                ],
                label="Allow caching of remote/cloudbased audio streams",
                description="To ensure smooth(er) playback as well as fast seeking, "
                "Music Assistant can cache audio streams on disk. \n"
                "On systems with limited diskspace, this can be disabled, "
                "but may result in less smooth playback or slower seeking.\n\n"
                "**Always:** Enforce caching of audio streams at all times "
                "(as long as there is enough free space).\n"
                "**Disabled:** Never cache audio streams.\n"
                "**Auto:** Let Music Assistant decide if caching "
                "should be used on a per-item base.",
                category="advanced",
                required=True,
            ),
        )

    async def setup(self, config: CoreConfig) -> None:
        """Async initialize of module."""
        # copy log level to audio/ffmpeg loggers
        AUDIO_LOGGER.setLevel(self.logger.level)
        FFMPEG_LOGGER.setLevel(self.logger.level)
        # perform check for ffmpeg version
        await check_ffmpeg_version()
        if self.mass.running_as_hass_addon:
            # When running as HAOS add-on, we run /tmp as tmpfs so we need
            # to pick another temporary location which is not /tmp.
            # We prefer the root/user dir because it will be cleaned up on a reboot
            self._audio_cache_dir = os.path.join(os.path.expanduser("~"), ".audio")
        if not await asyncio.to_thread(os.path.isdir, self._audio_cache_dir):
            await asyncio.to_thread(os.makedirs, self._audio_cache_dir)
        # enable cache by default if we have enough free space only
        disk_percentage_free = await get_free_space_percentage(self._audio_cache_dir)
        self.allow_cache_default = "auto" if disk_percentage_free > 25 else "disabled"
        # schedule cleanup of old audio cache files
        await self._clean_audio_cache()
        # start the webserver
        self.publish_port = config.get_value(CONF_BIND_PORT)
        self.publish_ip = config.get_value(CONF_PUBLISH_IP)
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
            bind_ip=config.get_value(CONF_BIND_IP),
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

        # work out pcm format based on output format
        pcm_format = AudioFormat(
            content_type=DEFAULT_PCM_FORMAT.content_type,
            sample_rate=output_format.sample_rate,
            # always use f32 internally for extra headroom for filters etc
            bit_depth=DEFAULT_PCM_FORMAT.bit_depth,
            channels=2,
        )

        crossfade = await self.mass.config.get_player_config_value(queue.queue_id, CONF_CROSSFADE)
        if crossfade and PlayerFeature.GAPLESS_PLAYBACK not in queue_player.supported_features:
            # crossfade is not supported on this player due to missing gapless playback
            self.logger.warning(
                "Crossfade disabled: Player %s does not support gapless playback",
                queue_player.display_name,
            )
            crossfade = False

        if crossfade:
            # crossfade is enabled, use special crossfaded single item stream
            # where the crossfade of the next track is present in the stream of
            # a single track. This only works if the player supports gapless playback.
            audio_input = self.get_queue_item_stream_with_crossfade(
                queue_item=queue_item,
                pcm_format=pcm_format,
                session_id=session_id,
            )
        else:
            audio_input = self.get_queue_item_stream(
                queue_item=queue_item,
                pcm_format=pcm_format,
            )

        async for chunk in get_ffmpeg_stream(
            audio_input=audio_input,
            input_format=pcm_format,
            output_format=output_format,
            filter_params=get_player_filter_params(
                self.mass, queue_player.player_id, pcm_format, output_format
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
        http_profile: str = await self.mass.config.get_player_config_value(
            queue_id, CONF_HTTP_PROFILE
        )
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
        if player_id not in self.announcements:
            raise web.HTTPNotFound(reason=f"No pending announcements for Player: {player_id}")
        announcement_url = self.announcements[player_id]
        use_pre_announce = try_parse_bool(request.query.get("pre_announce"))

        # work out output format/details
        fmt = request.match_info.get("fmt", announcement_url.rsplit(".")[-1])
        audio_format = AudioFormat(content_type=ContentType.try_parse(fmt))

        http_profile: str = await self.mass.config.get_player_config_value(
            player_id, CONF_HTTP_PROFILE
        )
        if http_profile == "forced_content_length":
            # given the fact that an announcement is just a short audio clip,
            # just send it over completely at once so we have a fixed content length
            data = b""
            async for chunk in self.get_announcement_stream(
                announcement_url=announcement_url,
                output_format=audio_format,
                use_pre_announce=use_pre_announce,
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
            content_sample_rate=plugin_source.audio_format.sample_rate,
            content_bit_depth=plugin_source.audio_format.bit_depth,
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
        http_profile: str = await self.mass.config.get_player_config_value(
            player_id, CONF_HTTP_PROFILE
        )
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
        last_fadeout_part = b""
        queue.flow_mode = True
        if not start_queue_item:
            # this can happen in some (edge case) race conditions
            return
        pcm_sample_size = int(
            pcm_format.sample_rate * (pcm_format.bit_depth / 8) * pcm_format.channels
        )
        crossfade_enabled = await self.mass.config.get_player_config_value(
            queue.queue_id, CONF_CROSSFADE
        )
        if start_queue_item.media_type != MediaType.TRACK:
            # we only support crossfade for tracks, not for radio items
            crossfade_enabled = False
        crossfade_duration = self.mass.config.get_raw_player_config_value(
            queue.queue_id, CONF_CROSSFADE_DURATION, 10
        )
        self.logger.info(
            "Start Queue Flow stream for Queue %s - crossfade: %s",
            queue.display_name,
            f"{crossfade_duration}s" if crossfade_enabled else "disabled",
        )
        total_bytes_sent = 0

        while True:
            # get (next) queue item to stream
            if queue_track is None:
                queue_track = start_queue_item
            else:
                try:
                    queue_track = await self.mass.player_queues.preload_next_queue_item(
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

            # set some basic vars
            pcm_sample_size = int(pcm_format.sample_rate * (pcm_format.bit_depth / 8) * 2)
            crossfade_size = int(pcm_sample_size * crossfade_duration)
            bytes_written = 0
            buffer = b""
            # handle incoming audio chunks
            async for chunk in self.get_queue_item_stream(
                queue_track,
                pcm_format=pcm_format,
            ):
                # buffer size needs to be big enough to include the crossfade part
                req_buffer_size = pcm_sample_size if not crossfade_enabled else crossfade_size

                # ALWAYS APPEND CHUNK TO BUFFER
                buffer += chunk
                del chunk
                if len(buffer) < req_buffer_size:
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
                        pcm_format=pcm_format,
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
            if self._crossfade_allowed(queue_track, flow_mode=True):
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
            del last_fadeout_part
        total_bytes_sent += bytes_written
        self.logger.info("Finished Queue Flow stream for Queue %s", queue.display_name)

    async def get_announcement_stream(
        self,
        announcement_url: str,
        output_format: AudioFormat,
        use_pre_announce: bool = False,
    ) -> AsyncGenerator[bytes, None]:
        """Get the special announcement stream."""
        filter_params = ["loudnorm=I=-10:LRA=11:TP=-2"]

        if use_pre_announce:
            # Note: TTS URLs might take a while to load cause the actual data are often generated
            # asynchronously by the TTS provider. If we ask ffmpeg to mix the pre-announce, it will
            # wait until it reads the TTS data, so the whole stream will be delayed. It is much
            # faster to first play the pre-announce using a separate ffmpeg stream, and only
            # afterwards play the TTS itself.
            #
            # For this to be effective the player itself needs to be able to start playback fast.
            # If the returned stream is used as input to ffmpeg we should pass -probesize 8096.
            #
            # Finally, if the output_format is non-PCM, raw concatenation can be problematic.
            # So far players seem to tolerate this, but it might break some player in the future.

            async for chunk in get_ffmpeg_stream(
                audio_input=ANNOUNCE_ALERT_FILE,
                input_format=AudioFormat(content_type=ContentType.try_parse(ANNOUNCE_ALERT_FILE)),
                output_format=output_format,
                filter_params=filter_params,
            ):
                yield chunk

        # work out output format/details
        fmt = announcement_url.rsplit(".")[-1]
        audio_format = AudioFormat(content_type=ContentType.try_parse(fmt))
        extra_input_args = ["-probesize", "8096"]  # start the stream before reading all TTS input
        async for chunk in get_ffmpeg_stream(
            audio_input=announcement_url,
            input_format=audio_format,
            output_format=output_format,
            filter_params=filter_params,
            extra_input_args=extra_input_args,
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
        player.active_source = plugin_source_id
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
            player.active_source = player.player_id
            plugin_source.in_use_by = None

    async def get_queue_item_stream(
        self,
        queue_item: QueueItem,
        pcm_format: AudioFormat,
    ) -> AsyncGenerator[bytes, None]:
        """Get the audio stream for a single queue item as raw PCM audio."""
        # collect all arguments for ffmpeg
        streamdetails = queue_item.streamdetails
        assert streamdetails
        filter_params = []

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

        if streamdetails.media_type == MediaType.RADIO or not streamdetails.duration:
            # pad some silence before the radio/live stream starts to create some headroom
            # for radio stations (or other live streams) that do not provide any look ahead buffer
            # without this, some radio streams jitter a lot, especially with dynamic normalization,
            # if the stream does not provide a look ahead buffer
            async for silence in get_silence(4, pcm_format):
                yield silence
                del silence

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

    async def get_queue_item_stream_with_crossfade(
        self,
        queue_item: QueueItem,
        pcm_format: AudioFormat,
        session_id: str | None = None,
    ) -> AsyncGenerator[bytes, None]:
        """Get the audio stream for a single queue item with crossfade to the next item."""
        queue = self.mass.player_queues.get(queue_item.queue_id)
        streamdetails = queue_item.streamdetails
        assert streamdetails
        crossfade_duration = self.mass.config.get_raw_player_config_value(
            queue_item.queue_id, CONF_CROSSFADE_DURATION, 10
        )
        self._crossfade_data.setdefault(queue.queue_id, CrossfadeData())
        crossfade_data = self._crossfade_data[queue.queue_id]

        self.logger.debug(
            "Start Streaming queue track: %s (%s) for queue %s - crossfade: %s",
            queue_item.streamdetails.uri,
            queue_item.name,
            queue.display_name,
            f"{crossfade_duration} seconds",
        )

        if crossfade_data.session_id != session_id:
            # invalidate expired crossfade data
            crossfade_data.fadeout_part = b""

        buffer = b""
        bytes_written = 0
        pcm_sample_size = int(pcm_format.sample_rate * (pcm_format.bit_depth / 8) * 2)
        # buffer size needs to be big enough to include the crossfade part

        crossfade_size = int(pcm_sample_size * crossfade_duration)

        async for chunk in self.get_queue_item_stream(queue_item, pcm_format):
            # ALWAYS APPEND CHUNK TO BUFFER
            buffer += chunk
            del chunk
            if len(buffer) < crossfade_size:
                # buffer is not full enough, move on
                continue

            ####  HANDLE CROSSFADE OF PREVIOUS TRACK AND NEW TRACK
            if crossfade_data and crossfade_data.fadeout_part:
                # perform crossfade
                fade_in_part = buffer[:crossfade_size]
                remaining_bytes = buffer[crossfade_size:]
                crossfade_part = await crossfade_pcm_parts(
                    fade_in_part=fade_in_part,
                    fade_out_part=crossfade_data.fadeout_part,
                    pcm_format=pcm_format,
                    fade_out_pcm_format=crossfade_data.pcm_format,
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
                crossfade_data.fadeout_part = b""
                buffer = b""
                del fade_in_part

            #### OTHER: enough data in buffer, feed to output
            while len(buffer) > crossfade_size:
                yield buffer[:pcm_sample_size]
                bytes_written += pcm_sample_size
                buffer = buffer[pcm_sample_size:]

        #### HANDLE END OF TRACK
        if crossfade_data and crossfade_data.fadeout_part:
            # edge case: we did not get enough data to make the crossfade
            if crossfade_data.pcm_format == pcm_format:
                yield crossfade_data.fadeout_part
                bytes_written += len(crossfade_data.fadeout_part)
        # always reset fadeout part at this point
        crossfade_data.fadeout_part = b""
        if self._crossfade_allowed(queue_item, flow_mode=False):
            # if crossfade is enabled, save fadeout part to pickup for next track
            crossfade_data.fadeout_part = buffer[-crossfade_size:]
            crossfade_data.pcm_format = pcm_format
            crossfade_data.session_id = session_id
            crossfade_data.queue_item_id = queue_item.queue_item_id
            remaining_bytes = buffer[:-crossfade_size]
            if remaining_bytes:
                yield remaining_bytes
                bytes_written += len(remaining_bytes)
            del remaining_bytes
        elif buffer:
            # no crossfade enabled/allowed, just yield the buffer last part
            bytes_written += len(buffer)
            yield buffer
        # make sure the buffer gets cleaned up
        del buffer

        # update duration details based on the actual pcm data we sent
        # this also accounts for crossfade and silence stripping
        seconds_streamed = bytes_written / pcm_sample_size
        streamdetails.seconds_streamed = seconds_streamed
        streamdetails.duration = streamdetails.seek_position + seconds_streamed
        queue_item.duration = streamdetails.duration
        if queue_item.media_type:
            queue_item.media_item.duration = streamdetails.duration
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

    async def _clean_audio_cache(self) -> None:
        """Clean up audio cache periodically."""
        free_space_in_cache_dir = await get_free_space(self._audio_cache_dir)
        # calculate max cache size based on free space in cache dir
        max_cache_size = min(15, free_space_in_cache_dir * 0.2)
        cache_enabled = await self.mass.config.get_core_config_value(
            self.domain, CONF_ALLOW_AUDIO_CACHE
        )
        if cache_enabled == "disabled":
            max_cache_size = 0.001

        def _clean_old_files(foldersize: float):
            files: list[os.DirEntry] = [x for x in os.scandir(self._audio_cache_dir) if x.is_file()]
            files.sort(key=lambda x: x.stat().st_atime)
            for _file in files:
                if _file.path in CACHE_FILES_IN_USE:
                    continue
                foldersize -= _file.stat().st_size / float(1 << 30)
                os.remove(_file.path)
                if foldersize < max_cache_size:
                    return

        foldersize = await get_folder_size(self._audio_cache_dir)
        if foldersize > max_cache_size:
            await asyncio.to_thread(_clean_old_files, foldersize)
        # reschedule self
        self.mass.call_later(3600, self._clean_audio_cache)

    def _crossfade_allowed(self, queue_item: QueueItem, flow_mode: bool = False) -> bool:
        """Get the crossfade config for a queue item."""
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
