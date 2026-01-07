"""
Controller to stream audio to players.

The streams controller hosts a basic, unprotected HTTP-only webserver
purely to stream audio packets to players and some control endpoints such as
the upnp callbacks and json rpc api for slimproto clients.
"""

from __future__ import annotations

import asyncio
import gc
import logging
import os
import urllib.parse
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, cast

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
from music_assistant_models.errors import (
    AudioError,
    InvalidDataError,
    ProviderUnavailableError,
    QueueEmpty,
)
from music_assistant_models.media_items import AudioFormat, Track
from music_assistant_models.player_queue import PlayLogEntry

from music_assistant.constants import (
    ANNOUNCE_ALERT_FILE,
    CONF_BIND_IP,
    CONF_BIND_PORT,
    CONF_CROSSFADE_DURATION,
    CONF_ENTRY_ENABLE_ICY_METADATA,
    CONF_ENTRY_LOG_LEVEL,
    CONF_ENTRY_SUPPORT_GAPLESS_DIFFERENT_SAMPLE_RATES,
    CONF_ENTRY_ZEROCONF_INTERFACES,
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
    DEFAULT_STREAM_HEADERS,
    ICY_HEADERS,
    INTERNAL_PCM_FORMAT,
    SILENCE_FILE,
    VERBOSE_LOG_LEVEL,
)
from music_assistant.controllers.players.player_controller import AnnounceData
from music_assistant.controllers.streams.smart_fades import SmartFadesMixer
from music_assistant.controllers.streams.smart_fades.analyzer import SmartFadesAnalyzer
from music_assistant.controllers.streams.smart_fades.fades import SMART_CROSSFADE_DURATION
from music_assistant.helpers.audio import LOGGER as AUDIO_LOGGER
from music_assistant.helpers.audio import (
    get_buffered_media_stream,
    get_chunksize,
    get_media_stream,
    get_player_filter_params,
    get_stream_details,
    resample_pcm_audio,
)
from music_assistant.helpers.buffered_generator import buffered, use_buffer
from music_assistant.helpers.ffmpeg import LOGGER as FFMPEG_LOGGER
from music_assistant.helpers.ffmpeg import check_ffmpeg_version, get_ffmpeg_stream
from music_assistant.helpers.util import (
    divide_chunks,
    get_ip_addresses,
    get_total_system_memory,
    select_free_port,
)
from music_assistant.helpers.webserver import Webserver
from music_assistant.models.core_controller import CoreController
from music_assistant.models.music_provider import MusicProvider
from music_assistant.models.plugin import PluginProvider, PluginSource
from music_assistant.models.smart_fades import SmartFadesMode
from music_assistant.providers.universal_group.constants import UGP_PREFIX
from music_assistant.providers.universal_group.player import UniversalGroupPlayer

if TYPE_CHECKING:
    from music_assistant_models.config_entries import CoreConfig
    from music_assistant_models.player import PlayerMedia
    from music_assistant_models.player_queue import PlayerQueue
    from music_assistant_models.queue_item import QueueItem
    from music_assistant_models.streamdetails import StreamDetails

    from music_assistant.mass import MusicAssistant
    from music_assistant.models.player import Player


isfile = wrap(os.path.isfile)

CONF_ALLOW_BUFFER: Final[str] = "allow_buffering"
CONF_ALLOW_CROSSFADE_SAME_ALBUM: Final[str] = "allow_crossfade_same_album"
CONF_SMART_FADES_LOG_LEVEL: Final[str] = "smart_fades_log_level"

