"""
Controller to stream audio to players.

The streams controller hosts a basic, unprotected HTTP-only webserver
purely to stream audio packets to players.
"""

from __future__ import annotations

import logging
import os
import struct
import time
from collections.abc import AsyncGenerator
from contextlib import aclosing
from math import ceil
from typing import TYPE_CHECKING, cast
from uuid import uuid4

from aiofiles.os import wrap
from aiohttp import web
from music_assistant_models.audio_processing import AudioQueueProcessing
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    CrossfadeMode,
    MediaType,
    PlayerFeature,
    ProviderType,
    VolumeNormalizationMode,
)
from music_assistant_models.errors import (
    AudioError,
    InvalidDataError,
    MediaNotFoundError,
    ProviderUnavailableError,
)
from music_assistant_models.helpers import get_global_cache_value
from music_assistant_models.media_items import AudioFormat

from music_assistant.constants import (
    CONF_BACKGROUND_SCAN_CONCURRENCY,
    CONF_BIND_IP,
    CONF_BIND_PORT,
    CONF_CROSSFADE_DURATION,
    CONF_CROSSFADE_MODE,
    CONF_ENTRY_ENABLE_ICY_METADATA,
    CONF_ENTRY_LOG_LEVEL,
    CONF_ENTRY_VOLUME_NORMALIZATION_TARGET,
    CONF_HTTP_PROFILE,
    CONF_OUTPUT_CODEC,
    CONF_PLAYER_QUEUES,
    CONF_PUBLISH_IP,
    CONF_VALUE_AUTO,
    CONF_VOLUME_NORMALIZATION_FIXED_GAIN_RADIO,
    CONF_VOLUME_NORMALIZATION_FIXED_GAIN_TRACKS,
    CONF_VOLUME_NORMALIZATION_RADIO,
    CONF_VOLUME_NORMALIZATION_TRACKS,
    DEFAULT_BACKGROUND_SCAN_CONCURRENCY,
    DEFAULT_HOST,
    DEFAULT_STREAM_HEADERS,
    DLNA_CONTENT_FEATURES,
    DLNA_CONTENT_FEATURES_REALTIME,
    ICY_HEADERS,
    SILENCE_FILE,
    VERBOSE_LOG_LEVEL,
    WILDCARD_BIND_IPS,
)
from music_assistant.controllers.players.helpers import AnnounceData
from music_assistant.controllers.streams.announcements import (
    DEFAULT_RENDER_TIMEOUT,
    AnnouncementRenderer,
)
from music_assistant.controllers.streams.audio import StreamsAudio, overlay_active
from music_assistant.controllers.streams.audio_analysis import AudioAnalysisController
from music_assistant.controllers.streams.audio_processing import (
    AudioProcessingManager,
)
from music_assistant.controllers.streams.constants import (
    CONF_ALLOW_CROSSFADE_SAME_ALBUM,
    CONF_BUFFER_SIZE,
    CONF_BUFFER_SIZE_DEFAULT,
    CONF_SMART_FADES_LOG_LEVEL,
    DEFAULT_PORT,
    BufferSize,
    get_available_buffer_sizes,
)
from music_assistant.helpers.audio import (
    calculate_content_length,
    get_content_length,
    get_mime_type,
    store_content_length_in_cache,
)
from music_assistant.helpers.buffered_generator import buffered
from music_assistant.helpers.ffmpeg import (
    CACHE_ATTR_FFMPEG_VERSION,
    CACHE_ATTR_LIBSOXR_PRESENT,
    check_ffmpeg_version,
    get_ffmpeg_stream,
)
from music_assistant.helpers.ffmpeg import LOGGER as FFMPEG_LOGGER
from music_assistant.helpers.util import (
    format_ip_for_url,
    get_ip_addresses,
    get_source_ip_for_target,
    sanitize_http_header_value,
)
from music_assistant.helpers.webserver import Webserver, redact_sensitive_headers
from music_assistant.models.core_controller import CoreController
from music_assistant.models.music_provider import MusicProvider
from music_assistant.models.plugin import PluginProvider
from music_assistant.providers.universal_group.constants import UGP_PREFIX
from music_assistant.providers.universal_group.player import UniversalGroupPlayer

if TYPE_CHECKING:
    from music_assistant_models.config_entries import CoreConfig
    from music_assistant_models.player import PlayerMedia
    from music_assistant_models.player_queue import PlayerQueue
    from music_assistant_models.queue_item import QueueItem
    from music_assistant_models.streamdetails import StreamMetadata

    from music_assistant.helpers.json import SerializableType
    from music_assistant.mass import MusicAssistant


isfile = wrap(os.path.isfile)


