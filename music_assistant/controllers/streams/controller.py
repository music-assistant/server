"""
Controller to stream audio to players.

The streams controller hosts a basic, unprotected HTTP-only webserver
purely to stream audio packets to players.
"""

from __future__ import annotations

import asyncio
import gc
import logging
import os
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, cast

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
from music_assistant_models.errors import AudioError, InvalidDataError, ProviderUnavailableError
from music_assistant_models.media_items import AudioFormat

from music_assistant.constants import (
    ANNOUNCE_ALERT_FILE,
    CONF_BACKGROUND_SCAN_CONCURRENCY,
    CONF_BIND_IP,
    CONF_BIND_PORT,
    CONF_CROSSFADE_DURATION,
    CONF_ENTRY_ENABLE_ICY_METADATA,
    CONF_ENTRY_LOG_LEVEL,
    CONF_HTTP_PROFILE,
    CONF_OUTPUT_CODEC,
    CONF_PUBLISH_IP,
    CONF_SMART_FADES_MODE,
    CONF_VOLUME_NORMALIZATION_FIXED_GAIN_RADIO,
    CONF_VOLUME_NORMALIZATION_FIXED_GAIN_TRACKS,
    CONF_VOLUME_NORMALIZATION_RADIO,
    CONF_VOLUME_NORMALIZATION_TRACKS,
    DEFAULT_BACKGROUND_SCAN_CONCURRENCY,
    DEFAULT_STREAM_HEADERS,
    DLNA_CONTENT_FEATURES,
    DLNA_CONTENT_FEATURES_REALTIME,
    ICY_HEADERS,
    SILENCE_FILE,
    VERBOSE_LOG_LEVEL,
)
from music_assistant.controllers.players.helpers import AnnounceData
from music_assistant.controllers.streams.audio import StreamsAudio
from music_assistant.controllers.streams.audio_analysis import AudioAnalysisController
from music_assistant.controllers.streams.constants import (
    CONF_ALLOW_CROSSFADE_SAME_ALBUM,
    CONF_BUFFER_SIZE,
    CONF_BUFFER_SIZE_DEFAULT,
    CONF_SMART_FADES_LOG_LEVEL,
    DEFAULT_PORT,
    BufferSize,
)
from music_assistant.helpers.audio import (
    calculate_content_length,
    get_content_length,
    get_mime_type,
    store_content_length_in_cache,
)
from music_assistant.helpers.buffered_generator import buffered
from music_assistant.helpers.ffmpeg import LOGGER as FFMPEG_LOGGER
from music_assistant.helpers.ffmpeg import check_ffmpeg_version, get_ffmpeg_stream
from music_assistant.helpers.util import format_ip_for_url, get_ip_addresses
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

    from music_assistant.mass import MusicAssistant


isfile = wrap(os.path.isfile)