# Calculate total system memory once at module load time
TOTAL_SYSTEM_MEMORY_GB: Final[float] = get_total_system_memory()
CONF_ALLOW_BUFFER_DEFAULT = TOTAL_SYSTEM_MEMORY_GB >= 8.0


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
    pcm_format: AudioFormat  # Format of the 'data' bytes (current/previous track's format)
    fade_in_pcm_format: AudioFormat  # Format for 'fade_in_size' (next track's format)
    queue_item_id: str


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
        self._smart_fades_mixer = SmartFadesMixer(self)
        self._smart_fades_analyzer = SmartFadesAnalyzer(self)

    @property
    def base_url(self) -> str:
        """Return the base_url for the streamserver."""
        return self._server.base_url

    @property
    def bind_ip(self) -> str:
        """Return the IP address this streamserver is bound to."""
        return self._bind_ip

    @property
    def smart_fades_mixer(self) -> SmartFadesMixer:
        """Return the SmartFadesMixer instance."""
        return self._smart_fades_mixer

    @property
    def smart_fades_analyzer(self) -> SmartFadesAnalyzer:
        """Return the SmartFadesAnalyzer instance."""
        return self._smart_fades_analyzer

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
                key=CONF_ALLOW_BUFFER,
                type=ConfigEntryType.BOOLEAN,
                default_value=CONF_ALLOW_BUFFER_DEFAULT,
                label="Allow (in-memory) buffering of (track) audio",
                description="By default, Music Assistant tries to be as resource "
                "efficient as possible when streaming audio, especially considering "
                "low-end devices such as Raspberry Pi's. This means that audio "
                "buffering is disabled by default to reduce memory usage. \n\n"
                "Enabling this option allows for in-memory buffering of audio, "
                "which (massively) improves playback (and seeking) performance but it comes "
                "at the cost of increased memory usage. "
                "If you run Music Assistant on a capable device with enough memory, "
                "enabling this option is strongly recommended.",
                required=False,
                category="audio",
            ),
            ConfigEntry(
                key=CONF_VOLUME_NORMALIZATION_RADIO,
                type=ConfigEntryType.STRING,
                default_value=VolumeNormalizationMode.FALLBACK_FIXED_GAIN,
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
                key=CONF_ALLOW_CROSSFADE_SAME_ALBUM,
                type=ConfigEntryType.BOOLEAN,
                default_value=False,
                label="Allow crossfade between tracks from the same album",
                description="Enabling this option allows for crossfading between tracks "
                "that are part of the same album.",
                category="audio",
            ),
            ConfigEntry(
                key=CONF_PUBLISH_IP,
                type=ConfigEntryType.STRING,
                default_value=ip_addresses[0],
                label="Published IP address",
                description="This IP address is communicated to players where to find this server."
                "\nMake sure that this IP can be reached by players on the local network, "
                "otherwise audio streaming will not work.",
                required=False,
                category="advanced",
            ),
            ConfigEntry(
                key=CONF_BIND_PORT,
                type=ConfigEntryType.INTEGER,
                default_value=default_port,
                label="TCP Port",
                description="The TCP port to run the server. "
                "Make sure that this server can be reached "
                "on the given IP and TCP port by players on the local network.",
                category="advanced",
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
                key=CONF_SMART_FADES_LOG_LEVEL,
                type=ConfigEntryType.STRING,
                label="Smart Fades Log level",
                description="Log level for the Smart Fades mixer and analyzer.",
                options=CONF_ENTRY_LOG_LEVEL.options,
                default_value="GLOBAL",
                category="advanced",
            ),
            CONF_ENTRY_ZEROCONF_INTERFACES,
        )

    async def setup(self, config: CoreConfig) -> None:
        """Async initialize of module."""
        # copy log level to audio/ffmpeg loggers
        AUDIO_LOGGER.setLevel(self.logger.level)
        FFMPEG_LOGGER.setLevel(self.logger.level)
        self._setup_smart_fades_logger(config)
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
            bind_port=cast("int", self.publish_port),
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
        # Start periodic garbage collection task
        # This ensures memory from audio buffers and streams is cleaned up regularly
        self.mass.call_later(900, self._periodic_garbage_collection)  # 15 minutes

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
        conf_output_codec = await self.mass.config.get_player_config_value(
            player_id, CONF_OUTPUT_CODEC, default="flac", return_type=str
        )
        output_codec = ContentType.try_parse(conf_output_codec or "flac")
        fmt = output_codec.value
        # handle raw pcm without exact format specifiers
        if output_codec.is_pcm() and ";" not in fmt:
            fmt += f";codec=pcm;rate={44100};bitrate={16};channels={2}"
        base_path = "flow" if flow_mode else "single"
        return f"{self._server.base_url}/{base_path}/{session_id}/{queue_item.queue_id}/{queue_item.queue_item_id}.{fmt}"  # noqa: E501

    async def get_plugin_source_url(
        self,
        plugin_source: PluginSource,
        player_id: str,
    ) -> str:
        """Get the url for the Plugin Source stream/proxy."""
        if plugin_source.audio_format.content_type.is_pcm():
            fmt = ContentType.WAV.value
        else:
            fmt = plugin_source.audio_format.content_type.value
        return f"{self._server.base_url}/pluginsource/{plugin_source.id}/{player_id}.{fmt}"

    async def serve_queue_item_stream(self, request: web.Request) -> web.StreamResponse:
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
        if not queue_player:
            raise web.HTTPNotFound(reason=f"Unknown Player: {queue_id}")

        # work out pcm format based on streamdetails
        pcm_format = await self._select_pcm_format(
            player=queue_player,
            streamdetails=queue_item.streamdetails,
            smartfades_enabled=True,
        )
        output_format = await self.get_output_format(
            output_format_str=request.match_info["fmt"],
            player=queue_player,
            content_sample_rate=pcm_format.sample_rate,
            content_bit_depth=pcm_format.bit_depth,
        )

        # prepare request, add some DLNA/UPNP compatible headers
        headers = {
            **DEFAULT_STREAM_HEADERS,
            "icy-name": queue_item.name,
            "contentFeatures.dlna.org": "DLNA.ORG_OP=01;DLNA.ORG_FLAGS=01500000000000000000000000000000",  # noqa: E501
            "Accept-Ranges": "none",
            "Content-Type": f"audio/{output_format.output_format_str}",
        }
        resp = web.StreamResponse(
            status=200,
            reason="OK",
            headers=headers,
        )
        resp.content_type = f"audio/{output_format.output_format_str}"
        http_profile = await self.mass.config.get_player_config_value(
            queue_id, CONF_HTTP_PROFILE, default="default", return_type=str
        )
        if http_profile == "forced_content_length" and not queue_item.duration:
            # just set an insane high content length to make sure the player keeps playing
            resp.content_length = get_chunksize(output_format, 12 * 3600)
        elif http_profile == "forced_content_length" and queue_item.duration:
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
                queue.queue_id, CONF_SMART_FADES_MODE, return_type=SmartFadesMode
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
                "Crossfade disabled: Player %s does not support gapless playback, "
                "consider enabling flow mode to enable crossfade on this player.",
                queue_player.display_name if queue_player else "Unknown Player",
            )
            smart_fades_mode = SmartFadesMode.DISABLED

        if smart_fades_mode != SmartFadesMode.DISABLED:
            # crossfade is enabled, use special crossfaded single item stream
            # where the crossfade of the next track is present in the stream of
            # a single track. This only works if the player supports gapless playback!
            audio_input = self.get_queue_item_stream_with_smartfade(
                queue_item=queue_item,
                pcm_format=pcm_format,
                smart_fades_mode=smart_fades_mode,
                standard_crossfade_duration=standard_crossfade_duration,
            )
        else:
            # no crossfade, just a regular single item stream
            audio_input = self.get_queue_item_stream(
                queue_item=queue_item,
                pcm_format=pcm_format,
                seek_position=queue_item.streamdetails.seek_position,
            )
        # stream the audio
        # this final ffmpeg process in the chain will convert the raw, lossless PCM audio into
        # the desired output format for the player including any player specific filter params
        # such as channels mixing, DSP, resampling and, only if needed, encoding to lossy formats
        if queue_item.media_type == MediaType.RADIO:
            # keep very short buffer for radio streams
            # to keep them (more or less) realtime and prevent time outs
            read_rate_input_args = ["-readrate", "1.0", "-readrate_initial_burst", "2"]
        else:
            # just allow the player to buffer whatever it wants for single item streams
            read_rate_input_args = None

        first_chunk_received = False
        bytes_sent = 0
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
            extra_input_args=read_rate_input_args,
        ):
            try:
                await resp.write(chunk)
                bytes_sent += len(chunk)
                if not first_chunk_received:
                    first_chunk_received = True
                    # inform the queue that the track is now loaded in the buffer
                    # so for example the next track can be enqueued
                    self.mass.player_queues.track_loaded_in_buffer(
                        queue_item.queue_id, queue_item.queue_item_id
                    )
            except (BrokenPipeError, ConnectionResetError, ConnectionError) as err:
                if first_chunk_received and not queue_player.stop_called:
                    # Player disconnected (unexpected) after receiving at least some data
                    # This could indicate buffering issues, network problems,
                    # or player-specific issues
                    bytes_expected = get_chunksize(output_format, queue_item.duration or 3600)
                    self.logger.warning(
                        "Player %s disconnected prematurely from stream for %s (%s) - "
                        "error: %s, sent %d bytes, expected (approx) bytes=%d",
                        queue.display_name,
                        queue_item.name,
                        queue_item.uri,
                        err.__class__.__name__,
                        bytes_sent,
                        bytes_expected,
                    )
                break
        if queue_item.streamdetails.stream_error:
            self.logger.error(
                "Error streaming QueueItem %s (%s) to %s - will try to skip to next item",
                queue_item.name,
                queue_item.uri,
                queue.display_name,
            )
            # try to skip to the next item in the queue after a short delay
            self.mass.call_later(5, self.mass.player_queues.next(queue_id))
        return resp

    async def serve_queue_flow_stream(self, request: web.Request) -> web.StreamResponse:
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
            "contentFeatures.dlna.org": "DLNA.ORG_OP=01;DLNA.ORG_FLAGS=01700000000000000000000000000000",  # noqa: E501
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
        http_profile = await self.mass.config.get_player_config_value(
            queue_id, CONF_HTTP_PROFILE, default="default", return_type=str
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
            # we need to slowly feed the music to avoid the player stopping and later
            # restarting (or completely failing) the audio stream by keeping the buffer short.
            # this is reported to be an issue especially with Chromecast players.
            # see for example: https://github.com/music-assistant/support/issues/3717
            # allow buffer ahead of 6 seconds and read rest in realtime
            extra_input_args=["-readrate", "1.0", "-readrate_initial_burst", "6"],
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

    async def serve_command_request(self, request: web.Request) -> web.FileResponse:
        """Handle special 'command' request for a player."""
        self._log_request(request)
        queue_id = request.match_info["queue_id"]
        command = request.match_info["command"]
        if command == "next":
            self.mass.create_task(self.mass.player_queues.next(queue_id))
        return web.FileResponse(SILENCE_FILE, headers={"icy-name": "Music Assistant"})

    async def serve_announcement_stream(self, request: web.Request) -> web.StreamResponse:
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

        http_profile = await self.mass.config.get_player_config_value(
            player_id, CONF_HTTP_PROFILE, default="default", return_type=str
        )
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

    async def serve_plugin_source_stream(self, request: web.Request) -> web.StreamResponse:
        """Stream PluginSource audio to a player."""
        self._log_request(request)
        plugin_source_id = request.match_info["plugin_source"]
        provider = cast("PluginProvider", self.mass.get_provider(plugin_source_id))
        if not provider:
            raise ProviderUnavailableError(f"Unknown PluginSource: {plugin_source_id}")
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
            "contentFeatures.dlna.org": "DLNA.ORG_OP=01;DLNA.ORG_FLAGS=01700000000000000000000000000000",  # noqa: E501
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
        http_profile = await self.mass.config.get_player_config_value(
            player_id, CONF_HTTP_PROFILE, default="default", return_type=str
        )
        if http_profile == "forced_content_length":
            # just set an insanely high content length to make sure the player keeps playing
            resp.content_length = get_chunksize(output_format, 12 * 3600)
        elif http_profile == "chunked":
            resp.enable_chunked_encoding()

        await resp.prepare(request)

        # return early if this is not a GET request
        if request.method != "GET":
            return resp

        # all checks passed, start streaming!
        if not plugin_source.audio_format:
            raise InvalidDataError(f"No audio format for plugin source {plugin_source_id}")
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

    def get_stream(
        self, media: PlayerMedia, pcm_format: AudioFormat, force_flow_mode: bool = False
    ) -> AsyncGenerator[bytes, None]:
        """
        Get a stream of the given media as raw PCM audio.

        This is used as helper for player providers that can consume the raw PCM
        audio stream directly (e.g. AirPlay) and not rely on HTTP transport.
        """
        # select audio source
        if media.media_type == MediaType.ANNOUNCEMENT:
            # special case: stream announcement
            assert media.custom_data
            audio_source = self.get_announcement_stream(
                media.custom_data["announcement_url"],
                output_format=pcm_format,
                pre_announce=media.custom_data["pre_announce"],
                pre_announce_url=media.custom_data["pre_announce_url"],
            )
        elif media.media_type == MediaType.PLUGIN_SOURCE:
            # special case: plugin source stream
            assert media.custom_data
            audio_source = self.get_plugin_source_stream(
                plugin_source_id=media.custom_data["source_id"],
                output_format=pcm_format,
                # need to pass player_id from the PlayerMedia object
                # because this could have been a group
                player_id=media.custom_data["player_id"],
            )
        elif (
            media.media_type == MediaType.FLOW_STREAM
            and media.source_id
            and media.source_id.startswith(UGP_PREFIX)
            and media.uri
            and "/ugp/" in media.uri
        ):
            # special case: member player accessing UGP stream
            # Check URI to distinguish from the UGP accessing its own stream
            ugp_player = cast("UniversalGroupPlayer", self.mass.players.get(media.source_id))
            ugp_stream = ugp_player.stream
            assert ugp_stream is not None  # for type checker
            if ugp_stream.base_pcm_format == pcm_format:
                # no conversion needed
                audio_source = ugp_stream.subscribe_raw()
            else:
                audio_source = ugp_stream.get_stream(output_format=pcm_format)
        elif (
            media.source_id
            and media.queue_item_id
            and (media.media_type == MediaType.FLOW_STREAM or force_flow_mode)
        ):
            # regular queue (flow) stream request
            queue = self.mass.player_queues.get(media.source_id)
            assert queue
            start_queue_item = self.mass.player_queues.get_item(
                media.source_id, media.queue_item_id
            )
            assert start_queue_item
            audio_source = self.mass.streams.get_queue_flow_stream(
                queue=queue,
                start_queue_item=start_queue_item,
                pcm_format=pcm_format,
            )
        elif media.source_id and media.queue_item_id:
            # single item stream (e.g. radio)
            queue_item = self.mass.player_queues.get_item(media.source_id, media.queue_item_id)
            assert queue_item
            audio_source = buffered(
                self.get_queue_item_stream(
                    queue_item=queue_item,
                    pcm_format=pcm_format,
                ),
                buffer_size=10,
                min_buffer_before_yield=2,
            )
        else:
            # assume url or some other direct path
            # NOTE: this will fail if its an uri not playable by ffmpeg
            audio_source = get_ffmpeg_stream(
                audio_input=media.uri,
                input_format=AudioFormat(content_type=ContentType.try_parse(media.uri)),
                output_format=pcm_format,
            )
        return audio_source

    @use_buffer(buffer_size=30, min_buffer_before_yield=2)
    async def get_queue_flow_stream(
        self,
        queue: PlayerQueue,
        start_queue_item: QueueItem,
        pcm_format: AudioFormat,
    ) -> AsyncGenerator[bytes, None]:
        """
        Get a flow stream of all tracks in the queue as raw PCM audio.

        yields chunks of exactly 1 second of audio in the given pcm_format.
        """
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
        pcm_sample_size = pcm_format.pcm_sample_size
        if start_queue_item.media_type != MediaType.TRACK:
            # no crossfade on non-tracks
            smart_fades_mode = SmartFadesMode.DISABLED
            standard_crossfade_duration = 0
        else:
            smart_fades_mode = await self.mass.config.get_player_config_value(
                queue.queue_id, CONF_SMART_FADES_MODE, return_type=SmartFadesMode
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
        total_chunks_received = 0

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
                raise InvalidDataError(
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
            # calculate crossfade buffer size
            crossfade_buffer_duration = (
                SMART_CROSSFADE_DURATION
                if smart_fades_mode == SmartFadesMode.SMART_CROSSFADE
                else standard_crossfade_duration
            )
            crossfade_buffer_duration = min(
                crossfade_buffer_duration,
                int(queue_track.streamdetails.duration / 2)
                if queue_track.streamdetails.duration
                else crossfade_buffer_duration,
            )
            # Ensure crossfade buffer size is aligned to frame boundaries
            # Frame size = bytes_per_sample * channels
            bytes_per_sample = pcm_format.bit_depth // 8
            frame_size = bytes_per_sample * pcm_format.channels
            crossfade_buffer_size = int(pcm_format.pcm_sample_size * crossfade_buffer_duration)
            # Round down to nearest frame boundary
            crossfade_buffer_size = (crossfade_buffer_size // frame_size) * frame_size

            bytes_written = 0
            buffer = b""
            # handle incoming audio chunks
            first_chunk_received = False
            # buffer size needs to be big enough to include the crossfade part

            async for chunk in self.get_queue_item_stream(
                queue_track,
                pcm_format=pcm_format,
                seek_position=queue_track.streamdetails.seek_position,
                raise_on_error=False,
            ):
                total_chunks_received += 1
                if not first_chunk_received:
                    first_chunk_received = True
                    # inform the queue that the track is now loaded in the buffer
                    # so the next track can be preloaded
                    self.mass.player_queues.track_loaded_in_buffer(
                        queue.queue_id, queue_track.queue_item_id
                    )
                if total_chunks_received < 10 and smart_fades_mode != SmartFadesMode.DISABLED:
                    # we want a stream to start as quickly as possible
                    # so for the first 10 chunks we keep a very short buffer
                    req_buffer_size = pcm_format.pcm_sample_size
                else:
                    req_buffer_size = (
                        pcm_sample_size
                        if smart_fades_mode == SmartFadesMode.DISABLED
                        else crossfade_buffer_size
                    )

                # ALWAYS APPEND CHUNK TO BUFFER
                buffer += chunk
                del chunk
                if len(buffer) < req_buffer_size:
                    # buffer is not full enough, move on
                    # yield control to event loop with 10ms delay
                    await asyncio.sleep(0.01)
                    continue

                ####  HANDLE CROSSFADE OF PREVIOUS TRACK AND NEW TRACK
                if last_fadeout_part and last_streamdetails:
                    # perform crossfade
                    fadein_part = buffer[:crossfade_buffer_size]
                    remaining_bytes = buffer[crossfade_buffer_size:]
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
                    bytes_written += int(crossfade_part_len / 2)
                    if last_play_log_entry:
                        assert last_play_log_entry.seconds_streamed is not None
                        last_play_log_entry.seconds_streamed += (
                            crossfade_part_len / 2 / pcm_sample_size
                        )
                    # yield crossfade_part (in pcm_sample_size chunks)
                    for _chunk in divide_chunks(crossfade_part, pcm_sample_size):
                        yield _chunk
                        del _chunk
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
                for _chunk in divide_chunks(last_fadeout_part, pcm_sample_size):
                    yield _chunk
                    del _chunk
                bytes_written += len(last_fadeout_part)
                last_fadeout_part = b""
            if self._crossfade_allowed(
                queue_track, smart_fades_mode=smart_fades_mode, flow_mode=True
            ):
                # if crossfade is enabled, save fadeout part to pickup for next track
                last_fadeout_part = buffer[-crossfade_buffer_size:]
                last_streamdetails = queue_track.streamdetails
                last_play_log_entry = play_log_entry
                remaining_bytes = buffer[:-crossfade_buffer_size]
                if remaining_bytes:
                    yield remaining_bytes
                    bytes_written += len(remaining_bytes)
                del remaining_bytes
            elif buffer:
                # no crossfade enabled, just yield the buffer last part
                bytes_written += len(buffer)
                for _chunk in divide_chunks(buffer, pcm_sample_size):
                    yield _chunk
                    del _chunk
            # make sure the buffer gets cleaned up
            del buffer

            # update duration details based on the actual pcm data we sent
            # this also accounts for crossfade and silence stripping
            seconds_streamed = bytes_written / pcm_sample_size
            queue_track.streamdetails.seconds_streamed = seconds_streamed
            queue_track.streamdetails.duration = int(
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
            for _chunk in divide_chunks(last_fadeout_part, pcm_sample_size):
                yield _chunk
                del _chunk
            # correct seconds streamed/duration
            last_part_seconds = len(last_fadeout_part) / pcm_sample_size
            streamdetails = queue_track.streamdetails
            assert streamdetails is not None
            streamdetails.seconds_streamed = (
                streamdetails.seconds_streamed or 0
            ) + last_part_seconds
            streamdetails.duration = int((streamdetails.duration or 0) + last_part_seconds)
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
        announcement_data: asyncio.Queue[bytes | None] = asyncio.Queue(10)
        # we are doing announcement in PCM first to avoid multiple encodings
        # when mixing pre-announce and announcement
        # also we have to deal with some TTS sources being super slow in delivering audio
        # so we take an approach where we start fetching the announcement in the background
        # while we can already start playing the pre-announce sound (if any)

        pcm_format = (
            output_format
            if output_format.content_type.is_pcm()
            else AudioFormat(
                sample_rate=output_format.sample_rate,
                content_type=ContentType.PCM_S16LE,
                bit_depth=16,
                channels=output_format.channels,
            )
        )

        async def fetch_announcement() -> None:
            fmt = announcement_url.rsplit(".")[-1]
            async for chunk in get_ffmpeg_stream(
                audio_input=announcement_url,
                input_format=AudioFormat(content_type=ContentType.try_parse(fmt)),
                output_format=pcm_format,
                chunk_size=get_chunksize(pcm_format, 1),
            ):
                await announcement_data.put(chunk)
            await announcement_data.put(None)  # signal end of stream

        self.mass.create_task(fetch_announcement())

        async def _announcement_stream() -> AsyncGenerator[bytes, None]:
            """Generate the PCM audio stream for the announcement + optional pre-announce."""
            if pre_announce:
                async for chunk in get_ffmpeg_stream(
                    audio_input=pre_announce_url,
                    input_format=AudioFormat(content_type=ContentType.try_parse(pre_announce_url)),
                    output_format=pcm_format,
                    chunk_size=get_chunksize(pcm_format, 1),
                ):
                    yield chunk
            # pad silence while we're waiting for the announcement to be ready
            while announcement_data.empty():
                yield b"\0" * int(
                    pcm_format.sample_rate * (pcm_format.bit_depth / 8) * pcm_format.channels * 0.1
                )
                await asyncio.sleep(0.1)
            # stream announcement
            while True:
                announcement_chunk = await announcement_data.get()
                if announcement_chunk is None:
                    break
                yield announcement_chunk

        if output_format == pcm_format:
            # no need to re-encode, just yield the raw PCM stream
            async for chunk in _announcement_stream():
                yield chunk
            return

        # stream final announcement in requested output format
        async for chunk in get_ffmpeg_stream(
            audio_input=_announcement_stream(),
            input_format=pcm_format,
            output_format=output_format,
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
        plugin_prov = cast("PluginProvider", self.mass.get_provider(plugin_source_id))
        if not plugin_prov:
            raise ProviderUnavailableError(f"Unknown PluginSource: {plugin_source_id}")

        plugin_source = plugin_prov.get_source()
        self.logger.debug(
            "Start streaming PluginSource %s to %s using output format %s",
            plugin_source_id,
            player_id,
            output_format,
        )
        # this should already be set by the player controller, but just to be sure
        plugin_source.in_use_by = player_id

        try:
            async for chunk in get_ffmpeg_stream(
                audio_input=cast(
                    "str | AsyncGenerator[bytes, None]",
                    plugin_prov.get_audio_stream(player_id)
                    if plugin_source.stream_type == StreamType.CUSTOM
                    else plugin_source.path,
                ),
                input_format=plugin_source.audio_format,
                output_format=output_format,
                filter_params=player_filter_params,
                extra_input_args=["-y", "-re"],
            ):
                if plugin_source.in_use_by != player_id:
                    # another player took over or the stream ended, stop streaming
                    break
                yield chunk
        finally:
            self.logger.debug(
                "Finished streaming PluginSource %s to %s", plugin_source_id, player_id
            )
            await asyncio.sleep(1)  # prevent race conditions when selecting source
            if plugin_source.in_use_by == player_id:
                # release control
                plugin_source.in_use_by = None

    async def get_queue_item_stream(
        self,
        queue_item: QueueItem,
        pcm_format: AudioFormat,
        seek_position: int = 0,
        raise_on_error: bool = True,
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
            # apply user defined fixed volume/gain correction
            config_key = (
                CONF_VOLUME_NORMALIZATION_FIXED_GAIN_TRACKS
                if streamdetails.media_type == MediaType.TRACK
                else CONF_VOLUME_NORMALIZATION_FIXED_GAIN_RADIO
            )
            gain_value = await self.mass.config.get_core_config_value(
                self.domain, config_key, default=0.0, return_type=float
            )
            gain_correct = round(gain_value, 2)
            filter_params.append(f"volume={gain_correct}dB")
        elif streamdetails.volume_normalization_mode == VolumeNormalizationMode.MEASUREMENT_ONLY:
            # volume normalization with known loudness measurement
            # apply volume/gain correction
            target_loudness = (
                float(streamdetails.target_loudness)
                if streamdetails.target_loudness is not None
                else 0.0
            )
            if streamdetails.prefer_album_loudness and streamdetails.loudness_album is not None:
                gain_correct = target_loudness - float(streamdetails.loudness_album)
            elif streamdetails.loudness is not None:
                gain_correct = target_loudness - float(streamdetails.loudness)
            else:
                gain_correct = 0.0
            gain_correct = round(gain_correct, 2)
            filter_params.append(f"volume={gain_correct}dB")
        streamdetails.volume_normalization_gain_correct = gain_correct

        allow_buffer = bool(
            self.mass.config.get_raw_core_config_value(
                self.domain, CONF_ALLOW_BUFFER, CONF_ALLOW_BUFFER_DEFAULT
            )
            and streamdetails.duration
        )

        self.logger.debug(
            "Starting queue item stream for %s (%s)"
            " - using buffer: %s"
            " - using fade-in: %s"
            " - using volume normalization: %s",
            queue_item.name,
            streamdetails.uri,
            allow_buffer,
            streamdetails.fade_in,
            streamdetails.volume_normalization_mode,
        )
        if allow_buffer:
            media_stream_gen = get_buffered_media_stream(
                self.mass,
                streamdetails=streamdetails,
                pcm_format=pcm_format,
                seek_position=int(seek_position),
                filter_params=filter_params,
            )
        else:
            media_stream_gen = get_media_stream(
                self.mass,
                streamdetails=streamdetails,
                pcm_format=pcm_format,
                seek_position=int(seek_position),
                filter_params=filter_params,
            )

        first_chunk_received = False
        fade_in_buffer = b""
        bytes_received = 0
        finished = False
        stream_started_at = asyncio.get_event_loop().time()
        try:
            async for chunk in media_stream_gen:
                bytes_received += len(chunk)
                if not first_chunk_received:
                    first_chunk_received = True
                    self.logger.debug(
                        "First audio chunk received for %s (%s) after %.2f seconds",
                        queue_item.name,
                        streamdetails.uri,
                        asyncio.get_event_loop().time() - stream_started_at,
                    )
                # handle optional fade-in
                if streamdetails.fade_in:
                    if len(fade_in_buffer) < pcm_format.pcm_sample_size * 4:
                        fade_in_buffer += chunk
                    elif fade_in_buffer:
                        async for fade_chunk in get_ffmpeg_stream(
                            # NOTE: get_ffmpeg_stream signature says str | AsyncGenerator
                            # but FFMpeg class actually accepts bytes too. This works at
                            # runtime but needs type: ignore for mypy.
                            audio_input=fade_in_buffer + chunk,  # type: ignore[arg-type]
                            input_format=pcm_format,
                            output_format=pcm_format,
                            filter_params=["afade=type=in:start_time=0:duration=3"],
                        ):
                            yield fade_chunk
                    fade_in_buffer = b""
                    streamdetails.fade_in = False
                else:
                    yield chunk
                # help garbage collection by explicitly deleting chunk
                del chunk
            finished = True
        except AudioError as err:
            streamdetails.stream_error = True
            queue_item.available = False
            if raise_on_error:
                raise
            # yes, we swallow the error here after logging it
            # so the outer stream can handle it gracefully
            self.logger.error(
                "AudioError while streaming queue item %s (%s): %s",
                queue_item.name,
                streamdetails.uri,
                err,
            )
        finally:
            # determine how many seconds we've streamed
            # for pcm output we can calculate this easily
            seconds_streamed = bytes_received / pcm_format.pcm_sample_size
            streamdetails.seconds_streamed = seconds_streamed
            self.logger.debug(
                "stream %s for %s in %.2f seconds - seconds streamed/buffered: %.2f",
                "aborted" if not finished else "finished",
                streamdetails.uri,
                asyncio.get_event_loop().time() - stream_started_at,
                seconds_streamed,
            )
            # report stream to provider
            if (finished or seconds_streamed >= 90) and (
                music_prov := self.mass.get_provider(streamdetails.provider)
            ):
                if TYPE_CHECKING:  # avoid circular import
                    assert isinstance(music_prov, MusicProvider)
                self.mass.create_task(music_prov.on_streamed(streamdetails))

    @use_buffer(buffer_size=30, min_buffer_before_yield=2)
    async def get_queue_item_stream_with_smartfade(
        self,
        queue_item: QueueItem,
        pcm_format: AudioFormat,
        smart_fades_mode: SmartFadesMode = SmartFadesMode.SMART_CROSSFADE,
        standard_crossfade_duration: int = 10,
    ) -> AsyncGenerator[bytes, None]:
        """Get the audio stream for a single queue item with (smart) crossfade to the next item."""
        queue = self.mass.player_queues.get(queue_item.queue_id)
        if not queue:
            raise RuntimeError(f"Queue {queue_item.queue_id} not found")

        streamdetails = queue_item.streamdetails
        assert streamdetails
        crossfade_data = self._crossfade_data.pop(queue.queue_id, None)

        if crossfade_data and streamdetails.seek_position > 0:
            # don't do crossfade when seeking into track
            crossfade_data = None
        if crossfade_data and (crossfade_data.queue_item_id != queue_item.queue_item_id):
            # edge case alert: the next item changed just while we were preloading/crossfading
            self.logger.warning(
                "Skipping crossfade data for queue %s - next item changed!", queue.display_name
            )
            crossfade_data = None

        self.logger.debug(
            "Start Streaming queue track: %s (%s) for queue %s "
            "- crossfade mode: %s "
            "- crossfading from previous track: %s ",
            queue_item.streamdetails.uri if queue_item.streamdetails else "Unknown URI",
            queue_item.name,
            queue.display_name,
            smart_fades_mode,
            "true" if crossfade_data else "false",
        )

        buffer = b""
        bytes_written = 0
        # calculate crossfade buffer size
        crossfade_buffer_duration = (
            SMART_CROSSFADE_DURATION
            if smart_fades_mode == SmartFadesMode.SMART_CROSSFADE
            else standard_crossfade_duration
        )
        crossfade_buffer_duration = min(
            crossfade_buffer_duration,
            int(streamdetails.duration / 2)
            if streamdetails.duration
            else crossfade_buffer_duration,
        )
        # Ensure crossfade buffer size is aligned to frame boundaries
        # Frame size = bytes_per_sample * channels
        bytes_per_sample = pcm_format.bit_depth // 8
        frame_size = bytes_per_sample * pcm_format.channels
        crossfade_buffer_size = int(pcm_format.pcm_sample_size * crossfade_buffer_duration)
        # Round down to nearest frame boundary
        crossfade_buffer_size = (crossfade_buffer_size // frame_size) * frame_size
        fade_out_data: bytes | None = None

        if crossfade_data:
            # Calculate discard amount in seconds (format-independent)
            # Use fade_in_pcm_format because fade_in_size is in the next track's original format
            fade_in_duration_seconds = (
                crossfade_data.fade_in_size / crossfade_data.fade_in_pcm_format.pcm_sample_size
            )
            discard_seconds = int(fade_in_duration_seconds) - 1
            # Calculate discard amounts in CURRENT track's format
            discard_bytes = int(discard_seconds * pcm_format.pcm_sample_size)
            # Convert fade_in_size to current track's format for correct leftover calculation
            fade_in_size_in_current_format = int(
                fade_in_duration_seconds * pcm_format.pcm_sample_size
            )
            discard_leftover = fade_in_size_in_current_format - discard_bytes
        else:
            discard_seconds = streamdetails.seek_position
            discard_leftover = 0
        total_chunks_received = 0
        req_buffer_size = crossfade_buffer_size
        async for chunk in self.get_queue_item_stream(
            queue_item, pcm_format, seek_position=discard_seconds
        ):
            total_chunks_received += 1
            if discard_leftover:
                # discard leftover bytes from crossfade data
                chunk = chunk[discard_leftover:]  # noqa: PLW2901
                discard_leftover = 0

            if total_chunks_received < 10:
                # we want a stream to start as quickly as possible
                # so for the first 10 chunks we keep a very short buffer
                req_buffer_size = pcm_format.pcm_sample_size
            else:
                req_buffer_size = crossfade_buffer_size

            # ALWAYS APPEND CHUNK TO BUFFER
            buffer += chunk
            del chunk
            if len(buffer) < req_buffer_size:
                # buffer is not full enough, move on
                continue

            ####  HANDLE CROSSFADE DATA FROM PREVIOUS TRACK
            if crossfade_data:
                # send the (second half of the) crossfade data
                if crossfade_data.pcm_format != pcm_format:
                    # edge case: pcm format mismatch, we need to resample
                    self.logger.debug(
                        "Resampling crossfade data from %s to %s for queue %s",
                        crossfade_data.pcm_format.sample_rate,
                        pcm_format.sample_rate,
                        queue.display_name,
                    )
                    resampled_data = await resample_pcm_audio(
                        crossfade_data.data,
                        crossfade_data.pcm_format,
                        pcm_format,
                    )
                    if resampled_data:
                        for _chunk in divide_chunks(resampled_data, pcm_format.pcm_sample_size):
                            yield _chunk
                        bytes_written += len(resampled_data)
                    else:
                        # Resampling failed, error already logged in resample_pcm_audio
                        # Skip crossfade data entirely - stream continues without it
                        self.logger.warning(
                            "Skipping crossfade data for queue %s due to resampling failure",
                            queue.display_name,
                        )
                else:
                    for _chunk in divide_chunks(crossfade_data.data, pcm_format.pcm_sample_size):
                        yield _chunk
                    bytes_written += len(crossfade_data.data)
                # clear vars
                crossfade_data = None

            #### OTHER: enough data in buffer, feed to output
            while len(buffer) > req_buffer_size:
                yield buffer[: pcm_format.pcm_sample_size]
                bytes_written += pcm_format.pcm_sample_size
                buffer = buffer[pcm_format.pcm_sample_size :]

        #### HANDLE END OF TRACK

        if crossfade_data:
            # edge case: we did not get enough data to send the crossfade data
            # send the (second half of the) crossfade data
            if crossfade_data.pcm_format != pcm_format:
                # (yet another) edge case: pcm format mismatch, we need to resample
                self.logger.debug(
                    "Resampling remaining crossfade data from %s to %s for queue %s",
                    crossfade_data.pcm_format.sample_rate,
                    pcm_format.sample_rate,
                    queue.display_name,
                )
                resampled_crossfade_data = await resample_pcm_audio(
                    crossfade_data.data,
                    crossfade_data.pcm_format,
                    pcm_format,
                )
                if resampled_crossfade_data:
                    crossfade_data.data = resampled_crossfade_data
                else:
                    # Resampling failed, error already logged in resample_pcm_audio
                    # Skip the crossfade data entirely
                    self.logger.warning(
                        "Skipping remaining crossfade data for queue %s due to resampling failure",
                        queue.display_name,
                    )
                    crossfade_data = None
            if crossfade_data:
                for _chunk in divide_chunks(crossfade_data.data, pcm_format.pcm_sample_size):
                    yield _chunk
                bytes_written += len(crossfade_data.data)
                crossfade_data = None

        # get next track for crossfade
        next_queue_item: QueueItem | None
        try:
            self.logger.debug(
                "Preloading NEXT track for crossfade for queue %s",
                queue.display_name,
            )
            next_queue_item = await self.mass.player_queues.load_next_queue_item(
                queue.queue_id, queue_item.queue_item_id
            )
            # set index_in_buffer to prevent our next track is overwritten while preloading
            if next_queue_item.streamdetails is None:
                raise InvalidDataError(
                    f"No streamdetails for next queue item {next_queue_item.queue_item_id}"
                )
            queue.index_in_buffer = self.mass.player_queues.index_by_id(
                queue.queue_id, next_queue_item.queue_item_id
            )
            queue_player = self.mass.players.get(queue.queue_id)
            assert queue_player is not None
            next_queue_item_pcm_format = await self._select_pcm_format(
                player=queue_player,
                streamdetails=next_queue_item.streamdetails,
                smartfades_enabled=True,
            )
        except QueueEmpty:
            # end of queue reached, no next item
            next_queue_item = None

        if not next_queue_item or not self._crossfade_allowed(
            queue_item,
            smart_fades_mode=smart_fades_mode,
            flow_mode=False,
            next_queue_item=next_queue_item,
            sample_rate=pcm_format.sample_rate,
            next_sample_rate=next_queue_item_pcm_format.sample_rate,
        ):
            # no crossfade enabled/allowed, just yield the buffer last part
            bytes_written += len(buffer)
            for _chunk in divide_chunks(buffer, pcm_format.pcm_sample_size):
                yield _chunk
        else:
            # if crossfade is enabled, save fadeout part in buffer to pickup for next track
            fade_out_data = buffer
            buffer = b""
            try:
                async for chunk in self.get_queue_item_stream(
                    next_queue_item, next_queue_item_pcm_format
                ):
                    # append to buffer until we reach crossfade size
                    # we only need the first X seconds of the NEXT track so we can
                    # perform the crossfade.
                    # the crossfaded audio of the previous and next track will be
                    # sent in two equal parts: first half now, second half
                    # when the next track starts. We use CrossfadeData to store
                    # the second half to be picked up by the next track's stream generator.
                    # Note that we more or less expect the user to have enabled the in-memory
                    # buffer so we can keep the next track's audio data in memory.
                    buffer += chunk
                    del chunk
                    if len(buffer) >= crossfade_buffer_size:
                        break
                ####  HANDLE CROSSFADE OF PREVIOUS TRACK AND NEW TRACK
                # Store original buffer size before any resampling for fade_in_size calculation
                # This size is in the next track's original format which is what we need
                original_buffer_size = len(buffer)
                if next_queue_item_pcm_format != pcm_format:
                    # edge case: pcm format mismatch, we need to resample the next track's
                    # beginning part before crossfading
                    self.logger.debug(
                        "Resampling next track's crossfade from %s to %s for queue %s",
                        next_queue_item_pcm_format.sample_rate,
                        pcm_format.sample_rate,
                        queue.display_name,
                    )
                    buffer = await resample_pcm_audio(
                        buffer,
                        next_queue_item_pcm_format,
                        pcm_format,
                    )
                # perform actual (smart fades) crossfade using mixer
                crossfade_bytes = await self._smart_fades_mixer.mix(
                    fade_in_part=buffer,
                    fade_out_part=fade_out_data,
                    fade_in_streamdetails=cast("StreamDetails", next_queue_item.streamdetails),
                    fade_out_streamdetails=streamdetails,
                    pcm_format=pcm_format,
                    standard_crossfade_duration=standard_crossfade_duration,
                    mode=smart_fades_mode,
                )
                # send half of the crossfade_part (= approx the fadeout part)
                split_point = (len(crossfade_bytes) + 1) // 2
                crossfade_first = crossfade_bytes[:split_point]
                crossfade_second = crossfade_bytes[split_point:]
                del crossfade_bytes
                bytes_written += len(crossfade_first)
                for _chunk in divide_chunks(crossfade_first, pcm_format.pcm_sample_size):
                    yield _chunk
                # store the other half for the next track
                # IMPORTANT: crossfade_second data is in CURRENT track's format (pcm_format)
                # because it was created from the resampled buffer used for mixing.
                # BUT fade_in_size represents bytes in NEXT track's original format
                # (next_queue_item_pcm_format) because that's how much of the next track
                # was consumed during the crossfade. We need both formats to correctly
                # handle the crossfade data when the next track starts.
                self._crossfade_data[queue_item.queue_id] = CrossfadeData(
                    data=crossfade_second,
                    fade_in_size=original_buffer_size,
                    pcm_format=pcm_format,  # Format of the data (current track)
                    fade_in_pcm_format=next_queue_item_pcm_format,  # Format for fade_in_size
                    queue_item_id=next_queue_item.queue_item_id,
                )
            except AudioError:
                # no crossfade possible, just yield the fade_out_data
                next_queue_item = None
                yield fade_out_data
                bytes_written += len(fade_out_data)
                del fade_out_data
        # make sure the buffer gets cleaned up
        del buffer
        # update duration details based on the actual pcm data we sent
        # this also accounts for crossfade and silence stripping
        seconds_streamed = bytes_written / pcm_format.pcm_sample_size
        streamdetails.seconds_streamed = seconds_streamed
        streamdetails.duration = int(streamdetails.seek_position + seconds_streamed)
        self.logger.debug(
            "Finished Streaming queue track: %s (%s) on queue %s "
            "- crossfade data prepared for next track: %s",
            streamdetails.uri,
            queue_item.name,
            queue.display_name,
            next_queue_item.name if next_queue_item else "N/A",
        )

    def _log_request(self, request: web.Request) -> None:
        """Log request."""
        if self.logger.isEnabledFor(VERBOSE_LOG_LEVEL):
            self.logger.log(
                VERBOSE_LOG_LEVEL,
                "Got %s request to %s from %s\nheaders: %s\n",
                request.method,
                request.path,
                request.remote,
                request.headers,
            )
        else:
            self.logger.debug(
                "Got %s request to %s from %s",
                request.method,
                request.path,
                request.remote,
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
        supported_rates_conf = cast(
            "list[tuple[str, str]]",
            await self.mass.config.get_player_config_value(
                player.player_id, CONF_SAMPLE_RATES, unpack_splitted_values=True
            ),
        )
        output_channels_str = self.mass.config.get_raw_player_config_value(
            player.player_id, CONF_OUTPUT_CHANNELS, "stereo"
        )
        supported_sample_rates = tuple(int(x[0]) for x in supported_rates_conf)
        supported_bit_depths = tuple(int(x[1]) for x in supported_rates_conf)

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
        supported_rates_conf = cast(
            "list[tuple[str, str]]",
            await self.mass.config.get_player_config_value(
                player.player_id, CONF_SAMPLE_RATES, unpack_splitted_values=True
            ),
        )
        supported_sample_rates = tuple(int(x[0]) for x in supported_rates_conf)
        output_sample_rate = INTERNAL_PCM_FORMAT.sample_rate
        for sample_rate in (192000, 96000, 48000, 44100):
            if sample_rate in supported_sample_rates:
                output_sample_rate = sample_rate
                break
        return AudioFormat(
            content_type=INTERNAL_PCM_FORMAT.content_type,
            sample_rate=output_sample_rate,
            bit_depth=INTERNAL_PCM_FORMAT.bit_depth,
            channels=2,
        )

    async def _select_pcm_format(
        self,
        player: Player,
        streamdetails: StreamDetails,
        smartfades_enabled: bool,
    ) -> AudioFormat:
        """Parse (player specific) stream internal PCM format."""
        supported_rates_conf = cast(
            "list[tuple[str, str]]",
            await self.mass.config.get_player_config_value(
                player.player_id, CONF_SAMPLE_RATES, unpack_splitted_values=True
            ),
        )
        supported_sample_rates = tuple(int(x[0]) for x in supported_rates_conf)
        # use highest supported rate within content rate
        output_sample_rate = max(
            (r for r in supported_sample_rates if r <= streamdetails.audio_format.sample_rate),
            default=48000,  # sane/safe default
        )
        # work out pcm format based on streamdetails
        pcm_format = AudioFormat(
            sample_rate=output_sample_rate,
            # always use f32 internally for extra headroom for filters etc
            content_type=INTERNAL_PCM_FORMAT.content_type,
            bit_depth=INTERNAL_PCM_FORMAT.bit_depth,
            channels=streamdetails.audio_format.channels,
        )
        if smartfades_enabled:
            pcm_format.channels = 2  # force stereo for crossfading

        return pcm_format

    def _crossfade_allowed(
        self,
        queue_item: QueueItem,
        smart_fades_mode: SmartFadesMode,
        flow_mode: bool = False,
        next_queue_item: QueueItem | None = None,
        sample_rate: int | None = None,
        next_sample_rate: int | None = None,
    ) -> bool:
        """Get the crossfade config for a queue item."""
        if smart_fades_mode == SmartFadesMode.DISABLED:
            return False
        if not (self.mass.players.get(queue_item.queue_id)):
            return False  # just a guard
        if queue_item.media_type != MediaType.TRACK:
            self.logger.debug("Skipping crossfade: current item is not a track")
            return False
        # check if the next item is part of the same album
        next_item = next_queue_item or self.mass.player_queues.get_next_item(
            queue_item.queue_id, queue_item.queue_item_id
        )
        if not next_item:
            # there is no next item!
            return False
        # check if next item is a track
        if next_item.media_type != MediaType.TRACK:
            self.logger.debug("Skipping crossfade: next item is not a track")
            return False
        if (
            isinstance(queue_item.media_item, Track)
            and isinstance(next_item.media_item, Track)
            and queue_item.media_item.album
            and next_item.media_item.album
            and queue_item.media_item.album == next_item.media_item.album
            and not self.mass.config.get_raw_core_config_value(
                self.domain, CONF_ALLOW_CROSSFADE_SAME_ALBUM, False
            )
        ):
            # in general, crossfade is not desired for tracks of the same (gapless) album
            # because we have no accurate way to determine if the album is gapless or not,
            # for now we just never crossfade between tracks of the same album
            self.logger.debug("Skipping crossfade: next item is part of the same album")
            return False

        # check if we're allowed to crossfade on different sample rates
        if (
            not flow_mode
            and sample_rate
            and next_sample_rate
            and sample_rate != next_sample_rate
            and not self.mass.config.get_raw_player_config_value(
                queue_item.queue_id,
                CONF_ENTRY_SUPPORT_GAPLESS_DIFFERENT_SAMPLE_RATES.key,
                CONF_ENTRY_SUPPORT_GAPLESS_DIFFERENT_SAMPLE_RATES.default_value,
            )
        ):
            self.logger.debug(
                "Skipping crossfade: player does not support gapless playback "
                "with different sample rates (%s vs %s)",
                sample_rate,
                next_sample_rate,
            )
            return False

        return True

    async def _periodic_garbage_collection(self) -> None:
        """Periodic garbage collection to free up memory from audio buffers and streams."""
        self.logger.log(
            VERBOSE_LOG_LEVEL,
            "Running periodic garbage collection...",
        )
        # Run garbage collection in executor to avoid blocking the event loop
        # Since this runs periodically (not in response to subprocess cleanup),
        # it's safe to run in a thread without causing thread-safety issues
        loop = asyncio.get_running_loop()
        collected = await loop.run_in_executor(None, gc.collect)
        self.logger.log(
            VERBOSE_LOG_LEVEL,
            "Garbage collection completed, collected %d objects",
            collected,
        )
        # Schedule next run in 15 minutes
        self.mass.call_later(900, self._periodic_garbage_collection)

    def _setup_smart_fades_logger(self, config: CoreConfig) -> None:
        """Set up smart fades logger level."""
        log_level = str(config.get_value(CONF_SMART_FADES_LOG_LEVEL))
        if log_level == "GLOBAL":
            self.smart_fades_analyzer.logger.setLevel(self.logger.level)
            self.smart_fades_mixer.logger.setLevel(self.logger.level)
        else:
            self.smart_fades_analyzer.logger.setLevel(log_level)
            self.smart_fades_mixer.logger.setLevel(log_level)