def _streaming_wav_header(output_format: AudioFormat) -> bytes:
    """Build a WAV header with open-ended (0xFFFFFFFF) RIFF/data sizes for live streams."""
    channels = output_format.channels
    sample_rate = output_format.sample_rate
    bits_per_sample = output_format.bit_depth
    byte_rate = sample_rate * channels * (bits_per_sample // 8)
    block_align = channels * (bits_per_sample // 8)
    # RIFF size & data size both set to 0xFFFFFFFF so clients honoring the WAV
    # length fields don't cut the stream off (default header hardcodes ~6.7h).
    return (
        b"RIFF"
        + struct.pack("<L", 0xFFFFFFFF)
        + b"WAVE"
        + b"fmt "
        + struct.pack(
            "<LHHLLHH", 16, 1, channels, sample_rate, byte_rate, block_align, bits_per_sample
        )
        + b"data"
        + struct.pack("<L", 0xFFFFFFFF)
    )


async def _wav_passthrough_stream(
    audio_input: AsyncGenerator[bytes], output_format: AudioFormat
) -> AsyncGenerator[bytes]:
    """Yield a WAV header followed by raw PCM bytes from ``audio_input``."""
    yield _streaming_wav_header(output_format)
    async for chunk in audio_input:
        yield chunk


def _get_publish_addresses(
    bind_ip: str, configured_publish_ip: str | None, all_ip_addresses: tuple[str, ...]
) -> list[str]:
    """
    Return the addresses this host should advertise to players on the local network.

    :param bind_ip: The configured bind IP (a wildcard means all interfaces).
    :param configured_publish_ip: The explicitly configured publish IP, or None when auto.
    :param all_ip_addresses: All detected host IP addresses, in ranked order.
    """
    if configured_publish_ip:
        # an explicitly configured address is the authoritative answer
        return [configured_publish_ip]
    if bind_ip and bind_ip not in WILDCARD_BIND_IPS:
        # only one interface is served, so no other address can be reached
        return [bind_ip]
    # Auto-detected: the primary address is only a guess at which interface the players
    # live on, so advertise every address (highest ranked first) and let the device pick
    # one it can reach. On a multi-homed host - a VPN or docker interface alongside the
    # LAN - the primary-route address is regularly not the one on the players' network.
    return list(all_ip_addresses)


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
        self.announcement_renderer = AnnouncementRenderer()
        self._bind_ip: str = "0.0.0.0"
        self._base_url: str = ""
        self._configured_publish_ip: str | None = None
        # every address players may reach this host on, best candidate first - for mDNS
        # records, which can carry them all, unlike the single-valued publish_ip
        self.publish_addresses: list[str] = []
        # the network as it was at the previous setup, to spot a runtime change
        self._network_fingerprint: tuple[str, str, int, tuple[str, ...]] | None = None
        self.audio = StreamsAudio(mass)
        self.audio_processing = AudioProcessingManager(mass)
        self._audio_analysis = AudioAnalysisController(self)
        # Number of queue streams (single item or flow) actively serving a player right now.
        # Audio analysis reads this (via audio_analysis.playback_active) to yield CPU while a
        # queue stream is live. Announcements are a separate path that never runs analysis.
        self._active_output_streams = 0

    @property
    def audio_analysis(self) -> AudioAnalysisController:
        """Return the AudioAnalysisController instance."""
        return self._audio_analysis

    def output_stream_active(self) -> bool:
        """Return whether a queue stream (single item or flow) is actively serving a player."""
        return self._active_output_streams > 0

    async def get_diagnostics(self) -> dict[str, SerializableType]:
        """Return diagnostics info for this controller to include in diagnostics reports."""
        return {
            "ffmpeg_version": get_global_cache_value(CACHE_ATTR_FFMPEG_VERSION),
            "libsoxr_support": get_global_cache_value(CACHE_ATTR_LIBSOXR_PRESENT),
            "active_output_streams": self._active_output_streams,
            "active_announcements": self.announcement_renderer.active_announcements,
            "active_announcement_renders": self.announcement_renderer.active_renders,
            "publish_ip_configured": self._configured_publish_ip is not None,
        }

    @property
    def base_url(self) -> str:
        """Return the base_url for the streamserver."""
        return self._base_url

    @property
    def bind_ip(self) -> str:
        """Return the IP address this streamserver is bound to."""
        return self._bind_ip

    async def get_source_ip(self, target_ip: str | None = None) -> str | None:
        """
        Return a local, bindable source IP on the player-facing network.

        For callers that bind a socket or hand a local interface address to a helper
        process, so their traffic leaves on the network the players live on. The result
        is always an address of this host, never the advertised address, which may not
        exist here at all.

        Returns None when no single interface should be pinned, which the caller must
        read as "bind all interfaces and let the routing table decide".

        :param target_ip: IP address of the device the traffic is meant for. Omit it for
            a shared consumer that serves every player at once; such a caller can only be
            pinned by an explicitly configured bind IP.
        """
        if self._bind_ip and self._bind_ip not in WILDCARD_BIND_IPS:
            if target_ip and not _same_ip_family(self._bind_ip, target_ip):
                return None
            return self._bind_ip
        if not target_ip:
            return None
        return await get_source_ip_for_target(target_ip) or None

    def get_publish_ip(self, target_ip: str) -> str | None:
        """
        Return the address to advertise to the device at ``target_ip``, if one is configured.

        Only an explicitly configured publish IP is returned. An auto-detected one is a
        guess at this host's primary interface, which on a multi-homed host is not
        necessarily the network the players live on, so callers that can derive the
        address from the connection itself must prefer that over the guess.

        Returns None when no publish IP was configured, or when the configured one cannot
        apply to this device.

        :param target_ip: IP address of the device that would receive the address, used to
            reject an address of the wrong IP family.
        """
        if not self._configured_publish_ip:
            return None
        if not _same_ip_family(self._configured_publish_ip, target_ip):
            return None
        return self._configured_publish_ip

    @property
    def smart_fades_available(self) -> bool:
        """
        Return whether smart crossfade can be used on this server.

        Requires a large-enough audio buffer (at least balanced) and a loaded
        smart fades audio analysis provider.
        """
        buffer_size = BufferSize(
            self.mass.config.get_raw_core_config_value(
                self.domain, CONF_BUFFER_SIZE, CONF_BUFFER_SIZE_DEFAULT
            )
        )
        return (
            buffer_size != BufferSize.MINIMAL and self.audio_analysis.smart_fades_provider_available
        )

    def get_crossfade_mode(self, queue: PlayerQueue) -> CrossfadeMode:
        """
        Return the effective crossfade mode for a queue.

        Combines the per-play on/off toggle with the crossfade_mode setting and smart fades
        availability: smart when enabled, selected and available; standard when enabled but smart
        is not selected/available; disabled otherwise.
        """
        if not queue.crossfade_enabled:
            return CrossfadeMode.DISABLED
        # default to smart when this server can use it, else standard
        default_mode = (
            CrossfadeMode.SMART_CROSSFADE
            if self.smart_fades_available
            else CrossfadeMode.STANDARD_CROSSFADE
        )
        mode = self.mass.config.get_effective_player_queue_config_value(
            queue.queue_id, CONF_CROSSFADE_MODE, default_mode
        )
        if mode == CrossfadeMode.SMART_CROSSFADE and self.smart_fades_available:
            return CrossfadeMode.SMART_CROSSFADE
        return CrossfadeMode.STANDARD_CROSSFADE

    def is_smart_fades_active(self, queue: PlayerQueue) -> bool:
        """Return whether the queue's effective crossfade mode is smart crossfade."""
        return self.get_crossfade_mode(queue) == CrossfadeMode.SMART_CROSSFADE

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return all Config Entries for this core module (if any)."""
        ip_addresses = await get_ip_addresses(include_ipv6=True)
        return (
            ConfigEntry(
                key=CONF_BUFFER_SIZE,
                type=ConfigEntryType.STRING,
                default_value=CONF_BUFFER_SIZE_DEFAULT,
                # Only offer presets the host's RAM can sustain (Balanced >= 4GB,
                # Maximum >= 7GB); see get_available_buffer_sizes.
                options=[ConfigValueOption(size.value) for size in get_available_buffer_sizes()],
                required=False,
                category="playback",
            ),
            ConfigEntry(
                key=CONF_VOLUME_NORMALIZATION_RADIO,
                type=ConfigEntryType.STRING,
                default_value=VolumeNormalizationMode.FALLBACK_DYNAMIC,
                options=[
                    ConfigValueOption(x.value, title=x.value.replace("_", " ").title())
                    for x in VolumeNormalizationMode
                ],
                category="playback",
            ),
            ConfigEntry(
                key=CONF_VOLUME_NORMALIZATION_TRACKS,
                type=ConfigEntryType.STRING,
                default_value=VolumeNormalizationMode.FALLBACK_DYNAMIC,
                options=[
                    ConfigValueOption(x.value, title=x.value.replace("_", " ").title())
                    for x in VolumeNormalizationMode
                ],
                category="playback",
            ),
            ConfigEntry(
                key=CONF_VOLUME_NORMALIZATION_FIXED_GAIN_RADIO,
                type=ConfigEntryType.FLOAT,
                range=(-20, 10),
                default_value=-6,
                category="playback",
            ),
            ConfigEntry(
                key=CONF_VOLUME_NORMALIZATION_FIXED_GAIN_TRACKS,
                type=ConfigEntryType.FLOAT,
                range=(-20, 10),
                default_value=-6,
                category="playback",
            ),
            CONF_ENTRY_VOLUME_NORMALIZATION_TARGET,
            ConfigEntry(
                key=CONF_ALLOW_CROSSFADE_SAME_ALBUM,
                type=ConfigEntryType.BOOLEAN,
                default_value=False,
                category="playback",
            ),
            ConfigEntry(
                key=CONF_PUBLISH_IP,
                type=ConfigEntryType.STRING,
                default_value=CONF_VALUE_AUTO,
                required=False,
                category="generic",
                advanced=True,
                requires_reload=True,
            ),
            ConfigEntry(
                key=CONF_BIND_PORT,
                type=ConfigEntryType.INTEGER,
                default_value=DEFAULT_PORT,
                category="generic",
                advanced=True,
                requires_reload=True,
            ),
            ConfigEntry(
                key=CONF_BIND_IP,
                type=ConfigEntryType.STRING,
                default_value=DEFAULT_HOST,
                options=[ConfigValueOption(x, title=x) for x in {DEFAULT_HOST, *ip_addresses}],
                category="generic",
                advanced=True,
                required=False,
                requires_reload=True,
            ),
            ConfigEntry(
                key=CONF_SMART_FADES_LOG_LEVEL,
                type=ConfigEntryType.STRING,
                options=CONF_ENTRY_LOG_LEVEL.options,
                default_value="GLOBAL",
                category="audio_analysis",
                advanced=True,
            ),
            ConfigEntry(
                key=CONF_BACKGROUND_SCAN_CONCURRENCY,
                type=ConfigEntryType.INTEGER,
                range=(1, 16),
                default_value=DEFAULT_BACKGROUND_SCAN_CONCURRENCY,
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
        configured_publish_ip = str(config.get_value(CONF_PUBLISH_IP) or CONF_VALUE_AUTO)
        self._configured_publish_ip = (
            None if configured_publish_ip == CONF_VALUE_AUTO else configured_publish_ip
        )
        all_ip_addresses = await get_ip_addresses(include_ipv6=True)
        bind_ip = str(config.get_value(CONF_BIND_IP))
        self._resolve_publish_state(bind_ip, all_ip_addresses)
        await self._server.setup(
            bind_ip=bind_ip,
            bind_port=cast("int", self.publish_port),
            base_url=self._base_url,
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
            ],
        )
        # adopt what the server actually bound to: a configured port of 0 is only resolved
        # by the OS at bind time and an unavailable bind IP falls back to all interfaces
        self.publish_port = cast("int", self._server.port)
        self._resolve_publish_state(self._server.bind_ip or DEFAULT_HOST, all_ip_addresses)
        # print a big fat message in the log where the streamserver is running
        # because this is a common source of issues for people with more complex setups
        self.logger.log(
            logging.INFO if self.mass.config.onboard_done else logging.WARNING,
            "\n\n################################################################################\n"
            "Started streamserver on %s:%s\n"
            "This is the IP address that is communicated to players.\n"
            "If this is incorrect, audio will not play!\n"
            "See the documentation for how to configure the publish IP for the Streamserver\n"
            "in Settings --> System --> Streams\n"
            "################################################################################\n",
            self.publish_ip,
            self.publish_port,
        )
        await self._reload_network_dependent_providers()

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
        protocol_player = self.mass.players.get_player(player_id)
        # AudioSource is realtime: serve as WAV (PCM + header) so the encode
        # step is a no-op passthrough — drops a whole ffmpeg from the
        # consumer-side pipeline and the latency that comes with it.
        if media.media_type == MediaType.AUDIO_SOURCE:
            output_codec = ContentType.WAV
        else:
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
        queue = self.mass.player_queues.get(queue_id) if queue_id else None
        crossfade_needs_flow_mode = (
            # crossfade only applies to tracks; if the queue has it enabled but the player(protocol)
            # does not support gapless playback, we need to enforce flow mode
            media.media_type == MediaType.TRACK
            and queue is not None
            and queue.crossfade_enabled
            and protocol_player
            and not protocol_player.supports_gapless
        )
        # the audio overlay is mixed into the queue's continuous (flow) stream;
        # per-item requests would restart the overlay at every track boundary
        overlay_needs_flow_mode = queue is not None and overlay_active(queue)
        # Determine flow_mode based on the actual player's capabilities.
        # This is done here (just-in-time) because the player's protocol determines this
        flow_mode = (
            protocol_player is not None
            and (protocol_player.flow_mode or crossfade_needs_flow_mode or overlay_needs_flow_mode)
            and media.media_type not in (MediaType.RADIO, MediaType.AUDIO_SOURCE)
        )
        base_path = "flow" if flow_mode else "single"
        return (
            f"{self.base_url}/{base_path}/{session_id}/{queue_id}/{queue_item_id}/{player_id}.{fmt}"
        )

    def update_stream_metadata(
        self,
        queue_id: str,
        source_id: str,
        provider: str,
        stream_metadata: StreamMetadata,
    ) -> None:
        """
        Push a live stream metadata update for an active AudioSource queue item.

        Used by plugin providers exposing an AudioSource (e.g. AirPlay receiver,
        Spotify Connect) to surface live track-change info without restarting
        the stream. The radio ICY metadata path uses the same underlying field
        via ``_update_radio_stream_metadata`` but goes through a separate path.

        The update is rejected silently unless the queue's current item is an
        AudioSource owned by ``provider`` with ``item_id == source_id``. This
        guard prevents a late callback (e.g. the provider firing one more
        metadata update after MA has already moved the queue on to a track or
        a different AudioSource) from stamping unrelated metadata over the
        wrong item.

        :param queue_id: The queue whose active item should receive the update.
        :param source_id: The AudioSource.item_id emitting this metadata.
        :param provider: The provider instance id emitting this metadata.
        :param stream_metadata: The new stream metadata to attach.
        """
        queue = self.mass.player_queues.get(queue_id)
        if queue is None:
            return
        current_item = queue.current_item
        if current_item is None or current_item.streamdetails is None:
            return
        sd = current_item.streamdetails
        if (
            sd.media_type != MediaType.AUDIO_SOURCE
            or sd.provider != provider
            or sd.item_id != source_id
        ):
            # Log at debug so misbehaving providers firing constantly are
            # diagnosable (count alone is the signal) without spamming higher
            # log levels for the legitimate transition cases.
            self.logger.debug(
                "Rejected metadata update for queue %s from provider %s source %s "
                "(current item: %s)",
                queue_id,
                provider,
                source_id,
                sd.uri if sd else "none",
            )
            return
        # Re-check identity *after* preparing the write so a queue advance that
        # races with this callback can't slip in between the guard above and
        # the mutation below. Plugins fire these from arbitrary executor /
        # event-loop threads (AirPlay metadata reader, Spotify webservice
        # handler, AriaCast WebSocket reader); the GIL keeps each attribute
        # write atomic but not the read-then-write sequence.
        if queue.current_item is not current_item or current_item.streamdetails is not sd:
            return
        sd.stream_metadata = stream_metadata
        sd.stream_metadata_last_updated = time.time()
        self.mass.player_queues.signal_update(queue_id)

    async def serve_queue_item_stream(self, request: web.Request) -> web.StreamResponse:  # noqa: PLR0915
        """Stream single queueitem audio to a player."""
        self._log_request(request)
        queue_id = request.match_info["queue_id"]
        player_id = request.match_info["player_id"]
        if not (queue := self.mass.player_queues.get(queue_id)):
            raise web.HTTPNotFound(reason=f"Unknown Queue: {queue_id}")
        session_id = request.match_info["session_id"]
        pq_data = self.mass.player_queues.queue_data(queue.queue_id)
        if pq_data.session_id is None or session_id != pq_data.session_id:
            raise web.HTTPNotFound(reason=f"Unknown (or invalid) session: {session_id}")
        if not (player := self.mass.players.get_player(player_id)):
            raise web.HTTPNotFound(reason=f"Unknown Player: {player_id}")
        queue_item_id = request.match_info["queue_item_id"]
        queue_item = self.mass.player_queues.get_item(queue_id, queue_item_id)
        if not queue_item:
            raise web.HTTPNotFound(reason=f"Unknown Queue item: {queue_item_id}")

        is_audio_source = (
            queue_item.media_item is not None
            and queue_item.media_item.media_type == MediaType.AUDIO_SOURCE
        )

        # HEAD probes for AudioSource items return a minimal response without
        # touching the plugin. on_source_selected is the lifecycle hook that
        # claims ownership and fires off transfer/handoff side effects (stop
        # the previous player, redirect on disallowed switch, etc.), and a
        # renderer probing with HEAD before GET should not trigger any of
        # that. The actual GET request goes through the full hook chain.
        if request.method != "GET" and is_audio_source:
            # Validate the providing plugin still exists before advertising the
            # source. Many DLNA renderers cache HEAD responses; returning 200
            # for a URI whose plugin has been unloaded would lie to the
            # renderer and the follow-up GET would fail unrecoverably.
            assert queue_item.media_item is not None
            if not isinstance(
                self.mass.get_provider(queue_item.media_item.provider), PluginProvider
            ):
                raise web.HTTPNotFound(
                    reason=f"AudioSource provider {queue_item.media_item.provider} unavailable"
                )
            # For PCM-fmt URLs, advertise audio/wav in HEAD: most DLNA renderers
            # key off the HEAD Content-Type to pick a decoder and do not handle
            # raw PCM (application/octet-stream). The actual GET response will
            # still wrap the bytes into a WAV container if needed via the same
            # mime-type translation downstream.
            head_fmt = request.match_info["fmt"]
            if ContentType.try_parse(head_fmt).is_pcm():
                head_fmt = ContentType.WAV.value
            headers = {
                **DEFAULT_STREAM_HEADERS,
                "icy-name": sanitize_http_header_value(queue_item.name),
                "contentFeatures.dlna.org": DLNA_CONTENT_FEATURES_REALTIME,
                "Content-Type": get_mime_type(head_fmt),
            }
            resp = web.StreamResponse(status=200, reason="OK", headers=headers)
            await resp.prepare(request)
            return resp

        # Fire on_source_selected hook for every AudioSource GET — this is the
        # single point where exclusive plugin sources claim ownership. Firing
        # unconditionally (regardless of whether streamdetails are cached from
        # a previous request) means a disconnect/reconnect for the same queue
        # item re-claims the lock with a fresh session id, instead of streaming
        # against the stale ownership of the prior request.
        # Source identity comes from queue_item.media_item because streamdetails
        # may not exist yet on the first request.
        # stream_session_id is a fresh per-request token threaded through to
        # on_source_unselected so the provider can distinguish a stale
        # teardown (e.g. a same-queue reconnect's first request completing
        # AFTER its replacement has already started streaming) from the
        # currently active session's real teardown.
        audio_source_provider: PluginProvider | None = None
        audio_source_id: str | None = None
        stream_session_id = uuid4().hex
        if (
            is_audio_source
            and queue_item.media_item is not None
            and (prov := self.mass.get_provider(queue_item.media_item.provider))
            and isinstance(prov, PluginProvider)
        ):
            audio_source_id = queue_item.media_item.item_id
            # Wire the provider into the finally block BEFORE awaiting the
            # hook: if the provider partially mutates state (claims the lock,
            # records the session id) and then raises a non-RuntimeError
            # exception (buggy plugin, asyncio.CancelledError, etc.), the
            # finally must still fire on_source_unselected so the lock gets
            # released. The provider's session-id guard makes a spurious
            # release a no-op if the lock was never actually claimed.
            audio_source_provider = prov
            try:
                await prov.on_source_selected(
                    audio_source_id, player_id, queue_id, stream_session_id
                )
            except RuntimeError as err:
                # Provider intentionally aborts the original request (e.g.
                # allow_player_switch=False has just redirected play_media to
                # the configured target). Surface as 404 so the disallowed
                # player drops the connection cleanly instead of treating an
                # uncaught 500 as transient and retrying. The provider
                # contract requires raising BEFORE claiming, so this is a
                # clean abort — but we still let the finally run, where the
                # session-id guard makes the unselect a no-op.
                self.logger.info(
                    "AudioSource %s aborted stream for player %s: %s",
                    audio_source_id,
                    player_id,
                    err,
                )
                raise web.HTTPNotFound(reason=str(err))

        try:
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
                    raise web.HTTPNotFound(
                        reason=f"No streamdetails for Queue item: {queue_item_id}"
                    )

            standard_crossfade_duration = self.mass.config.get_raw_core_config_value(
                CONF_PLAYER_QUEUES, CONF_CROSSFADE_DURATION, 8
            )
            if queue_item.media_type != MediaType.TRACK:
                crossfade_mode = CrossfadeMode.DISABLED
            else:
                crossfade_mode = self.get_crossfade_mode(queue)
            if (
                crossfade_mode != CrossfadeMode.DISABLED
                and PlayerFeature.GAPLESS_PLAYBACK not in player.state.supported_features
            ):
                self.logger.warning(
                    "Crossfade disabled: Player %s does not support gapless playback, "
                    "consider enabling flow mode to enable crossfade on this player.",
                    player.state.name,
                )
                crossfade_mode = CrossfadeMode.DISABLED

            # pick output format based on the streamdetails and player capabilities
            pcm_format = await self.audio.select_pcm_format(
                player=player,
                streamdetails=queue_item.streamdetails,
                crossfade_enabled=crossfade_mode != CrossfadeMode.DISABLED,
                overlay_active=(queue_item.media_type == MediaType.RADIO and overlay_active(queue)),
            )
            output_format = await self.audio.get_output_format(
                output_format_str=request.match_info["fmt"],
                player=player,
                content_sample_rate=pcm_format.sample_rate,
                content_bit_depth=pcm_format.bit_depth,
                media_type=queue_item.media_type,
            )

            # prepare request, add some DLNA/UPNP compatible headers
            # icy-name is sanitized (all control chars, not just newlines) to avoid a
            # "Potential header injection attack" ValueError by aiohttp
            # see https://github.com/music-assistant/support/issues/4913
            # and https://github.com/music-assistant/support/issues/5791
            # use realtime DLNA flags for radio (sender-paced) since the source delivers slowly
            dlna_features = (
                DLNA_CONTENT_FEATURES_REALTIME
                if queue_item.media_type != MediaType.TRACK
                else DLNA_CONTENT_FEATURES
            )
            headers = {
                **DEFAULT_STREAM_HEADERS,
                "icy-name": sanitize_http_header_value(queue_item.name),
                "contentFeatures.dlna.org": dlna_features,
                "Content-Type": get_mime_type(output_format.output_format_str),
            }

            resp = web.StreamResponse(status=200, reason="OK", headers=headers)
            resp.content_type = get_mime_type(output_format.output_format_str)
            http_profile = player.get_config_value(CONF_HTTP_PROFILE, "default")
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

            self._update_audio_processing_context(
                queue=queue,
                queue_item=queue_item,
                pcm_format=pcm_format,
                crossfade_mode=crossfade_mode,
                overlay_enabled=(
                    queue_item.media_type == MediaType.RADIO and overlay_active(queue)
                ),
                session_id=session_id,
            )

            if crossfade_mode != CrossfadeMode.DISABLED:
                # crossfade is enabled, use special crossfaded single item stream
                # where the crossfade of the next track is present in the stream of
                # a single track. This only works if the player supports gapless playback!
                audio_input = self.audio.get_queue_item_stream_with_smartfade(
                    player=player,
                    queue_item=queue_item,
                    pcm_format=pcm_format,
                    crossfade_mode=crossfade_mode,
                    standard_crossfade_duration=standard_crossfade_duration,
                    session_id=session_id,
                )
            else:
                # no crossfade, just a regular single item stream
                audio_input = self.audio.get_queue_item_stream(
                    queue_item=queue_item,
                    pcm_format=pcm_format,
                    seek_position=int(queue_item.streamdetails.seek_position),
                    playback_speed=cast(
                        "float", queue_item.extra_attributes.get("playback_speed", 1.0)
                    ),
                    session_id=session_id,
                )
            if queue_item.media_type == MediaType.RADIO and overlay_active(queue):
                # radio plays as a single long-lived stream (never in flow mode),
                # so mix the audio overlay in here
                audio_input = self.audio.get_overlay_mixed_stream(queue, audio_input, pcm_format)
            # stream the audio
            # this final ffmpeg process in the chain converts raw lossless PCM into
            # the desired output format for the player including any player specific
            # filter params such as channels mixing, DSP, resampling and, only if
            # needed, encoding to lossy formats
            output_plan = self.audio.get_player_output_plan(
                player_id=player.player_id,
                input_format=pcm_format,
                output_format=output_format,
                shared_player_ids=player.state.group_members,
                queue_id=queue_id,
                session_id=session_id,
                queue_item_id=queue_item.queue_item_id,
            )
            filter_params = output_plan.filter_params
            # Fast path for live AudioSource: when the player accepts WAV at the
            # source's exact PCM rate/depth/channels and no filters apply, we
            # skip the encode ffmpeg entirely and just stream a WAV header
            # followed by the raw PCM bytes — saves an ffmpeg process and the
            # latency of its internal buffer on every realtime stream.
            audio_bytes: AsyncGenerator[bytes]
            if (
                queue_item.media_type == MediaType.AUDIO_SOURCE
                and output_format.content_type == ContentType.WAV
                and not filter_params
                and output_format.sample_rate == pcm_format.sample_rate
                and output_format.bit_depth == pcm_format.bit_depth
                and output_format.channels == pcm_format.channels
            ):
                audio_bytes = _wav_passthrough_stream(audio_input, output_format)
            else:
                audio_bytes = get_ffmpeg_stream(
                    audio_input=audio_input,
                    input_format=pcm_format,
                    output_format=output_format,
                    filter_params=filter_params,
                )
            first_chunk_received = False
            bytes_sent = 0
            # Mark this player as actively streaming so audio analysis yields CPU to playback
            # for the duration of the transfer (see audio_analysis.playback_active).
            self._active_output_streams += 1
            try:
                # aclosing guarantees the generator (and thus the ffmpeg process chain
                # behind it) is torn down immediately when the player disconnects
                # mid-stream, instead of lingering until garbage collection finalizes
                # the abandoned generator.
                async with aclosing(audio_bytes):
                    async for chunk in audio_bytes:
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
                                # Player disconnected (unexpected) after receiving at least
                                # some data. This could indicate buffering issues, network
                                # problems, or player-specific issues.
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
            finally:
                self._active_output_streams -= 1
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
        finally:
            # Paired with on_source_selected — fires regardless of how streaming
            # ended (normal completion, client disconnect, exception). Lets
            # NAMED_PIPE plugins release ownership without depending on an
            # external session event. The stream_session_id is the same token
            # passed to on_source_selected; the provider must reject the
            # callback if it does not match the currently stored active
            # session (otherwise a stale teardown from a superseded same-queue
            # request would clear the live claim of its replacement).
            if audio_source_provider is not None and audio_source_id is not None:
                # Provider teardown failures must not break the response cycle
                # (we're already in finally for a stream that ended one way or
                # another), but they MUST surface in logs — otherwise a buggy
                # plugin leaks _in_use_by_queue forever and there is no trail.
                try:
                    await audio_source_provider.on_source_unselected(
                        audio_source_id, queue_id, stream_session_id
                    )
                except Exception:
                    self.logger.warning(
                        "on_source_unselected raised for provider %s source %s queue %s",
                        audio_source_provider.instance_id,
                        audio_source_id,
                        queue_id,
                        exc_info=True,
                    )

    async def serve_queue_flow_stream(self, request: web.Request) -> web.StreamResponse:  # noqa: PLR0915
        """Stream Queue Flow audio to player."""
        self._log_request(request)
        queue_id = request.match_info["queue_id"]
        player_id = request.match_info["player_id"]
        if not (queue := self.mass.player_queues.get(queue_id)):
            raise web.HTTPNotFound(reason=f"Unknown Queue: {queue_id}")
        session_id = request.match_info["session_id"]
        queue_data = self.mass.player_queues.queue_data(queue_id)
        if queue_data.session_id is None or session_id != queue_data.session_id:
            raise web.HTTPNotFound(reason=f"Unknown (or invalid) session: {session_id}")
        if not (player := self.mass.players.get_player(player_id)):
            raise web.HTTPNotFound(reason=f"Unknown Player: {player_id}")
        start_queue_item_id = request.match_info["queue_item_id"]
        start_queue_item = self.mass.player_queues.get_item(queue_id, start_queue_item_id)
        if not start_queue_item:
            raise web.HTTPNotFound(reason=f"Unknown Queue item: {start_queue_item_id}")

        # select the PCM format for the flow stream, anchored on the first track
        crossfade_mode = (
            self.get_crossfade_mode(queue)
            if start_queue_item.media_type == MediaType.TRACK
            else CrossfadeMode.DISABLED
        )
        flow_pcm_format = await self.audio.select_flow_pcm_format(
            player,
            start_streamdetails=start_queue_item.streamdetails,
            crossfade_enabled=crossfade_mode != CrossfadeMode.DISABLED,
            overlay_active=overlay_active(queue),
        )

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

        # prepare request, add some DLNA/UPNP compatible headers.
        # icy-name (in DEFAULT_STREAM_HEADERS) is always present so players have a
        # readable stream name; the rest of the ICY/shoutcast metadata headers are
        # only advertised when the client actually requested ICY metadata, rather
        # than on every flow response.
        headers = {
            **DEFAULT_STREAM_HEADERS,
            **(ICY_HEADERS if enable_icy else {}),
            "contentFeatures.dlna.org": DLNA_CONTENT_FEATURES_REALTIME,
            "Content-Type": get_mime_type(output_format.output_format_str),
        }
        if enable_icy:
            headers["icy-metaint"] = str(icy_meta_interval)

        resp = web.StreamResponse(status=200, reason="OK", headers=headers)
        http_profile = player.get_config_value(CONF_HTTP_PROFILE, "default")
        if http_profile == "forced_content_length":
            # just set an insane high content length to make sure the player keeps playing
            resp.content_length = calculate_content_length(output_format, 12 * 3600)
        elif http_profile == "chunked":
            resp.enable_chunked_encoding()

        await resp.prepare(request)

        # return early if this is not a GET request
        if request.method != "GET":
            return resp

        self._update_audio_processing_context(
            queue=queue,
            queue_item=start_queue_item,
            pcm_format=flow_pcm_format,
            crossfade_mode=crossfade_mode,
            overlay_enabled=overlay_active(queue),
            session_id=session_id,
        )
        output_plan = self.audio.get_player_output_plan(
            player.player_id,
            flow_pcm_format,
            output_format,
            shared_player_ids=player.state.group_members,
            queue_id=queue_id,
            session_id=session_id,
        )

        # all checks passed, start streaming!
        # this final ffmpeg process in the chain will convert the raw, lossless PCM audio into
        # the desired output format for the player including any player specific filter params
        # such as channels mixing, DSP, resampling and, only if needed, encoding to lossy formats
        self.logger.debug("Start serving Queue flow audio stream for %s", queue.display_name)

        # Mark this player as actively streaming so audio analysis yields CPU to playback
        # for the duration of the flow stream (see audio_analysis.playback_active).
        self._active_output_streams += 1
        flow_stream = self.audio.get_queue_flow_stream(
            queue=queue,
            start_queue_item=start_queue_item,
            pcm_format=flow_pcm_format,
            session_id=session_id,
            protocol_player=player,
        )
        if overlay_active(queue):
            flow_stream = self.audio.get_overlay_mixed_stream(queue, flow_stream, flow_pcm_format)
        audio_bytes = get_ffmpeg_stream(
            audio_input=flow_stream,
            input_format=flow_pcm_format,
            output_format=output_format,
            filter_params=output_plan.filter_params,
            # we need to slowly feed the music to avoid the player stopping and later
            # restarting (or completely failing) the audio stream by keeping the buffer short.
            # this is reported to be an issue especially with Chromecast players.
            # see for example: https://github.com/music-assistant/support/issues/3717
            # allow buffer ahead of a few seconds and read rest in (near) realtime
            extra_input_args=["-readrate", "1.1", "-readrate_initial_burst", "5"],
            chunk_size=icy_meta_interval if enable_icy else calculate_content_length(output_format),
        )
        try:
            # aclosing guarantees the flow stream (and thus the ffmpeg process chain
            # behind it) is torn down immediately when the player disconnects
            # mid-stream, instead of lingering until garbage collection finalizes
            # the abandoned generator.
            async with aclosing(audio_bytes):
                async for chunk in audio_bytes:
                    try:
                        await resp.write(chunk)
                    except BrokenPipeError, ConnectionResetError, ConnectionError:
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
        finally:
            self._active_output_streams -= 1

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
        if not (player := self.mass.players.get_player(player_id)):
            raise web.HTTPNotFound(reason=f"Unknown Player: {player_id}")
        if not (announce_data := self.announcement_renderer.get_for_player(player_id)):
            raise web.HTTPNotFound(reason=f"No pending announcements for Player: {player_id}")

        # work out output format/details
        fmt = request.match_info["fmt"]
        audio_format = AudioFormat(content_type=ContentType.try_parse(fmt))

        http_profile = self._get_announcement_http_profile(player_id, announce_data)

        # return early if this is not a GET request:
        # players often probe the url with a HEAD request before fetching it and
        # rendering the announcement for such a probe would run the entire (costly)
        # TTS/ffmpeg chain twice for a single announcement.
        if request.method != "GET":
            resp = web.StreamResponse(status=200, reason="OK", headers=DEFAULT_STREAM_HEADERS)
            resp.content_type = get_mime_type(audio_format.output_format_str)
            if http_profile == "chunked":
                resp.enable_chunked_encoding()
            await resp.prepare(request)
            return resp

        if http_profile == "forced_content_length":
            # given the fact that an announcement is just a short audio clip,
            # just send it over completely at once so we have a fixed content length
            data = bytearray()
            announcement_stream = self.get_announcement_stream(announce_data, audio_format)
            # aclosing guarantees the stream (and thus the ffmpeg process chain behind
            # it) is torn down immediately when the request is cancelled, instead of
            # lingering until garbage collection finalizes the abandoned generator.
            async with aclosing(announcement_stream):
                async for chunk in announcement_stream:
                    data += chunk
            return web.Response(
                body=bytes(data),
                content_type=get_mime_type(audio_format.output_format_str),
                headers=DEFAULT_STREAM_HEADERS,
            )

        resp = web.StreamResponse(status=200, reason="OK", headers=DEFAULT_STREAM_HEADERS)
        resp.content_type = get_mime_type(audio_format.output_format_str)
        if http_profile == "chunked":
            resp.enable_chunked_encoding()

        await resp.prepare(request)

        # all checks passed, start streaming!
        self.logger.debug(
            "Start serving audio stream for Announcement %s to %s",
            announce_data["announcement_url"],
            player.display_name,
        )
        announcement_stream = self.get_announcement_stream(announce_data, audio_format)
        # aclosing guarantees the stream (and thus the ffmpeg process chain behind
        # it) is torn down immediately when the player disconnects mid-stream,
        # instead of lingering until garbage collection finalizes the abandoned
        # generator.
        async with aclosing(announcement_stream):
            async for chunk in announcement_stream:
                try:
                    await resp.write(chunk)
                except BrokenPipeError, ConnectionResetError:
                    break

        self.logger.debug(
            "Finished serving audio stream for Announcement %s to %s",
            announce_data["announcement_url"],
            player.display_name,
        )

        return resp

    def get_command_url(self, player_or_queue_id: str, command: str) -> str:
        """Get the url for the special command stream."""
        return f"{self.base_url}/command/{player_or_queue_id}/{command}.mp3"

    def get_announcement_url(
        self,
        player_id: str,
        content_type: ContentType = ContentType.MP3,
    ) -> str:
        """
        Get the url that serves the announcement registered for the given player.

        :param player_id: The player the announcement is played on.
        :param content_type: The format to serve the announcement in.
        """
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
    ) -> AsyncGenerator[bytes]:
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
            return self.get_announcement_stream(cast("AnnounceData", media.custom_data), pcm_format)
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
            queue = self.mass.player_queues.get(queue_id)
            queue_session_id = cast(
                "str | None",
                (media.custom_data or {}).get("session_id"),
            )
            crossfade_needs_flow_mode = (
                # crossfade only applies to tracks; if the queue has it enabled but the
                # player(protocol) does not support gapless playback, we need to enforce flow mode
                media.media_type == MediaType.TRACK
                and queue is not None
                and queue.crossfade_enabled
                and protocol_player
                and not protocol_player.supports_gapless
            )
            # the audio overlay is mixed into the queue's continuous (flow) stream;
            # per-item requests would restart the overlay at every track boundary
            overlay_needs_flow_mode = queue is not None and overlay_active(queue)
            flow_mode = (
                force_flow_mode
                or (protocol_player is not None and protocol_player.flow_mode)
                or crossfade_needs_flow_mode
                or overlay_needs_flow_mode
            )
            if media.media_type in (MediaType.RADIO, MediaType.AUDIO_SOURCE):
                # flow_mode for live/infinite streams is pointless
                flow_mode = False
            if flow_mode:
                # flow stream request
                assert queue
                start_queue_item = self.mass.player_queues.get_item(
                    media.source_id, media.queue_item_id
                )
                assert start_queue_item
                crossfade_mode = (
                    self.get_crossfade_mode(queue)
                    if start_queue_item.media_type == MediaType.TRACK
                    else CrossfadeMode.DISABLED
                )
                self._update_audio_processing_context(
                    queue=queue,
                    queue_item=start_queue_item,
                    pcm_format=pcm_format,
                    crossfade_mode=crossfade_mode,
                    overlay_enabled=overlay_active(queue),
                    session_id=queue_session_id,
                )
                flow_stream = self.audio.get_queue_flow_stream(
                    queue=queue,
                    start_queue_item=start_queue_item,
                    pcm_format=pcm_format,
                    session_id=queue_session_id,
                    protocol_player=protocol_player,
                )
                if overlay_active(queue):
                    flow_stream = self.audio.get_overlay_mixed_stream(
                        queue, flow_stream, pcm_format
                    )
                if use_flow_stream_buffering:
                    return buffered(flow_stream, buffer_size=30, min_buffer_before_yield=1)
                return flow_stream
            # single item stream (e.g. radio or non-flow mode)
            queue_item = self.mass.player_queues.get_item(media.source_id, media.queue_item_id)
            assert queue_item
            if queue is not None:
                self._update_audio_processing_context(
                    queue=queue,
                    queue_item=queue_item,
                    pcm_format=pcm_format,
                    crossfade_mode=(
                        self.get_crossfade_mode(queue)
                        if queue_item.media_type == MediaType.TRACK
                        else CrossfadeMode.DISABLED
                    ),
                    overlay_enabled=(
                        queue_item.media_type == MediaType.RADIO and overlay_active(queue)
                    ),
                    session_id=queue_session_id,
                )
            inner_stream = self.audio.get_queue_item_stream(
                queue_item=queue_item,
                pcm_format=pcm_format,
                playback_speed=cast(
                    "float", queue_item.extra_attributes.get("playback_speed", 1.0)
                ),
                session_id=queue_session_id,
            )
            if (
                queue is not None
                and queue_item.media_type == MediaType.RADIO
                and overlay_active(queue)
            ):
                # radio plays as a single long-lived stream, so mix the overlay in here
                inner_stream = self.audio.get_overlay_mixed_stream(queue, inner_stream, pcm_format)
            # mirror the on_source_selected/unselected lifecycle the HTTP route
            # fires, so direct-PCM consumers (AirPlay, Snapcast, UGP) honour the
            # plugin contract too
            if (
                queue_item.media_item is not None
                and queue_item.media_item.media_type == MediaType.AUDIO_SOURCE
            ):
                return self._wrap_with_audio_source_lifecycle(
                    inner=inner_stream,
                    queue_item=queue_item,
                    player_id=player_id or media.source_id,
                )
            return inner_stream
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
    ) -> AsyncGenerator[bytes]:
        """Create a 30 seconds preview audioclip for the given media item."""
        if not (music_prov := self.mass.get_provider(provider_instance_id_or_domain)):
            raise ProviderUnavailableError
        if music_prov.type != ProviderType.MUSIC:
            msg = f"{provider_instance_id_or_domain} is not a music provider"
            raise InvalidDataError(msg)
        music_prov = cast("MusicProvider", music_prov)

        try:
            await self.mass.music.get_item(
                media_type,
                item_id,
                provider_instance_id_or_domain,
                allow_update_metadata=False,
            )
        except MediaNotFoundError as err:
            msg = f"Item {item_id} not found in provider {provider_instance_id_or_domain}"
            raise InvalidDataError(msg) from err

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
        self, announce_data: AnnounceData, output_format: AudioFormat
    ) -> AsyncGenerator[bytes]:
        """
        Get the audio of an announcement (pre-announce chime + announcement).

        Any number of consumers may stream the same announcement at once; its source is
        fetched and decoded only once. The audio stays available while the stream is
        held open.

        :param announce_data: The announcement to stream.
        :param output_format: The format to deliver the audio in.
        """
        render = self.announcement_renderer.acquire(announce_data)
        try:
            # aclosing guarantees this consumer's ffmpeg encoder is torn down
            # immediately when it goes away, instead of lingering until garbage
            # collection finalizes the abandoned generator.
            stream = render.get_stream(output_format)
            async with aclosing(stream):
                async for chunk in stream:
                    yield chunk
        finally:
            await self.announcement_renderer.release(render)

    async def get_announcement_duration(
        self, announcement: PlayerMedia, timeout: float = DEFAULT_RENDER_TIMEOUT
    ) -> int | None:
        """
        Get the exact duration (in seconds) of an announcement, once it finished rendering.

        Waits for the audio to be rendered in full, so call this while the announcement
        plays rather than before handing it to a player. Returns None when the length can
        not be determined, e.g. the announcement is no longer playing or its source did
        not deliver in time.

        :param announcement: The announcement to return the duration for.
        :param timeout: Maximum time to wait for the audio to finish rendering.
        """
        if announcement.duration:
            return announcement.duration
        if not announcement.custom_data:
            return None
        render = self.announcement_renderer.get(cast("AnnounceData", announcement.custom_data))
        if render is None:
            return None
        duration = await render.wait_finished(timeout)
        return ceil(duration) if duration else None

    async def _wrap_with_audio_source_lifecycle(
        self,
        inner: AsyncGenerator[bytes],
        queue_item: QueueItem,
        player_id: str,
    ) -> AsyncGenerator[bytes]:
        """
        Wrap an AudioSource queue item stream with on_source_selected/unselected hooks.

        Direct-PCM consumers (AirPlay, Snapcast, UGP, ...) call ``get_stream`` instead
        of going through the HTTP route, but the plugin contract requires the
        lifecycle hooks to fire for every actual stream request — they're what
        claim/release the per-queue exclusive ownership and trigger acquisition
        side effects like the Spotify Connect Web API play kick. This wrapper
        gives those consumers the same lifecycle the HTTP route already provides.

        :param inner: The underlying audio stream generator.
        :param queue_item: The AudioSource queue item being streamed.
        :param player_id: The protocol player consuming this stream.
        """
        media_item = queue_item.media_item
        assert media_item is not None  # caller checked media_type == AUDIO_SOURCE
        prov = self.mass.get_provider(media_item.provider)
        queue_id = queue_item.queue_id
        if not isinstance(prov, PluginProvider):
            async for chunk in inner:
                yield chunk
            return
        source_id = media_item.item_id
        stream_session_id = uuid4().hex
        # single try/finally so on_source_unselected fires even when
        # on_source_selected raises after partially claiming state; the
        # provider's session_id guard makes a no-op claim release safe.
        try:
            try:
                await prov.on_source_selected(source_id, player_id, queue_id, stream_session_id)
            except RuntimeError as err:
                # provider intentionally aborts the request — surface as AudioError
                raise AudioError(str(err)) from err
            async for chunk in inner:
                yield chunk
        finally:
            try:
                await prov.on_source_unselected(source_id, queue_id, stream_session_id)
            except Exception as err:
                self.logger.exception(
                    "on_source_unselected raised for provider %s source %s queue %s: %s",
                    prov.instance_id,
                    source_id,
                    queue_id,
                    err,
                )

    def _update_audio_processing_context(
        self,
        queue: PlayerQueue,
        queue_item: QueueItem,
        pcm_format: AudioFormat,
        crossfade_mode: CrossfadeMode,
        overlay_enabled: bool,
        session_id: str | None = None,
    ) -> None:
        """
        Store the shared processing context selected for a queue item.

        :param queue: Active player queue.
        :param queue_item: Queue item being prepared.
        :param pcm_format: Shared PCM format leaving queue processing.
        :param crossfade_mode: Effective crossfade mode for the item.
        :param overlay_enabled: Whether an overlay is mixed into this stream.
        :param session_id: Queue session that owns processing-detail updates.
        """
        if queue_item.streamdetails is None:
            return
        queue_data = self.mass.player_queues.queue_data_or_none(queue.queue_id)
        if (
            queue_data is None
            or (processing_session_id := session_id or queue_data.session_id) is None
            or queue_data.session_id != processing_session_id
        ):
            return
        self.audio_processing.start_session(queue.queue_id, processing_session_id)
        self.audio_processing.update_item_context(
            queue_id=queue.queue_id,
            session_id=processing_session_id,
            queue_item_id=queue_item.queue_item_id,
            queue_processing=AudioQueueProcessing(
                pcm_format=pcm_format,
                playback_speed=cast(
                    "float",
                    queue_item.extra_attributes.get("playback_speed", 1.0),
                ),
                crossfade_mode=crossfade_mode,
                overlay_active=overlay_enabled,
            ),
            alters_audio=queue_item.streamdetails.fade_in,
        )

    def _get_announcement_http_profile(self, player_id: str, announce_data: AnnounceData) -> str:
        """
        Resolve the http profile for serving an announcement stream.

        Announcement urls are registered under the visible player's id, but the
        stream may be fetched by a linked protocol player; the profile must come
        from the player that actually performs the fetch.
        """
        announce_player = None
        if announce_player_id := announce_data.get("announce_player_id"):
            announce_player = self.mass.players.get_player(announce_player_id)
        if announce_player is None:
            announce_player = self.mass.players.get_player(player_id)
        if announce_player is None:
            return "default"
        return announce_player.get_output_config_value(CONF_HTTP_PROFILE, "default")

    def _log_request(self, request: web.Request) -> None:
        """Log request."""
        if self.logger.isEnabledFor(VERBOSE_LOG_LEVEL):
            self.logger.log(
                VERBOSE_LOG_LEVEL,
                "Got %s request to %s from %s\nheaders: %s\n",
                request.method,
                request.path,
                request.remote,
                redact_sensitive_headers(request.headers),
            )
        else:
            self.logger.debug(
                "Got %s request to %s from %s", request.method, request.path, request.remote
            )

    async def _reload_network_dependent_providers(self) -> None:
        """Reload the providers that captured the streamserver network, if it changed."""
        previous = self._network_fingerprint
        current = (
            self._bind_ip,
            str(self.publish_ip),
            cast("int", self.publish_port),
            tuple(self.publish_addresses),
        )
        if previous is None or previous == current:
            self._network_fingerprint = current
            return
        # these providers bind or advertise the network while they load, so a plain
        # reload is what moves them over - they share no lighter rebind path
        instance_ids = [
            prov.instance_id
            for prov in self.mass.providers
            if prov.reload_on_streams_network_change
        ]
        for instance_id in instance_ids:
            try:
                config = await self.mass.config.get_provider_config(instance_id)
                self.logger.info(
                    "Streamserver network changed, reloading provider %s",
                    config.name or config.domain,
                )
                await self.mass.load_provider_config(config)
            except Exception as err:
                self.logger.warning(
                    "Error reloading provider %s: %s",
                    instance_id,
                    str(err) or err.__class__.__name__,
                    exc_info=err,
                )
        # only mark the new network as applied once the loop completed, so a run cut short
        # by a second config change runs again on the next reload
        self._network_fingerprint = current

    def _setup_smart_fades_logger(self, config: CoreConfig) -> None:
        """Set up smart fades logger level."""
        log_level = str(config.get_value(CONF_SMART_FADES_LOG_LEVEL))
        if log_level == "GLOBAL":
            self.audio.smart_fades_mixer.logger.setLevel(self.logger.level)
        else:
            self.audio.smart_fades_mixer.logger.setLevel(log_level)

    def _resolve_publish_state(self, bind_ip: str, all_ip_addresses: tuple[str, ...]) -> None:
        """
        Resolve the addresses and base URL to advertise for the given bind address.

        Reads ``self.publish_port``, so set that first.

        :param bind_ip: Address the streamserver binds to (a wildcard means all interfaces).
        :param all_ip_addresses: All detected host IP addresses, in ranked order.
        """
        self._bind_ip = bind_ip
        if self._configured_publish_ip:
            self.publish_ip = self._configured_publish_ip
        elif bind_ip and bind_ip not in WILDCARD_BIND_IPS:
            # only this interface serves the stream, so no other address can reach it
            self.publish_ip = bind_ip
        else:
            self.publish_ip = all_ip_addresses[0]
        self.publish_addresses = _get_publish_addresses(
            bind_ip, self._configured_publish_ip, all_ip_addresses
        )
        self._base_url = f"http://{format_ip_for_url(self.publish_ip)}:{self.publish_port}"


def _same_ip_family(ip: str, other_ip: str) -> bool:
    """Return whether two addresses belong to the same IP family."""
    return (":" in ip) == (":" in other_ip)