class StreamsController(CoreController):
    """Controller to stream audio to players."""

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
        self._bind_ip: str = "0.0.0.0"
        self.audio = StreamsAudio(mass)
        self._audio_analysis = AudioAnalysisController(self)

    @property
    def audio_analysis(self) -> AudioAnalysisController:
        """Return the AudioAnalysisController instance."""
        return self._audio_analysis

    @property
    def base_url(self) -> str:
        """Return the base_url for the streamserver."""
        return self._server.base_url

    @property
    def bind_ip(self) -> str:
        """Return the IP address this streamserver is bound to."""
        return self._bind_ip

    async def get_config_entries(
        self, action: str | None = None, values: dict[str, ConfigValueType] | None = None
    ) -> tuple[ConfigEntry, ...]:
        """Return all Config Entries for this core module (if any)."""
        ip_addresses = await get_ip_addresses(include_ipv6=True)
        return (
            ConfigEntry(
                key=CONF_BUFFER_SIZE,
                type=ConfigEntryType.STRING,
                default_value=CONF_BUFFER_SIZE_DEFAULT,
                label="Audio buffer size",
                description="Controls how much audio is buffered in memory. "
                "A larger buffer improves playback stability and seeking "
                "but uses more memory.\n\n"
                "- **Minimal**: Small buffer, "
                "recommended for memory-constrained devices.\n"
                "- **Balanced**: Moderate buffer, "
                "good balance for most systems.\n"
                "- **Maximum**: Large buffer, "
                "best performance for systems with plenty of memory.",
                options=[
                    ConfigValueOption("Minimal", BufferSize.MINIMAL.value),
                    ConfigValueOption("Balanced", BufferSize.BALANCED.value),
                    ConfigValueOption("Maximum", BufferSize.MAXIMUM.value),
                ],
                required=False,
                category="playback",
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
                category="playback",
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
                category="playback",
            ),
            ConfigEntry(
                key=CONF_VOLUME_NORMALIZATION_FIXED_GAIN_RADIO,
                type=ConfigEntryType.FLOAT,
                range=(-20, 10),
                default_value=-6,
                label="Fixed/fallback gain adjustment for radio streams",
                category="playback",
            ),
            ConfigEntry(
                key=CONF_VOLUME_NORMALIZATION_FIXED_GAIN_TRACKS,
                type=ConfigEntryType.FLOAT,
                range=(-20, 10),
                default_value=-6,
                label="Fixed/fallback gain adjustment for tracks",
                category="playback",
            ),
            ConfigEntry(
                key=CONF_ALLOW_CROSSFADE_SAME_ALBUM,
                type=ConfigEntryType.BOOLEAN,
                default_value=False,
                label="Allow crossfade between tracks from the same album",
                description="Enabling this option allows for crossfading between tracks "
                "that are part of the same album.",
                category="playback",
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
                category="generic",
                advanced=True,
                requires_reload=True,
            ),
            ConfigEntry(
                key=CONF_BIND_PORT,
                type=ConfigEntryType.INTEGER,
                default_value=DEFAULT_PORT,
                label="TCP Port",
                description="The TCP port to run the server. "
                "Make sure that this server can be reached "
                "on the given IP and TCP port by players on the local network.",
                category="generic",
                advanced=True,
                requires_reload=True,
            ),
            ConfigEntry(
                key=CONF_BIND_IP,
                type=ConfigEntryType.STRING,
                default_value="0.0.0.0",
                options=[ConfigValueOption(x, x) for x in {"0.0.0.0", *ip_addresses}],
                label="Bind to IP/interface",
                description="Start the stream server on this specific interface. \n"
                "Use 0.0.0.0 to bind to all interfaces (both IPv4 and IPv6), "
                "which is the default. \n"
                "This is an advanced setting that should normally "
                "not be adjusted in regular setups.",
                category="generic",
                advanced=True,
                required=False,
                requires_reload=True,
            ),
            ConfigEntry(
                key=CONF_SMART_FADES_LOG_LEVEL,
                type=ConfigEntryType.STRING,
                label="Smart Fades Log level",
                description="Log level for the Smart Fades mixer and analyzer.",
                options=CONF_ENTRY_LOG_LEVEL.options,
                default_value="GLOBAL",
                category="audio_analysis",
                advanced=True,
            ),
            ConfigEntry(
                key=CONF_BACKGROUND_SCAN_CONCURRENCY,
                type=ConfigEntryType.INTEGER,
                range=(1, 8),
                default_value=DEFAULT_BACKGROUND_SCAN_CONCURRENCY,
                label="Background analysis concurrency",
                description="Maximum number of tracks analyzed concurrently during the nightly "
                "background scan. Default 1 (serial). Increase only if your hardware can handle "
                "concurrent torch/ffmpeg work.",
                category="audio_analysis",
            ),
        )

    async def setup(self, config: CoreConfig) -> None:
        """Async initialize of module."""
        # initialize the audio sub-controller (needs mass.streams to be set)
        self.audio.setup()
        self._audio_analysis.setup()
        # copy log level to audio/ffmpeg loggers
        self.audio.logger.setLevel(self.logger.level)
        FFMPEG_LOGGER.setLevel(self.logger.level)
        self._setup_smart_fades_logger(config)
        # perform check for ffmpeg version
        await check_ffmpeg_version()
        # start the webserver
        self.publish_port = config.get_value(CONF_BIND_PORT, DEFAULT_PORT)
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
            base_url=f"http://{format_ip_for_url(str(self.publish_ip))}:{self.publish_port}",
            static_routes=[
                (
                    "*",
                    "/flow/{session_id}/{queue_id}/{queue_item_id}/{player_id}.{fmt}",
                    self.serve_queue_flow_stream,
                ),
                (
                    "*",
                    "/single/{session_id}/{queue_id}/{queue_item_id}/{player_id}.{fmt}",
                    self.serve_queue_item_stream,
                ),
                ("*", "/command/{queue_id}/{command}.mp3", self.serve_command_request),
                ("*", "/announcement/{player_id}.{fmt}", self.serve_announcement_stream),
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
        await self._audio_analysis.close()
        await self._server.close()

    async def resolve_stream_url(self, player_id: str, media: PlayerMedia) -> str:
        """
        Resolve the stream URL for the given PlayerMedia.

        :param player_id: The (protocol) player ID requesting the stream.
        :param media: The PlayerMedia object for which to resolve the stream URL.
        :return: The resolved stream URL as a string.
        """
        if media.media_type in (MediaType.ANNOUNCEMENT, MediaType.FLOW_STREAM):
            return media.uri
        if media.media_type == MediaType.PLUGIN_SOURCE:
            if media.custom_data and (source_id := media.custom_data.get("source_id")):
                plugin_source = self.mass.players.get_plugin_source(source_id)
                if plugin_source:
                    return await self.get_plugin_source_url(plugin_source, player_id)
            return media.uri
        protocol_player = self.mass.players.get_player(player_id)
        conf_output_codec = cast(
            "str",
            protocol_player.config.get_value(CONF_OUTPUT_CODEC, default="flac")
            if protocol_player
            else "flac",
        )
        output_codec = ContentType.try_parse(conf_output_codec)
        fmt = output_codec.value
        # handle raw pcm without exact format specifiers
        if output_codec.is_pcm() and ";" not in fmt:
            fmt += f";codec=pcm;rate={44100};bitrate={16};channels={2}"
        extra_data = media.custom_data or {}
        session_id = extra_data.get("session_id")
        queue_item_id = media.queue_item_id
        if not session_id or not queue_item_id:
            raise InvalidDataError("Can not resolve stream URL: Invalid PlayerMedia data")
        queue_id = media.source_id
        crossfade_needs_flow_mode = (
            # if the player(queue) has crossfade enabled but the player(protocol) does not support
            # gapless playback, we need to enforce flow mode
            queue_id
            and (queue_player := self.mass.players.get_player(queue_id))
            and queue_player.config.get_value(CONF_SMART_FADES_MODE) != SmartFadesMode.DISABLED
            and protocol_player
            and not protocol_player.supports_gapless
        )
        # Determine flow_mode based on the actual player's capabilities.
        # This is done here (just-in-time) because the player's protocol determines this
        flow_mode = (
            protocol_player is not None
            and (protocol_player.flow_mode or crossfade_needs_flow_mode)
            and media.media_type not in (MediaType.RADIO, MediaType.PLUGIN_SOURCE)
        )
        base_path = "flow" if flow_mode else "single"
        return f"{self._server.base_url}/{base_path}/{session_id}/{queue_id}/{queue_item_id}/{player_id}.{fmt}"

    async def get_plugin_source_url(self, plugin_source: PluginSource, player_id: str) -> str:
        """Get the url for the Plugin Source stream/proxy."""
        if plugin_source.audio_format.content_type.is_pcm():
            fmt = ContentType.WAV.value
        else:
            fmt = plugin_source.audio_format.content_type.value
        return f"{self._server.base_url}/pluginsource/{plugin_source.id}/{player_id}.{fmt}"

    async def serve_queue_item_stream(self, request: web.Request) -> web.StreamResponse:  # noqa: PLR0915
        """Stream single queueitem audio to a player."""
        self._log_request(request)
        queue_id = request.match_info["queue_id"]
        player_id = request.match_info["player_id"]
        if not (queue := self.mass.player_queues.get(queue_id)):
            raise web.HTTPNotFound(reason=f"Unknown Queue: {queue_id}")
        session_id = request.match_info["session_id"]
        if queue.session_id and session_id != queue.session_id:
            raise web.HTTPNotFound(reason=f"Unknown (or invalid) session: {session_id}")
        if not (player := self.mass.players.get_player(player_id)):
            raise web.HTTPNotFound(reason=f"Unknown Player: {player_id}")
        queue_item_id = request.match_info["queue_item_id"]
        queue_item = self.mass.player_queues.get_item(queue_id, queue_item_id)
        if not queue_item:
            raise web.HTTPNotFound(reason=f"Unknown Queue item: {queue_item_id}")
        if not queue_item.streamdetails:
            try:
                queue_item.streamdetails = await self.audio.get_stream_details(
                    queue_item=queue_item
                )
            except Exception as e:
                self.logger.error(
                    "Failed to get streamdetails for QueueItem %s: %s", queue_item_id, e
                )
                queue_item.available = False
                raise web.HTTPNotFound(reason=f"No streamdetails for Queue item: {queue_item_id}")

        # pick output format based on the streamdetails and player capabilities
        pcm_format = await self.audio.select_pcm_format(
            player=player, streamdetails=queue_item.streamdetails, smartfades_enabled=True
        )
        output_format = await self.audio.get_output_format(
            output_format_str=request.match_info["fmt"],
            player=player,
            content_sample_rate=pcm_format.sample_rate,
            content_bit_depth=pcm_format.bit_depth,
            media_type=queue_item.media_type,
        )

        # prepare request, add some DLNA/UPNP compatible headers
        # icy-name is sanitized to avoid a "Potential header injection attack" exception by aiohttp
        # see https://github.com/music-assistant/support/issues/4913
        # use realtime DLNA flags for radio (sender-paced) since the source delivers slowly
        dlna_features = (
            DLNA_CONTENT_FEATURES_REALTIME
            if queue_item.media_type != MediaType.TRACK
            else DLNA_CONTENT_FEATURES
        )
        headers = {
            **DEFAULT_STREAM_HEADERS,
            "icy-name": queue_item.name.replace("\n", " ").replace("\r", " ").replace("\t", " "),
            "contentFeatures.dlna.org": dlna_features,
            "Content-Type": get_mime_type(output_format.output_format_str),
        }

        resp = web.StreamResponse(status=200, reason="OK", headers=headers)
        resp.content_type = get_mime_type(output_format.output_format_str)
        http_profile = await self.mass.config.get_player_config_value(
            player_id, CONF_HTTP_PROFILE, default="default", return_type=str
        )
        if http_profile == "forced_content_length" and not queue_item.duration:
            # just set an insane high content length to make sure the player keeps playing
            resp.content_length = calculate_content_length(output_format, 12 * 3600)
        elif http_profile == "forced_content_length" and queue_item.duration:
            # estimate content length based on effective duration
            # account for seek position (e.g., crossfade from previous track)
            seek_pos = queue_item.streamdetails.seek_position if queue_item.streamdetails else 0
            effective_duration = max(queue_item.duration - seek_pos, 1)
            # use cached actual bytes-per-second if available (from a previous stream)
            resp.content_length = await get_content_length(
                self.mass, queue_item.uri, output_format, effective_duration
            )
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
            and PlayerFeature.GAPLESS_PLAYBACK not in player.state.supported_features
        ):
            self.logger.warning(
                "Crossfade disabled: Player %s does not support gapless playback, "
                "consider enabling flow mode to enable crossfade on this player.",
                player.state.name if player else "Unknown Player",
            )
            smart_fades_mode = SmartFadesMode.DISABLED

        if smart_fades_mode != SmartFadesMode.DISABLED:
            # crossfade is enabled, use special crossfaded single item stream
            # where the crossfade of the next track is present in the stream of
            # a single track. This only works if the player supports gapless playback!
            audio_input = self.audio.get_queue_item_stream_with_smartfade(
                player=player,
                queue_item=queue_item,
                pcm_format=pcm_format,
                smart_fades_mode=smart_fades_mode,
                standard_crossfade_duration=standard_crossfade_duration,
            )
        else:
            # no crossfade, just a regular single item stream
            audio_input = self.audio.get_queue_item_stream(
                queue_item=queue_item,
                pcm_format=pcm_format,
                seek_position=queue_item.streamdetails.seek_position,
                playback_speed=cast(
                    "float", queue_item.extra_attributes.get("playback_speed", 1.0)
                ),
            )
        # stream the audio
        # this final ffmpeg process in the chain will convert the raw, lossless PCM audio into
        # the desired output format for the player including any player specific filter params
        # such as channels mixing, DSP, resampling and, only if needed, encoding to lossy formats
        first_chunk_received = False
        bytes_sent = 0
        async for chunk in get_ffmpeg_stream(
            audio_input=audio_input,
            input_format=pcm_format,
            output_format=output_format,
            filter_params=self.audio.get_player_filter_params(
                player_id=player.player_id, input_format=pcm_format, output_format=output_format
            ),
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
                if (
                    first_chunk_received
                    and not player.stop_called
                    and queue_item.streamdetails.duration  # ignore for radio streams
                ):
                    # Player disconnected (unexpected) after receiving at least some data
                    # This could indicate buffering issues, network problems,
                    # or player-specific issues.
                    self.logger.warning(
                        "Player %s disconnected prematurely from stream for %s (%s) - "
                        "error: %s, sent %d bytes, content_length=%s",
                        queue.display_name,
                        queue_item.name,
                        queue_item.uri,
                        err.__class__.__name__,
                        bytes_sent,
                        resp.content_length,
                    )
                break
        if queue_item.streamdetails.stream_error:
            self.logger.error(
                "Error streaming QueueItem %s (%s) to %s",
                queue_item.name,
                queue_item.uri,
                queue.display_name,
            )
        elif (
            bytes_sent > 0
            and queue_item.streamdetails
            and queue_item.streamdetails.seconds_streamed
            and queue_item.duration
        ):
            # cache the actual encoded bytes-per-second for this URI + output format
            # so future content_length estimates are near-exact
            self.mass.create_task(
                store_content_length_in_cache(
                    self.mass,
                    queue_item.uri,
                    output_format,
                    bytes_sent,
                    queue_item.streamdetails.seconds_streamed,
                )
            )
        return resp

    async def serve_queue_flow_stream(self, request: web.Request) -> web.StreamResponse:
        """Stream Queue Flow audio to player."""
        self._log_request(request)
        queue_id = request.match_info["queue_id"]
        player_id = request.match_info["player_id"]
        if not (queue := self.mass.player_queues.get(queue_id)):
            raise web.HTTPNotFound(reason=f"Unknown Queue: {queue_id}")
        if not (player := self.mass.players.get_player(player_id)):
            raise web.HTTPNotFound(reason=f"Unknown Player: {player_id}")
        start_queue_item_id = request.match_info["queue_item_id"]
        start_queue_item = self.mass.player_queues.get_item(queue_id, start_queue_item_id)
        if not start_queue_item:
            raise web.HTTPNotFound(reason=f"Unknown Queue item: {start_queue_item_id}")

        # select the highest possible PCM settings for this player
        flow_pcm_format = await self.audio.select_flow_format(player)

        # work out output format/details
        output_format = await self.audio.get_output_format(
            output_format_str=request.match_info["fmt"],
            player=player,
            content_sample_rate=flow_pcm_format.sample_rate,
            content_bit_depth=flow_pcm_format.bit_depth,
            media_type=start_queue_item.media_type,
        )
        # work out ICY metadata support
        icy_preference = self.mass.config.get_raw_player_config_value(
            player_id,
            CONF_ENTRY_ENABLE_ICY_METADATA.key,
            CONF_ENTRY_ENABLE_ICY_METADATA.default_value,
        )
        enable_icy = request.headers.get("Icy-MetaData", "") == "1" and icy_preference != "disabled"
        icy_meta_interval = 256000 if icy_preference == "full" else 16384

        # prepare request, add some DLNA/UPNP compatible headers
        headers = {
            **DEFAULT_STREAM_HEADERS,
            **ICY_HEADERS,
            "contentFeatures.dlna.org": DLNA_CONTENT_FEATURES_REALTIME,
            "Content-Type": get_mime_type(output_format.output_format_str),
        }
        if enable_icy:
            headers["icy-metaint"] = str(icy_meta_interval)

        resp = web.StreamResponse(status=200, reason="OK", headers=headers)
        http_profile = await self.mass.config.get_player_config_value(
            player_id, CONF_HTTP_PROFILE, default="default", return_type=str
        )
        if http_profile == "forced_content_length":
            # just set an insane high content length to make sure the player keeps playing
            resp.content_length = calculate_content_length(output_format, 12 * 3600)
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
            audio_input=self.audio.get_queue_flow_stream(
                queue=queue, start_queue_item=start_queue_item, pcm_format=flow_pcm_format
            ),
            input_format=flow_pcm_format,
            output_format=output_format,
            filter_params=self.audio.get_player_filter_params(
                player.player_id, flow_pcm_format, output_format
            ),
            # we need to slowly feed the music to avoid the player stopping and later
            # restarting (or completely failing) the audio stream by keeping the buffer short.
            # this is reported to be an issue especially with Chromecast players.
            # see for example: https://github.com/music-assistant/support/issues/3717
            # allow buffer ahead of a few seconds and read rest in (near) realtime
            extra_input_args=["-readrate", "1.1", "-readrate_initial_burst", "5"],
            chunk_size=icy_meta_interval if enable_icy else calculate_content_length(output_format),
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
                content_type=get_mime_type(audio_format.output_format_str),
                headers=DEFAULT_STREAM_HEADERS,
            )

        resp = web.StreamResponse(status=200, reason="OK", headers=DEFAULT_STREAM_HEADERS)
        resp.content_type = get_mime_type(audio_format.output_format_str)
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
            player.state.name,
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
            player.state.name,
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
        player = self.mass.players.get_player(player_id)
        if not player:
            raise web.HTTPNotFound(reason=f"Unknown Player: {player_id}")
        plugin_source = provider.get_source()
        output_format = await self.audio.get_output_format(
            output_format_str=request.match_info["fmt"],
            player=player,
            content_sample_rate=plugin_source.audio_format.sample_rate,
            content_bit_depth=plugin_source.audio_format.bit_depth,
            media_type=MediaType.PLUGIN_SOURCE,
        )
        headers = {
            **DEFAULT_STREAM_HEADERS,
            "contentFeatures.dlna.org": DLNA_CONTENT_FEATURES_REALTIME,
            "icy-name": plugin_source.name,
            "Content-Type": get_mime_type(output_format.output_format_str),
        }

        resp = web.StreamResponse(status=200, reason="OK", headers=headers)
        resp.content_type = get_mime_type(output_format.output_format_str)
        http_profile = await self.mass.config.get_player_config_value(
            player_id, CONF_HTTP_PROFILE, default="default", return_type=str
        )
        if http_profile == "forced_content_length":
            # just set an insanely high content length to make sure the player keeps playing
            resp.content_length = calculate_content_length(output_format, 12 * 3600)
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
            player_filter_params=self.audio.get_player_filter_params(
                player_id, plugin_source.audio_format, output_format
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
        self,
        media: PlayerMedia,
        pcm_format: AudioFormat,
        player_id: str | None = None,
        force_flow_mode: bool = False,
        use_flow_stream_buffering: bool = False,
    ) -> AsyncGenerator[bytes, None]:
        """
        Get a stream of the given media as raw PCM audio.

        This is used as helper for player providers that can consume the raw PCM
        audio stream directly (e.g. AirPlay) and not rely on HTTP transport.

        :param media: The PlayerMedia to stream.
        :param pcm_format: The desired output PCM format.
        :param player_id: The player ID requesting the stream. Used to determine
            if flow mode should be used based on the player's capabilities.
        :param force_flow_mode: Force flow mode regardless of player capabilities.
            Used for multi-client streaming scenarios that require continuous streams.
        :param use_flow_stream_buffering: Buffer the flow stream to provide headroom
            during smart fades transitions. Use for consumers that read directly
            (e.g. AirPlay, Snapcast) and can't tolerate stalls.
        """
        # select audio source
        if media.media_type == MediaType.ANNOUNCEMENT:
            # special case: stream announcement
            assert media.custom_data
            return self.get_announcement_stream(
                media.custom_data["announcement_url"],
                output_format=pcm_format,
                pre_announce=media.custom_data["pre_announce"],
                pre_announce_url=media.custom_data["pre_announce_url"],
            )
        if media.media_type == MediaType.PLUGIN_SOURCE:
            # special case: plugin source stream
            assert media.custom_data
            return self.get_plugin_source_stream(
                plugin_source_id=media.custom_data["source_id"],
                output_format=pcm_format,
                # need to pass player_id from the PlayerMedia object
                # because this could have been a group
                player_id=media.custom_data["player_id"],
            )
        if (
            media.source_id
            and media.source_id.startswith(UGP_PREFIX)
            and media.uri
            and "/ugp/" in media.uri
        ):
            # special case: member player accessing UGP stream
            # Check URI to distinguish from the UGP accessing its own stream
            ugp_player = cast("UniversalGroupPlayer", self.mass.players.get_player(media.source_id))
            ugp_stream = ugp_player.stream
            assert ugp_stream is not None  # for type checker
            if ugp_stream.base_pcm_format == pcm_format:
                # no conversion needed
                return ugp_stream.subscribe_raw()
            return ugp_stream.get_stream(output_format=pcm_format)
        if media.source_id and media.queue_item_id:
            # Queue stream request - determine flow_mode based on player capabilities
            # or force it if explicitly requested (e.g., for multi-client streaming)
            protocol_player = self.mass.players.get_player(player_id) if player_id else None
            queue_id = media.source_id
            crossfade_needs_flow_mode = (
                # if the player(queue) has crossfade enabled but the player(protocol)
                # does not support gapless playback, we need to enforce flow mode
                queue_id
                and (queue_player := self.mass.players.get_player(queue_id))
                and queue_player.config.get_value(CONF_SMART_FADES_MODE) != SmartFadesMode.DISABLED
                and protocol_player
                and not protocol_player.supports_gapless
            )
            flow_mode = (
                force_flow_mode
                or (protocol_player is not None and protocol_player.flow_mode)
                or crossfade_needs_flow_mode
            )
            if media.media_type == MediaType.RADIO:
                # flow_mode for radio is pointless
                flow_mode = False
            if flow_mode:
                # flow stream request
                queue = self.mass.player_queues.get(media.source_id)
                assert queue
                start_queue_item = self.mass.player_queues.get_item(
                    media.source_id, media.queue_item_id
                )
                assert start_queue_item
                flow_stream = self.audio.get_queue_flow_stream(
                    queue=queue, start_queue_item=start_queue_item, pcm_format=pcm_format
                )
                if use_flow_stream_buffering:
                    return buffered(flow_stream, buffer_size=30, min_buffer_before_yield=1)
                return flow_stream
            # single item stream (e.g. radio or non-flow mode)
            queue_item = self.mass.player_queues.get_item(media.source_id, media.queue_item_id)
            assert queue_item
            return self.audio.get_queue_item_stream(
                queue_item=queue_item,
                pcm_format=pcm_format,
                playback_speed=cast(
                    "float", queue_item.extra_attributes.get("playback_speed", 1.0)
                ),
            )
        # assume url or some other direct path
        # NOTE: this will fail if its an uri not playable by ffmpeg
        return get_ffmpeg_stream(
            audio_input=media.uri,
            input_format=AudioFormat(content_type=ContentType.try_parse(media.uri)),
            output_format=pcm_format,
        )

    async def get_preview_stream(
        self,
        provider_instance_id_or_domain: str,
        item_id: str,
        media_type: MediaType = MediaType.TRACK,
    ) -> AsyncGenerator[bytes, None]:
        """Create a 30 seconds preview audioclip for the given media item."""
        if not (music_prov := self.mass.get_provider(provider_instance_id_or_domain)):
            raise ProviderUnavailableError
        if TYPE_CHECKING:
            assert isinstance(music_prov, MusicProvider)

        if not await music_prov.get_item(media_type, item_id):
            msg = f"Item {item_id} not found in provider {provider_instance_id_or_domain}"
            raise InvalidDataError(msg)

        streamdetails = await music_prov.get_stream_details(item_id, media_type)
        pcm_format = AudioFormat(
            content_type=ContentType.from_bit_depth(streamdetails.audio_format.bit_depth),
            sample_rate=streamdetails.audio_format.sample_rate,
            bit_depth=streamdetails.audio_format.bit_depth,
            channels=streamdetails.audio_format.channels,
        )
        async for chunk in get_ffmpeg_stream(
            audio_input=self.audio.get_media_stream(
                streamdetails=streamdetails, pcm_format=pcm_format
            ),
            input_format=pcm_format,
            output_format=AudioFormat(content_type=ContentType.AAC),
            extra_input_args=["-t", "30"],
        ):
            yield chunk

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
            fmt = announcement_url.rsplit(".", maxsplit=1)[-1]
            try:
                async for chunk in get_ffmpeg_stream(
                    audio_input=announcement_url,
                    input_format=AudioFormat(content_type=ContentType.try_parse(fmt)),
                    output_format=pcm_format,
                    chunk_size=calculate_content_length(pcm_format, 1),
                ):
                    await announcement_data.put(chunk)
            except AudioError as err:
                self.logger.warning(
                    "Failed to fetch announcement audio from %s: %s", announcement_url, err
                )
            finally:
                await announcement_data.put(None)  # always signal end of stream

        self.mass.create_task(fetch_announcement())

        async def _announcement_stream() -> AsyncGenerator[bytes, None]:
            """Generate the PCM audio stream for the announcement + optional pre-announce."""
            if pre_announce:
                async for chunk in get_ffmpeg_stream(
                    audio_input=pre_announce_url,
                    input_format=AudioFormat(content_type=ContentType.try_parse(pre_announce_url)),
                    output_format=pcm_format,
                    chunk_size=calculate_content_length(pcm_format, 1),
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
            audio_input=_announcement_stream(), input_format=pcm_format, output_format=output_format
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
                "Got %s request to %s from %s", request.method, request.path, request.remote
            )

    async def _periodic_garbage_collection(self) -> None:
        """Periodic garbage collection to free up memory from audio buffers and streams."""
        self.logger.log(VERBOSE_LOG_LEVEL, "Running periodic garbage collection...")
        # Run gc.collect on the event loop thread to avoid thread-safety issues
        # with StreamWriter.__del__ calling close() which requires the event loop thread
        collected = gc.collect()
        self.logger.log(
            VERBOSE_LOG_LEVEL, "Garbage collection completed, collected %d objects", collected
        )
        # Schedule next run in 15 minutes
        self.mass.call_later(900, self._periodic_garbage_collection)

    def _setup_smart_fades_logger(self, config: CoreConfig) -> None:
        """Set up smart fades logger level."""
        log_level = str(config.get_value(CONF_SMART_FADES_LOG_LEVEL))
        if log_level == "GLOBAL":
            self.audio.smart_fades_mixer.logger.setLevel(self.logger.level)
        else:
            self.audio.smart_fades_mixer.logger.setLevel(log_level)
