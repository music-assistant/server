"""
Audio streaming helpers that interact with core controllers and providers.

This module contains all audio stream acquisition and processing functions
that need access to the MusicAssistant instance. Generic audio utilities
that do not need controller interaction live in helpers/audio.py.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections.abc import AsyncGenerator, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlparse

import aiofiles
import aiofiles.os
import aiohttp
import shortuuid
from aiohttp import ClientConnectorSSLError, ClientResponseError, ClientTimeout
from music_assistant_models.audio_processing import (
    AudioDSPDetails,
    AudioOutputDetails,
    AudioQueueProcessing,
)
from music_assistant_models.dsp import (
    AudioChannel,
    ConvolutionFilter,
    DSPConfig,
    DSPFilter,
    DSPState,
)
from music_assistant_models.enums import (
    ContentType,
    CrossfadeMode,
    MediaType,
    PlayerFeature,
    ProviderType,
    StreamType,
    VolumeNormalizationMode,
)
from music_assistant_models.errors import (
    AudioError,
    InvalidDataError,
    MediaNotFoundError,
    MusicAssistantError,
    ProviderPermissionDenied,
    ProviderUnavailableError,
    QueueEmpty,
    RetriesExhausted,
)
from music_assistant_models.media_items import AudioFormat, Track
from music_assistant_models.player_queue import PlayLogEntry
from music_assistant_models.streamdetails import MultiPartPath, StreamMetadata

from music_assistant.constants import (
    CONF_CROSSFADE_DURATION,
    CONF_ENTRY_CROSSFADE_DIFFERENT_SAMPLE_RATES,
    CONF_ENTRY_VOLUME_NORMALIZATION_TARGET,
    CONF_FLOW_MODE_SAMPLE_RATE,
    CONF_OUTPUT_CHANNELS,
    CONF_PLAYER_QUEUES,
    CONF_VALUE_DISABLED,
    CONF_VALUE_ENABLED,
    CONF_VOLUME_NORMALIZATION,
    CONF_VOLUME_NORMALIZATION_FIXED_GAIN_RADIO,
    CONF_VOLUME_NORMALIZATION_FIXED_GAIN_TRACKS,
    CONF_VOLUME_NORMALIZATION_RADIO,
    CONF_VOLUME_NORMALIZATION_TARGET,
    CONF_VOLUME_NORMALIZATION_TRACKS,
    DSP_IRS_DIRNAME,
    FLOW_MODE_SAMPLE_RATE_48000,
    FLOW_MODE_SAMPLE_RATE_96000,
    FLOW_MODE_SAMPLE_RATE_BIT_PERFECT,
    FLOW_MODE_SAMPLE_RATE_HIGHEST,
    FLOW_MODE_SAMPLE_RATE_SMART,
    INTERNAL_PCM_FORMAT,
    MASS_LOGGER_NAME,
    STREAM_STALL_TIMEOUT,
    STREAM_START_TIMEOUT,
    VERBOSE_LOG_LEVEL,
)
from music_assistant.controllers.streams.audio_analysis import (
    LOUDNESS_ANALYSIS_DOMAIN,
)
from music_assistant.controllers.streams.audio_buffer import AudioBuffer
from music_assistant.controllers.streams.audio_processing import (
    AudioOutputPlan,
    get_normalization_details,
)
from music_assistant.controllers.streams.constants import (
    CACHE_CATEGORY_RESOLVED_RADIO_URL,
    CACHE_PROVIDER,
    CONF_ALLOW_CROSSFADE_SAME_ALBUM,
)
from music_assistant.controllers.streams.ogg_handler import get_chained_ogg_stream
from music_assistant.controllers.streams.smart_fades import SmartFadesMixer
from music_assistant.controllers.streams.smart_fades.fades import SmartFade
from music_assistant.controllers.streams.smart_fades.helpers import SMART_CROSSFADE_DURATION
from music_assistant.helpers import ssl as ssl_util
from music_assistant.helpers.aiohttp_client import encoded_request_url
from music_assistant.helpers.audio import (
    HTTP_HEADERS,
    HTTP_HEADERS_ICY,
    build_concat_filelist,
    calculate_content_length,
    get_bit_rate,
    get_normalization_mode,
    get_parts_from_position,
    is_grouping_preventing_dsp,
    iter_pcm_slices,
    parse_extinf_metadata,
    realtime_pcm_pacer,
    resample_pcm_audio,
    resolve_output_player_ids,
)
from music_assistant.helpers.dsp import ComplexFilter, filter_to_ffmpeg_params
from music_assistant.helpers.ffmpeg import (
    FFMpeg,
    get_ffmpeg_overlay_stream,
    get_ffmpeg_stream,
)
from music_assistant.helpers.named_pipe import read_named_pipe
from music_assistant.helpers.playlists import IsHLSPlaylist, PlaylistItem, fetch_playlist, parse_m3u
from music_assistant.helpers.throttle_retry import BYPASS_THROTTLER
from music_assistant.helpers.util import (
    clean_stream_title,
    detect_charset,
    parse_quoted_stream_title,
    parse_title_and_version,
    remove_file,
)

if TYPE_CHECKING:
    from music_assistant_models.player_queue import PlayerQueue
    from music_assistant_models.queue_item import QueueItem
    from music_assistant_models.streamdetails import StreamDetails

    from music_assistant.mass import MusicAssistant
    from music_assistant.models.music_provider import MusicProvider
    from music_assistant.models.player import Player
    from music_assistant.models.plugin import PluginProvider

# ruff: noqa: PLR0915

# Seconds of PCM yielded directly to the player before the crossfade holdback starts buffering.
WARMUP_DURATION = 8

# Chunk size for the realtime AudioSource path; small enough to keep ffmpeg→consumer
# latency below ~50 ms while still amortising per-chunk overhead.
AUDIO_SOURCE_CHUNK_SECONDS = 0.02

# Terminal errors get_icy_radio_stream raises once a single mirror is exhausted; the
# multi-mirror reader treats these as the signal to fail over to the next URL.
RADIO_MIRROR_FAILOVER_ERRORS = (
    MediaNotFoundError,
    ProviderPermissionDenied,
    ProviderUnavailableError,
    RetriesExhausted,
    InvalidDataError,
)


@dataclass
class CrossfadeData:
    """Data class to hold crossfade data."""

    data: bytes
    fade_in_size: int
    pcm_format: AudioFormat  # Format of the 'data' bytes (current/previous track's format)
    fade_in_pcm_format: AudioFormat  # Format for 'fade_in_size' (next track's format)
    queue_item_id: str
    # Offset for the fade_in track's elapsed time calculation, to account for crossfade duration and trim
    elapsed_time_offset: float = 0.0
    # Normalization mode the intro PCM was baked with, used to pin the next track's body to the same mode
    normalization_mode: VolumeNormalizationMode | None = None


def _snap_supported_rate_up(target: int, supported_sample_rates: list[int]) -> int:
    """Snap target up, falling back to its highest supported divisor or the maximum."""
    if target in supported_sample_rates:
        return target
    higher = [r for r in supported_sample_rates if r > target]
    if higher:
        return min(higher)
    same_family = [r for r in supported_sample_rates if target % r == 0]
    return max(same_family) if same_family else max(supported_sample_rates)


def _snap_supported_rate_down(target: int, supported_sample_rates: list[int]) -> int:
    """Snap target down to the highest supported rate <= target, falling back to min."""
    if target in supported_sample_rates:
        return target
    lower = [r for r in supported_sample_rates if r < target]
    return max(lower) if lower else min(supported_sample_rates)


def _pcm_formats_match(a: AudioFormat, b: AudioFormat) -> bool:
    """Return True if two PCM formats describe identical raw bytes."""
    return (
        a.content_type == b.content_type
        and a.sample_rate == b.sample_rate
        and a.bit_depth == b.bit_depth
        and a.channels == b.channels
    )


def overlay_active(queue: PlayerQueue) -> bool:
    """Return True if the given queue has an audio overlay enabled and a source selected."""
    return queue.overlay_enabled and queue.overlay_source is not None


class StreamsAudio:
    """Audio stream acquisition and processing for the streams controller."""

    def __init__(self, mass: MusicAssistant) -> None:
        """
        Initialize StreamsAudio.

        :param mass: The MusicAssistant instance.
        """
        self.mass = mass
        self.logger = logging.getLogger(f"{MASS_LOGGER_NAME}.streams.audio")
        self._crossfade_data: dict[str, CrossfadeData] = {}
        self._smart_fades_mixer: SmartFadesMixer | None = None

    def setup(self) -> None:
        """Set up the audio sub-controller (called after all core controllers are created)."""
        self._smart_fades_mixer = SmartFadesMixer(self.mass.streams)

    @property
    def smart_fades_mixer(self) -> SmartFadesMixer:
        """Return the smart fades mixer."""
        assert self._smart_fades_mixer is not None, "StreamsAudio.setup() not called"
        return self._smart_fades_mixer

    # --- Public methods ---

    async def get_stream_details(
        self,
        queue_item: QueueItem,
        seek_position: int = 0,
        fade_in: bool = False,
        prefer_album_loudness: bool = False,
    ) -> StreamDetails:
        """
        Get streamdetails for the given QueueItem.

        This is called just-in-time when a PlayerQueue wants a MediaItem to be played.
        Do not try to request streamdetails too much in advance as this is expiring data.
        """
        mass = self.mass
        streamdetails: StreamDetails | None = None
        time_start = time.time()
        self.logger.debug("Getting streamdetails for %s", queue_item.uri)

        if not queue_item.media_item and not queue_item.streamdetails:
            # in case of a non-media item queue item, the streamdetails should already be provided
            # this should not happen, but guard it just in case
            raise MediaNotFoundError(
                f"Unable to retrieve streamdetails for {queue_item.name} ({queue_item.uri})"
            )

        if queue_item.streamdetails and (
            # reuse if the buffer can serve this seek position (fast seek path)
            (
                queue_item.streamdetails.buffer
                and queue_item.streamdetails.buffer.is_valid(int(seek_position * 1000))
            )
            # or reuse if streamdetails hasn't expired yet (new buffer will be created)
            or (queue_item.streamdetails.created_at + queue_item.streamdetails.expiration)
            > time.time()
        ):
            streamdetails = queue_item.streamdetails
        else:
            # need to (re)create streamdetails
            # retrieve streamdetails from provider

            media_item = queue_item.media_item
            assert media_item is not None  # for type checking
            preferred_providers: list[str] = []
            if (
                (pq_data := mass.player_queues.queue_data_or_none(queue_item.queue_id))
                and pq_data.userid
                and (playback_user := await mass.webserver.auth.get_user(pq_data.userid))
                and playback_user.provider_filter
            ):
                # handle steering into user preferred providerinstance
                preferred_providers = playback_user.provider_filter
            else:
                preferred_providers = [x.provider_instance for x in media_item.provider_mappings]
            # Remember the last AudioError so we can re-raise its (actionable)
            # message instead of the generic MediaNotFoundError below.
            last_audio_error: AudioError | None = None
            attempted: set[tuple[str, str]] = set()
            for allow_other_provider in (False, True):
                if streamdetails:
                    break
                # sort by quality and check item's availability
                for prov_media in sorted(
                    media_item.provider_mappings, key=lambda x: x.quality or 0, reverse=True
                ):
                    if not prov_media.available:
                        self.logger.debug(f"Skipping unavailable {prov_media}")
                        continue
                    if (
                        not allow_other_provider
                        and prov_media.provider_instance not in preferred_providers
                    ):
                        continue
                    # the second pass is there to widen to providers the steering held back,
                    # not to give a mapping a second chance: without a user provider filter
                    # the first pass already tried them all, so re-attempting one just buys
                    # the same failure at the cost of another provider round-trip
                    attempt = (prov_media.provider_instance, prov_media.item_id)
                    if attempt in attempted:
                        continue
                    # guard that provider is available
                    provider = mass.get_provider(prov_media.provider_instance)
                    if not provider:
                        self.logger.debug(f"Skipping {prov_media} - provider not available")
                        continue  # provider not available ?
                    attempted.add(attempt)
                    # get streamdetails from provider; music and plugin providers
                    # share this signature, so either type can own the item.
                    try:
                        BYPASS_THROTTLER.set(True)
                        stream_prov = cast("MusicProvider | PluginProvider", provider)
                        streamdetails = await stream_prov.get_stream_details(
                            prov_media.item_id, media_item.media_type
                        )
                    except AudioError as err:
                        last_audio_error = err
                        self.logger.warning(str(err))
                    except MusicAssistantError as err:
                        self.logger.warning(str(err))
                    else:
                        break
                    finally:
                        BYPASS_THROTTLER.set(False)

            if not streamdetails:
                if last_audio_error is not None:
                    raise last_audio_error
                msg = f"Unable to retrieve streamdetails for {queue_item.name} ({queue_item.uri})"
                raise MediaNotFoundError(msg)

            # work out how to handle radio stream
            if (
                streamdetails.stream_type in (StreamType.ICY, StreamType.HLS, StreamType.HTTP)
                and streamdetails.media_type == MediaType.RADIO
                and isinstance(streamdetails.path, str)
            ):
                resolved_url, stream_type = await self.resolve_radio_stream(streamdetails.path)
                streamdetails.path = resolved_url
                streamdetails.stream_type = stream_type
                # Set up metadata monitoring callback for HLS radio streams, if not already set
                if (
                    stream_type == StreamType.HLS
                    and not streamdetails.stream_metadata_update_callback
                ):
                    streamdetails.stream_metadata_update_callback = partial(
                        self._update_hls_radio_metadata
                    )
                    streamdetails.stream_metadata_update_interval = 5

        # providers report an unknown duration as either None or 0
        if not streamdetails.duration:
            if queue_item.media_item and queue_item.media_item.duration:
                streamdetails.duration = queue_item.media_item.duration
            elif queue_item.duration:
                streamdetails.duration = queue_item.duration
        if seek_position and not streamdetails.allow_seek:
            self.logger.warning("seeking is not possible on this stream!")
            seek_position = 0
        elif seek_position and not streamdetails.duration:
            self.logger.warning("seeking is not possible on duration-less streams!")
            seek_position = 0

        # set queue_id on the streamdetails so we know what is being streamed
        streamdetails.queue_id = queue_item.queue_id
        # handle skip/fade_in details
        streamdetails.seek_position = seek_position
        streamdetails.fade_in = fade_in

        streamdetails.prefer_album_loudness = prefer_album_loudness
        conf_volume_normalization_target = float(
            mass.streams.get_config_value(CONF_VOLUME_NORMALIZATION_TARGET, return_type=int)
        )
        # guard against invalid volume normalization values
        # range and default_value are guaranteed to be set for this constant
        volume_range = CONF_ENTRY_VOLUME_NORMALIZATION_TARGET.range
        assert volume_range is not None
        if (
            conf_volume_normalization_target < volume_range[0]
            or conf_volume_normalization_target >= volume_range[1]
        ):
            default_val = CONF_ENTRY_VOLUME_NORMALIZATION_TARGET.default_value
            assert isinstance(default_val, (int, float))
            conf_volume_normalization_target = float(default_val)
            self.logger.warning(
                "Invalid volume normalization target configured, resetting to default of %s LUFS",
                CONF_ENTRY_VOLUME_NORMALIZATION_TARGET.default_value,
            )
        streamdetails.target_loudness = conf_volume_normalization_target
        volume_normalization_enabled = (
            mass.config.get_effective_player_queue_config_value(
                streamdetails.queue_id, CONF_VOLUME_NORMALIZATION, CONF_VALUE_ENABLED
            )
            != CONF_VALUE_DISABLED
        )
        streamdetails.volume_normalization_mode = get_normalization_mode(
            self._get_volume_normalization_preference(streamdetails),
            volume_normalization_enabled,
            streamdetails,
        )

        self.logger.debug(
            "Retrieved streamdetails for %s in %s milliseconds",
            queue_item.uri,
            int((time.time() - time_start) * 1000),
        )
        return streamdetails

    async def get_media_stream(
        self,
        streamdetails: StreamDetails,
        pcm_format: AudioFormat,
        seek_position: int = 0,
        filter_params: list[str] | None = None,
        chunk_seconds: float = 1.0,
    ) -> AsyncGenerator[bytes]:
        """
        Get audio stream for given media details as raw PCM.

        :param streamdetails: Details of the stream to fetch.
        :param pcm_format: Target PCM format the consumer expects.
        :param seek_position: Seek offset in seconds (only honoured when the
            source allows seeking; ignored for live AudioSources).
        :param filter_params: Optional ffmpeg filter expressions.
        :param chunk_seconds: Size of each yielded chunk in seconds of audio.
            Defaults to 1 s for track-like sources; callers streaming live
            AudioSources should pass a much smaller value (e.g. 0.02) to keep
            end-to-end latency low.
        """
        mass = self.mass
        logger = self.logger.getChild("media_stream")
        logger.log(VERBOSE_LOG_LEVEL, "Starting media stream for %s", streamdetails.uri)
        # copy: the args below are appended per call, while the StreamDetails is cached on
        # the queue item and reused across calls (retry, seek, background analysis)
        extra_input_args = list(streamdetails.extra_input_args or [])
        # the branches below zero out seek_position where the seek is delegated to the
        # source itself, so keep the requested position for the duration writeback
        requested_seek_position = seek_position

        # work out audio source for these streamdetails
        audio_source: str | AsyncGenerator[bytes]
        stream_type = streamdetails.stream_type
        if stream_type == StreamType.CUSTOM:
            # MusicProvider and PluginProvider both expose get_audio_stream with the same shape
            provider = mass.get_provider(streamdetails.provider)
            if provider is None:
                raise ProviderUnavailableError(
                    f"Provider {streamdetails.provider} for stream is no longer available"
                )
            provider = cast("MusicProvider | PluginProvider", provider)
            audio_source = provider.get_audio_stream(
                streamdetails, seek_position=seek_position if streamdetails.can_seek else 0
            )
            seek_position = 0 if streamdetails.can_seek else seek_position
        elif stream_type == StreamType.ICY:
            assert streamdetails.path is not None
            assert isinstance(streamdetails.path, (str, list))
            audio_source = self.get_reconnecting_icy_radio_stream(streamdetails.path, streamdetails)
            seek_position = 0
        elif stream_type == StreamType.SHOUTCAST:
            assert isinstance(streamdetails.path, str)
            audio_source = self.get_shoutcast_stream(streamdetails.path, streamdetails)
            seek_position = 0
        elif stream_type == StreamType.IN_BAND:
            assert isinstance(streamdetails.path, str)  # for type checking

            # For IN_BAND (OGG/Opus) radio streams, use chained OGG handler.
            # This handles the chained OGG format by stitching logical bitstreams together
            # so FFmpeg sees a single continuous stream. Metadata is extracted in-band.
            def _on_inband_metadata(metadata: dict[str, str]) -> None:
                """Handle metadata extracted from the OGG stream."""
                title = metadata.get("title", "")
                artist = metadata.get("artist", "")
                album = metadata.get("album", "")
                if not artist and " - " in title:
                    artist, title = title.split(" - ", 1)
                if title or artist:
                    if artist and title:
                        stream_title = f"{artist} - {title}"
                    elif title:
                        stream_title = title
                    else:
                        stream_title = artist
                    cleaned_title = clean_stream_title(stream_title)
                    if cleaned_title and cleaned_title != streamdetails.stream_title:
                        self.logger.log(VERBOSE_LOG_LEVEL, "In-band metadata: %s", cleaned_title)
                        streamdetails.stream_title = cleaned_title
                        self._update_radio_stream_metadata(
                            streamdetails,
                            artist=artist or None,
                            title=title or cleaned_title,
                            album=album or None,
                        )

            audio_source = get_chained_ogg_stream(
                mass, streamdetails.path, metadata_callback=_on_inband_metadata
            )
            seek_position = 0  # seeking not possible on radio streams
        elif stream_type == StreamType.HLS:
            assert isinstance(streamdetails.path, str)  # for type checking
            substream = await self.get_hls_substream(streamdetails.path)
            audio_source = substream.path
            if streamdetails.media_type == MediaType.RADIO:
                # HLS streams (especially the BBC) struggle when they're played directly
                # with ffmpeg, where they just stop after some minutes,
                # so we tell ffmpeg to loop around in this case.
                extra_input_args += ["-stream_loop", "-1", "-re"]
        else:
            # all other stream types (HTTP, FILE, etc)
            if stream_type == StreamType.ENCRYPTED_HTTP:
                assert streamdetails.decryption_key is not None  # for type checking
                extra_input_args += ["-decryption_key", streamdetails.decryption_key]
            if isinstance(streamdetails.path, list):
                # multi part stream
                audio_source = self.get_multi_file_stream(streamdetails, seek_position)
                seek_position = 0  # handled by get_multi_file_stream
            else:
                # regular single file/url stream
                assert isinstance(streamdetails.path, str)  # for type checking
                audio_source = streamdetails.path

        # pace ffmpeg at native rate for live sources; the producer (e.g.
        # librespot's pipe backend) may otherwise write faster than realtime
        if streamdetails.media_type == MediaType.AUDIO_SOURCE and "-re" not in extra_input_args:
            extra_input_args += ["-re"]

        # handle seek support
        if seek_position and streamdetails.duration and streamdetails.allow_seek:
            extra_input_args += ["-ss", str(int(seek_position))]

        bytes_sent = 0
        finished = False
        cancelled = False
        first_chunk_received = False
        ffmpeg_loglevel = "debug" if self.logger.isEnabledFor(VERBOSE_LOG_LEVEL) else "info"
        # When a provider hands us already-decoded audio (e.g. Spotify Connect /
        # AirPlay receivers piping PCM after their own decode), audio_format is
        # the original source format meant for display while decoded_audio_format
        # is what ffmpeg actually needs to read off the wire.
        ffmpeg_input_format = streamdetails.decoded_audio_format or streamdetails.audio_format
        ffmpeg_proc = FFMpeg(
            audio_input=audio_source,
            input_format=ffmpeg_input_format,
            output_format=pcm_format,
            filter_params=filter_params,
            extra_input_args=extra_input_args,
            collect_log_history=True,
            loglevel=ffmpeg_loglevel,
        )

        try:
            await ffmpeg_proc.start()
            assert ffmpeg_proc.proc is not None  # for type checking
            if logger.isEnabledFor(VERBOSE_LOG_LEVEL):
                logger.log(
                    VERBOSE_LOG_LEVEL,
                    "Started media stream for %s - using streamtype: %s "
                    "- pcm format: %s - ffmpeg PID: %s",
                    streamdetails.uri,
                    streamdetails.stream_type,
                    pcm_format.content_type.value,
                    ffmpeg_proc.proc.pid,
                )
            else:
                logger.debug(
                    "Started media stream for %s - using streamtype: %s",
                    streamdetails.uri,
                    streamdetails.stream_type,
                )
            stream_start = mass.loop.time()
            chunk_size = calculate_content_length(pcm_format, chunk_seconds)
            chunk_iter = ffmpeg_proc.iter_chunked(chunk_size)
            while True:
                # Time the read, not the yield: catches a stalled source, ignores backpressure.
                read_timeout = (
                    STREAM_START_TIMEOUT if not first_chunk_received else STREAM_STALL_TIMEOUT
                )
                try:
                    async with asyncio.timeout(read_timeout):
                        chunk = await anext(chunk_iter)
                except StopAsyncIteration:
                    break
                except TimeoutError as err:
                    raise AudioError(f"Source stalled: no audio for {read_timeout}s") from err
                if not first_chunk_received:
                    # At this point ffmpeg has started and should now know the codec used
                    # for encoding the audio.
                    # Note: ffmpeg_proc.input_format is the same object as
                    # ffmpeg_input_format, so sample_rate / bit_depth / bit_rate
                    # parsed from the ffmpeg log already live on streamdetails too.
                    first_chunk_received = True
                    # Skip the codec_type writeback when the provider declared a
                    # decoded format: audio_format already holds the authoritative
                    # source codec and the probed value would just be the
                    # post-decode wire format (e.g. PCM for Spotify Connect).
                    if streamdetails.decoded_audio_format is None:
                        streamdetails.audio_format.codec_type = ffmpeg_proc.input_format.codec_type
                    # Some providers omit (or report 0 for) the item duration; ffmpeg can
                    # usually probe it from the source. Only apply when missing so we
                    # don't clobber an accurate provider value with a rounded one.
                    if ffmpeg_proc.parsed_duration is not None and not streamdetails.duration:
                        streamdetails.duration = ffmpeg_proc.parsed_duration
                    logger.debug(
                        "First chunk received after %.2f seconds (codec detected: %s)",
                        mass.loop.time() - stream_start,
                        ffmpeg_proc.input_format.codec_type,
                    )
                yield chunk
                bytes_sent += len(chunk)

            # end of audio/track reached
            logger.debug("End of media stream reached for %s", streamdetails.uri)
            # wait until stderr also completed reading
            await ffmpeg_proc.wait_with_timeout(5)
            logger.log(
                VERBOSE_LOG_LEVEL,
                "FFmpeg process ended with return code %s for %s",
                ffmpeg_proc.returncode,
                streamdetails.uri,
            )
            if ffmpeg_proc.returncode not in (0, None):
                log_trail = "\n".join(list(ffmpeg_proc.log_history)[-5:])
                raise AudioError(f"FFMpeg exited with code {ffmpeg_proc.returncode}: {log_trail}")
            if bytes_sent == 0:
                # edge case: no audio data was received at all
                raise AudioError("No audio was received")
            finished = True
        except (Exception, GeneratorExit, asyncio.CancelledError) as err:
            if isinstance(err, asyncio.CancelledError | GeneratorExit):
                # we were cancelled, just raise
                cancelled = True
                raise
            # dump the last 10 lines of the log in case of an unclean exit
            logger.warning("\n".join(list(ffmpeg_proc.log_history)[-10:]))
            raise AudioError(f"Error while streaming: {err}") from err
        finally:
            # always ensure close is called which also handles all cleanup
            await ffmpeg_proc.close()
            # determine how many seconds we've received
            # for pcm output we can calculate this easily
            seconds_received = bytes_sent / pcm_format.pcm_sample_size if bytes_sent else 0
            # store accurate duration, but only for a playthrough from the very start:
            # a seeked stream yields the remaining audio, not the item's full length
            if finished and not requested_seek_position and seconds_received:
                streamdetails.duration = int(seconds_received)

            logger.log(
                VERBOSE_LOG_LEVEL,
                "stream %s (with code %s) for %s",
                "cancelled" if cancelled else "finished" if finished else "aborted",
                ffmpeg_proc.returncode,
                streamdetails.uri,
            )

    async def resolve_radio_stream(self, url: str) -> tuple[str, StreamType]:
        """
        Resolve a streaming radio URL.

        Unwraps playlists and determines stream type (ICY, HLS, SHOUTCAST, IN_BAND, HTTP).

        :param url: Radio stream URL to resolve
        """
        mass = self.mass
        if cache := await mass.cache.get(
            key=url, provider=CACHE_PROVIDER, category=CACHE_CATEGORY_RESOLVED_RADIO_URL
        ):
            if TYPE_CHECKING:
                cache = cast("tuple[str, str]", cache)
            return (cache[0], StreamType(cache[1]))

        stream_type = StreamType.HTTP
        timeout = ClientTimeout(total=None, connect=10, sock_read=5)

        try:
            async with self._connect_radio_stream(
                url, headers=HTTP_HEADERS_ICY, allow_redirects=True, timeout=timeout
            ) as resp:
                headers = resp.headers
                resp.raise_for_status()
                if not resp.headers:
                    raise InvalidDataError("no headers found")

            if headers.get("icy-metaint") is not None:
                stream_type = StreamType.ICY
            elif headers.get("content-type", "") in ("application/ogg", "audio/ogg"):
                # Ogg streams (Opus/Vorbis) have in-band metadata via Vorbis comments
                stream_type = StreamType.IN_BAND

            if (
                url.endswith((".m3u", ".m3u8", ".pls"))
                or ".m3u?" in url
                or ".m3u8?" in url
                or ".pls?" in url
                or "audio/x-mpegurl" in headers.get("content-type", "")
                or "audio/x-scpls" in headers.get("content-type", "")
            ):
                try:
                    substreams = await fetch_playlist(mass, url)
                    if not any(x for x in substreams if x.length):
                        for line in substreams:
                            if not line.is_url:
                                continue
                            return await self.resolve_radio_stream(line.path)
                        raise InvalidDataError("No content found in playlist")
                except IsHLSPlaylist:
                    stream_type = StreamType.HLS

        except TimeoutError as err:
            self.logger.warning("Timeout while parsing radio URL %s", url)
            raise InvalidDataError(f"Timeout connecting to {url}") from err

        except aiohttp.ClientResponseError as err:
            if err.status == 404:
                raise MediaNotFoundError(f"Radio stream not found: {url}") from err
            if err.status == 403:
                raise InvalidDataError(f"Access denied to radio stream: {url}") from err
            if err.status >= 500:
                raise InvalidDataError(
                    f"Radio stream server error (HTTP {err.status}): {url}"
                ) from err
            if err.status == 400:
                # 400 errors might be from legacy Shoutcast servers
                return await self._handle_client_error_for_radio_stream(url, err, stream_type)
            raise InvalidDataError(f"HTTP error {err.status} from {url}") from err

        except aiohttp.ClientError as err:
            return await self._handle_client_error_for_radio_stream(url, err, stream_type)

        return await self._cache_radio_result(url, stream_type)

    async def get_icy_radio_stream(
        self, url: str, streamdetails: StreamDetails
    ) -> AsyncGenerator[bytes]:
        """
        Stream radio audio with ICY metadata support, reconnecting on disconnect.

        Requires icy-metaint header support. Stream type should be validated
        by resolve_radio_stream() before calling this function.

        :param url: Radio stream URL
        :param streamdetails: StreamDetails to update with metadata
        """
        self.logger.debug("Start streaming radio with ICY metadata from url %s", url)
        timeout = ClientTimeout(total=0, connect=30, sock_read=5 * 60)
        # Budget for *consecutive* reconnects that delivered no audio. A connection
        # that actually streamed data resets it, so a healthy long-running stream can
        # reconnect indefinitely while a dead/looping one bails out instead of spinning.
        failed_reconnects = 0
        max_failed_reconnects = 25

        while True:
            streamed_data = False
            try:
                async with self._connect_radio_stream(
                    url, allow_redirects=True, headers=HTTP_HEADERS_ICY, timeout=timeout
                ) as resp:
                    # surface a non-200 (e.g. on reconnect) as a ClientResponseError so the
                    # terminal/HTTP handling below applies instead of failing on the header
                    resp.raise_for_status()
                    meta_int_str = resp.headers.get("icy-metaint")
                    if not meta_int_str:
                        raise InvalidDataError(f"No icy-metaint header for radio stream: {url}")
                    try:
                        meta_int = int(meta_int_str)
                    except ValueError as err:
                        raise InvalidDataError(
                            f"Invalid icy-metaint value for radio stream: {url}"
                        ) from err
                    if meta_int <= 0:
                        raise InvalidDataError(f"Invalid icy-metaint value for radio stream: {url}")
                    # readexactly raises IncompleteReadError when the server closes the
                    # connection mid-frame; that (and the network errors below) drops us
                    # out to the reconnect handler so a live stream survives the blip.
                    while True:
                        chunk = await resp.content.readexactly(meta_int)
                        streamed_data = True
                        yield chunk
                        meta_byte = await resp.content.readexactly(1)
                        if meta_byte == b"\x00":
                            continue
                        meta_length = ord(meta_byte) * 16
                        meta_data = await resp.content.readexactly(meta_length)
                        self._parse_icy_metadata(meta_data, streamdetails)
            except asyncio.CancelledError:
                self.logger.debug("ICY radio stream cancelled for %s", url)
                raise
            except aiohttp.ClientResponseError as err:
                if err.status == 404:
                    raise MediaNotFoundError(f"Radio stream not found: {url}") from err
                if err.status == 403:
                    raise ProviderPermissionDenied(f"Radio stream access denied: {url}") from err
                raise ProviderUnavailableError(
                    f"Radio stream returned HTTP {err.status}: {err}"
                ) from err
            except (
                asyncio.IncompleteReadError,
                aiohttp.ClientConnectionError,
                aiohttp.ClientPayloadError,
                aiohttp.ServerDisconnectedError,
            ) as err:
                if streamed_data:
                    # a healthy session that dropped - reconnect without spending budget
                    failed_reconnects = 0
                    self.logger.debug("ICY radio stream dropped, reconnecting: %s", err)
                else:
                    failed_reconnects += 1
                    if failed_reconnects > max_failed_reconnects:
                        raise RetriesExhausted(
                            f"ICY radio stream failed after {max_failed_reconnects} "
                            f"reconnects without data: {err}"
                        ) from err
                    self.logger.warning(
                        "ICY radio stream reconnect produced no data (%d/%d): %s",
                        failed_reconnects,
                        max_failed_reconnects,
                        err,
                    )
                await asyncio.sleep(0.5)

    async def get_reconnecting_icy_radio_stream(
        self, url: str | list[MultiPartPath], streamdetails: StreamDetails
    ) -> AsyncGenerator[bytes]:
        """
        Yield ICY radio audio with metadata, failing over across mirror URLs.

        A single URL is delegated to :meth:`get_icy_radio_stream`, which already reconnects
        on disconnect. Multiple URLs are treated as interchangeable mirrors and tried in turn;
        a mirror that delivers audio resets the failover budget, so a healthy mirror keeps
        streaming while a set of unreachable mirrors raises the last error instead of spinning.

        :param url: One stream URL, or a list of mirror URLs to fail over between.
        :param streamdetails: StreamDetails to update with metadata.
        """
        urls = self._normalize_reconnecting_urls(url)
        if len(urls) == 1:
            async for chunk in self.get_icy_radio_stream(urls[0], streamdetails):
                yield chunk
            return

        url_index = 0
        failed_rotations = 0
        max_failed_rotations = len(urls) * 2
        last_err: MusicAssistantError | None = None
        while failed_rotations <= max_failed_rotations:
            current_url = urls[url_index % len(urls)]
            url_index += 1
            delivered_audio = False
            try:
                async for chunk in self.get_icy_radio_stream(current_url, streamdetails):
                    delivered_audio = True
                    failed_rotations = 0
                    # release the previous failure while healthy: it pins the full
                    # exception traceback (with frames) for the lifetime of the stream
                    last_err = None
                    yield chunk
                return
            except RADIO_MIRROR_FAILOVER_ERRORS as err:
                last_err = err
                if not delivered_audio:
                    failed_rotations += 1
                self.logger.warning(
                    "ICY radio mirror %s failed, trying next url (%d/%d): %s",
                    current_url,
                    failed_rotations,
                    max_failed_rotations,
                    err,
                )
        if last_err is not None:
            raise last_err

    async def get_reconnecting_radio_stream(self, url: str) -> AsyncGenerator[bytes]:
        """
        Yield continuous radio stream data, automatically reconnecting on disconnect.

        :param url: URL of the radio stream.
        """
        timeout = ClientTimeout(total=None, connect=30, sock_read=5 * 60)
        reconnect_count = 0
        max_reconnects = 1000  # Allow many reconnects for long-running radio

        while reconnect_count <= max_reconnects:
            try:
                async with self._connect_radio_stream(
                    url, allow_redirects=True, headers=HTTP_HEADERS, timeout=timeout
                ) as resp:
                    chunk_count = 0
                    async for chunk in resp.content.iter_any():
                        chunk_count += 1
                        yield chunk

                    # Connection closed normally - reconnect
                    self.logger.debug(
                        "Radio stream connection closed after %d chunks, reconnecting... "
                        "(reconnect #%d)",
                        chunk_count,
                        reconnect_count,
                    )
                    reconnect_count += 1
                    await asyncio.sleep(0.1)  # Brief delay before reconnect

            except asyncio.CancelledError:
                self.logger.debug("Radio stream cancelled for %s", url)
                raise
            except (
                aiohttp.ClientConnectionError,
                aiohttp.ClientPayloadError,
                aiohttp.ServerDisconnectedError,
            ) as err:
                # Transient network errors - retry
                self.logger.warning("Radio stream error (reconnect #%d): %s", reconnect_count, err)
                reconnect_count += 1
                if reconnect_count > max_reconnects:
                    raise RetriesExhausted(
                        f"Radio stream failed after {max_reconnects} reconnects: {err}"
                    ) from err
                await asyncio.sleep(0.5)
            except aiohttp.ClientResponseError as err:
                if err.status == 404:
                    raise MediaNotFoundError(f"Radio stream not found: {url}") from err
                if err.status == 403:
                    raise ProviderPermissionDenied(f"Radio stream access denied: {url}") from err
                # Other HTTP errors (5xx etc) - could be temporary
                raise ProviderUnavailableError(
                    f"Radio stream returned HTTP {err.status}: {err}"
                ) from err

        self.logger.warning("Radio stream reached max reconnects (%d) for %s", max_reconnects, url)

    async def get_hls_substream(self, url: str) -> PlaylistItem:
        """Select the (highest quality) HLS substream for given HLS playlist/URL."""
        mass = self.mass
        timeout = ClientTimeout(total=None, connect=30, sock_read=5 * 60)
        # fetch master playlist and select (best) child playlist
        # https://datatracker.ietf.org/doc/html/draft-pantos-http-live-streaming-19#section-10
        async with mass.http_session_no_ssl.get(
            encoded_request_url(url), allow_redirects=True, headers=HTTP_HEADERS, timeout=timeout
        ) as resp:
            resp.raise_for_status()
            raw_data = await resp.read()
            encoding = await detect_charset(raw_data, preferred=resp.charset)
            master_m3u_data = raw_data.decode(encoding, errors="replace")
        substreams = parse_m3u(master_m3u_data)
        # There is a chance that we did not get a master playlist with subplaylists
        # but just a single master/sub playlist with the actual audio stream(s)
        # so we need to detect if the playlist child's contain audio streams or
        # sub-playlists.
        if any(
            x
            for x in substreams
            if (x.length or x.path.endswith((".mp4", ".aac")))
            and not x.path.endswith((".m3u", ".m3u8"))
        ):
            return PlaylistItem(path=url, key=substreams[0].key)
        # sort substreams on best quality (highest bandwidth) when available
        if any(x for x in substreams if x.stream_info):
            substreams.sort(
                key=lambda x: int(
                    x.stream_info.get("BANDWIDTH", "0") if x.stream_info is not None else 0
                ),
                reverse=True,
            )
        substream = substreams[0]
        if not substream.path.startswith("http"):
            # path is relative, stitch it together
            base_path = url.rsplit("/", 1)[0]
            substream.path = base_path + "/" + substream.path
        return substream

    async def get_multi_file_stream(
        self,
        streamdetails: StreamDetails,
        seek_position: int = 0,
    ) -> AsyncGenerator[bytes]:
        """
        Return audio stream for a concatenation of multiple files.

        Arguments:
        seek_position: The position to seek to in seconds
        """
        if not isinstance(streamdetails.path, list):
            raise InvalidDataError("Multi-file streamdetails requires a list of MultiPartPath")
        parts, seek_position = get_parts_from_position(streamdetails.path, seek_position)
        files_list = [part.path for part in parts]

        # concat input files
        temp_file = f"/tmp/{shortuuid.random(20)}.txt"  # noqa: S108
        async with aiofiles.open(temp_file, "w") as f:
            await f.write(build_concat_filelist(files_list))

        try:
            async for chunk in get_ffmpeg_stream(
                audio_input=temp_file,
                input_format=streamdetails.audio_format,
                output_format=AudioFormat(
                    content_type=ContentType.NUT,
                    sample_rate=streamdetails.audio_format.sample_rate,
                    bit_depth=streamdetails.audio_format.bit_depth,
                    channels=streamdetails.audio_format.channels,
                ),
                extra_input_args=[
                    "-safe",
                    "0",
                    "-f",
                    "concat",
                    "-i",
                    temp_file,
                    "-ss",
                    str(seek_position),
                ],
            ):
                yield chunk
        finally:
            await remove_file(temp_file)

    def get_player_output_plan(
        self,
        player_id: str,
        input_format: AudioFormat,
        output_format: AudioFormat,
        *,
        shared_player_ids: Iterable[str] | None = None,
        handoff_format: AudioFormat | None = None,
        queue_id: str | None = None,
        session_id: str | None = None,
        queue_item_id: str | None = None,
    ) -> AudioOutputPlan:
        """
        Return executable filters and matching output details for a player.

        :param player_id: Destination player identifier.
        :param input_format: PCM format entering player-specific processing.
        :param output_format: Furthest downstream output format known to the server.
        :param shared_player_ids: Additional players receiving this identical output path.
            An empty iterable marks a path that can gain shared destinations later.
        :param handoff_format: Earlier provider handoff format when it differs.
        :param queue_id: Explicit queue identifier for the processing snapshot.
        :param session_id: Explicit queue session identifier for the processing snapshot.
        :param queue_item_id: Queue item for a single-item output path.
        """
        filter_params: list[str | ComplexFilter] = []
        player = self.mass.players.get_player(player_id)
        destination_player_id = (
            player.protocol_parent_id if player and player.protocol_parent_id else player_id
        )
        resolved_shared_player_ids = (
            resolve_output_player_ids(self.mass, shared_player_ids) - {destination_player_id}
            if shared_player_ids is not None
            else None
        )
        destination_player_ids = {destination_player_id, *(resolved_shared_player_ids or ())}
        if player:
            dsp_config_id = self._resolve_player_dsp_config_id(player)
            dsp = self._resolve_player_dsp_config(player)
            configured_dsp = self.mass.config.get_player_dsp_config(dsp_config_id)
            if configured_dsp.enabled and not dsp.enabled and is_grouping_preventing_dsp(player):
                dsp_state = DSPState.DISABLED_BY_UNSUPPORTED_GROUP
            else:
                dsp_state = DSPState.ENABLED if dsp.enabled else DSPState.DISABLED
        else:
            dsp_config_id = player_id
            dsp = self.mass.config.get_player_dsp_config(player_id)
            dsp_state = DSPState.ENABLED if dsp.enabled else DSPState.DISABLED

        enabled_filters = [dsp_filter for dsp_filter in dsp.filters if dsp_filter.enabled]
        # a neutral filter (0 dB gain, centered balance) emits no params; exclude
        # it so it is not reported as an active, non-bit-perfect stage
        effective_filters: list[DSPFilter] = []
        if dsp.enabled:
            if dsp.input_gain != 0:
                filter_params.append(f"volume={dsp.input_gain}dB")
            ir_dir = os.path.join(self.mass.storage_path, DSP_IRS_DIRNAME)
            known_ir_ids = {record["ir_id"] for record in self.mass.config.get_dsp_irs()}
            for dsp_filter in enabled_filters:
                if isinstance(dsp_filter, ConvolutionFilter) and dsp_filter.ir_id:
                    # ffmpeg fails to open the graph if the impulse response file is gone,
                    # which costs the player all audio, so drop the filter instead
                    if dsp_filter.ir_id not in known_ir_ids:
                        self.logger.warning(
                            "Skipping the convolution filter of player %s: "
                            "impulse response %s is not stored",
                            player_id,
                            dsp_filter.ir_id,
                        )
                        continue
                params = filter_to_ffmpeg_params(dsp_filter, input_format, ir_dir=ir_dir)
                if not params:
                    continue
                filter_params.extend(params)
                effective_filters.append(dsp_filter)
            if dsp.output_gain != 0:
                filter_params.append(f"volume={dsp.output_gain}dB")

        channel_value = self._get_output_channels(player, player_id)
        source_channel = None
        channel_mix = ""
        # a single channel source is already the downmix and holds no FL/FR to select
        # from, where a pan would silently resolve every gain to zero
        if input_format.channels > 1:
            if channel_value == "left":
                source_channel = AudioChannel.FL
                channel_mix = "FL"
            elif channel_value == "right":
                source_channel = AudioChannel.FR
                channel_mix = "FR"
            elif channel_value == "mono":
                # both source channels feed the downmix, so report ALL to keep the
                # output from ever being presented as bit perfect
                source_channel = AudioChannel.ALL
                channel_mix = "0.5*FL+0.5*FR"
        if channel_mix:
            # the pan runs in the command that emits the handoff format, and it feeds
            # every channel of it explicitly: leaving ffmpeg to upmix from a single
            # channel costs 3 dB through its rematrix
            if (handoff_format or output_format).channels == 1:
                filter_params.append(f"pan=mono|c0={channel_mix}")
            else:
                filter_params.append(f"pan=stereo|c0={channel_mix}|c1={channel_mix}")

        output_details = AudioOutputDetails(
            player_ids=sorted(destination_player_ids),
            dsp=AudioDSPDetails(
                state=dsp_state,
                input_gain=dsp.input_gain if dsp.enabled else 0.0,
                filters=effective_filters,
                output_gain=dsp.output_gain if dsp.enabled else 0.0,
                preset_id=dsp.preset_id,
            ),
            source_channel=source_channel,
            output_format=output_format,
        )
        output_plan = AudioOutputPlan(
            filter_params=filter_params,
            output_details=output_details,
            input_format=input_format,
            handoff_format=handoff_format,
            dsp_config_id=dsp_config_id,
        )
        if queue_id is not None and session_id is not None:
            self.mass.streams.audio_processing.update_output(
                destination_player_id,
                output_plan,
                shared_player_ids=resolved_shared_player_ids,
                queue_id=queue_id,
                session_id=session_id,
                queue_item_id=queue_item_id,
            )
        self.logger.log(
            VERBOSE_LOG_LEVEL,
            "Generated ffmpeg params for player %s: %s",
            player_id,
            filter_params,
        )
        return output_plan

    async def get_output_format(
        self,
        output_format_str: str,
        player: Player,
        content_sample_rate: int,
        content_bit_depth: int,
        media_type: MediaType = MediaType.UNKNOWN,
    ) -> AudioFormat:
        """Parse (player specific) output format details for given format string."""
        content_type: ContentType = ContentType.try_parse(output_format_str)
        player_supported_rates = player.get_supported_sample_rates()
        supported_sample_rates = [sr for sr, _ in player_supported_rates]
        if content_sample_rate in supported_sample_rates:
            output_sample_rate = content_sample_rate
        else:
            output_sample_rate = max(supported_sample_rates)
        # only consider bit depths that are actually paired with the chosen sample rate
        bit_depths_for_rate = [
            bd for (sr, bd) in player_supported_rates if sr == output_sample_rate
        ]
        output_bit_depth = min(content_bit_depth, max(bit_depths_for_rate, default=16))

        if not content_type.is_lossless():
            # no point in having a higher bit depth for lossy formats
            output_bit_depth = 16
            output_sample_rate = min(48000, output_sample_rate)
        if media_type not in (MediaType.TRACK, MediaType.AUDIO_SOURCE, MediaType.FLOW_STREAM):
            # no point in having a higher bit depth for non-track media types (e.g. TTS, radio)
            output_bit_depth = min(output_bit_depth, 16)
        if output_format_str == "pcm":
            content_type = ContentType.from_bit_depth(output_bit_depth)

        output_channels_str = self._get_output_channels(player, player.player_id)
        fmt = AudioFormat(
            content_type=content_type,
            sample_rate=output_sample_rate,
            bit_depth=output_bit_depth,
            channels=1 if output_channels_str != "stereo" else 2,
        )
        fmt.bit_rate = get_bit_rate(fmt)
        return fmt

    async def select_pcm_format(
        self,
        player: Player,
        streamdetails: StreamDetails,
        crossfade_enabled: bool,
        overlay_active: bool = False,
    ) -> AudioFormat:
        """
        Select the internal PCM format for streaming a single queue item.

        Used by the per-item (non-flow) stream path. The sample rate is the highest
        rate the player supports that is <= the source rate, so the source is never
        upsampled. The bit depth follows the source unless audio processing
        (crossfade, volume normalization, DSP) is active — those need F32 headroom
        to avoid clipping/precision loss. Surround sources are folded down to stereo.
        Realtime AudioSource items skip all processing and get a pure passthrough
        format (source rate/bit depth when the player supports them).

        :param player: The player requesting the stream.
        :param streamdetails: Stream details for the current item.
        :param crossfade_enabled: Whether crossfade is enabled for this stream.
        :param overlay_active: Whether an audio overlay will be mixed into this stream.
        """
        if streamdetails.media_type == MediaType.AUDIO_SOURCE:
            return self._select_audio_source_pcm_format(player, streamdetails)
        supported_sample_rates = [sr for sr, _ in player.get_supported_sample_rates()]
        # snap-down: pick the highest supported rate <= source. when the source rate
        # is below every supported rate (e.g. 22 kHz content on a 44.1k-only player),
        # fall back to the lowest supported rate instead of a hardcoded 48 kHz that
        # the player may not actually support.
        output_sample_rate = max(
            (r for r in supported_sample_rates if r <= streamdetails.audio_format.sample_rate),
            default=min(supported_sample_rates),
        )
        content_type, bit_depth = self._pick_pcm_bit_depth(
            (player,),
            streamdetails,
            crossfade_enabled,
            overlay_active,
        )
        pcm_format = AudioFormat(
            sample_rate=output_sample_rate,
            content_type=content_type,
            bit_depth=bit_depth,
            # fold surround sources down to stereo right at the decode step: no
            # output format carries more than two channels, so a wider PCM format
            # only makes every bytes-to-seconds sum on the stream come out short
            channels=min(streamdetails.audio_format.channels, 2),
        )
        if crossfade_enabled or overlay_active:
            pcm_format.channels = 2
        return pcm_format

    async def select_flow_pcm_format(
        self,
        player: Player,
        start_streamdetails: StreamDetails | None = None,
        crossfade_enabled: bool = False,
        overlay_active: bool = False,
        fallback_sample_rate: int | None = None,
        output_players: Iterable[Player] | None = None,
    ) -> AudioFormat:
        """
        Select the internal PCM format for a Queue Flow Mode stream.

        Used by the gapless flow path that stitches multiple queue items into one
        continuous PCM stream. The sample rate is driven by the player's
        ``CONF_FLOW_MODE_SAMPLE_RATE`` setting (smart/bit_perfect/48k/96k/highest)
        — for the anchored modes it follows the first track's rate, for the fixed
        modes it snaps to the configured rate. The bit depth follows the first
        track's source unless audio processing is active (then F32 for headroom),
        avoiding an unnecessary up-convert to 32-bit when none of the consumers
        will benefit from it. When the first item is a realtime AudioSource, the
        flow mode config is ignored and a pure passthrough format is used so the
        source audio is delivered with minimum overhead and latency.

        :param player: The player the flow stream is being prepared for.
        :param start_streamdetails: Stream details of the first track in the flow.
            Required for the anchored modes ('smart' / 'bit_perfect') and for the
            bit-depth optimization. May be omitted for the fixed-rate modes — when
            omitted the bit depth defaults to F32.
        :param crossfade_enabled: Whether the queue will use crossfade transitions.
        :param overlay_active: Whether an audio overlay will be mixed into the stream.
        :param fallback_sample_rate: Preferred rate when the first item format is unknown.
        :param output_players: All players consuming the shared PCM stream. Their common
            sample rates and processing requirements determine the session format.
        """
        players = tuple(output_players) if output_players is not None else (player,)
        if not players:
            raise AudioError("At least one output player is required")
        supported_sample_rates = sorted(
            set.intersection(
                *(
                    {sample_rate for sample_rate, _ in item.get_supported_sample_rates()}
                    for item in players
                )
            )
        )
        if not supported_sample_rates:
            raise AudioError("Output players do not share a supported sample rate")
        if start_streamdetails is not None and (
            start_streamdetails.media_type == MediaType.AUDIO_SOURCE
        ):
            return self._select_audio_source_pcm_format(
                player,
                start_streamdetails,
                supported_sample_rates=supported_sample_rates,
            )
        flow_mode_conf = cast(
            "str",
            player.config.get_value(CONF_FLOW_MODE_SAMPLE_RATE, FLOW_MODE_SAMPLE_RATE_SMART),
        )

        if flow_mode_conf == FLOW_MODE_SAMPLE_RATE_HIGHEST:
            output_sample_rate = max(supported_sample_rates)
        elif flow_mode_conf == FLOW_MODE_SAMPLE_RATE_48000:
            # for the fixed-rate modes, the user picked a specific bandwidth/quality
            # ceiling; prefer the highest supported rate <= target
            output_sample_rate = _snap_supported_rate_down(48000, supported_sample_rates)
        elif flow_mode_conf == FLOW_MODE_SAMPLE_RATE_96000:
            output_sample_rate = _snap_supported_rate_down(96000, supported_sample_rates)
        else:
            # smart or bit_perfect (default): anchor the flow at the starting track's
            # sample rate; if the player doesn't natively support it, upsample to the
            # closest higher supported rate
            target_rate = (
                start_streamdetails.audio_format.sample_rate
                if start_streamdetails
                else (
                    fallback_sample_rate
                    if fallback_sample_rate is not None
                    else max(supported_sample_rates)
                )
            )
            output_sample_rate = _snap_supported_rate_up(target_rate, supported_sample_rates)

        content_type, bit_depth = self._pick_pcm_bit_depth(
            players, start_streamdetails, crossfade_enabled, overlay_active
        )
        return AudioFormat(
            content_type=content_type,
            sample_rate=output_sample_rate,
            bit_depth=bit_depth,
            channels=2,
        )

    async def get_audio_source_stream(
        self,
        queue_item: QueueItem,
        pcm_format: AudioFormat,
        raise_on_error: bool = True,
    ) -> AsyncGenerator[bytes]:
        """
        Get the realtime PCM stream for an AudioSource queue item.

        AudioSources are live/realtime: bytes flow at the producer's pace, with
        no pre-buffering, no loudness hydration, no volume normalization, no
        crossfade/fade-in, no playback-speed shift, no next-track preload. The
        path stays as small as possible to keep end-to-end latency low.

        Fast path: when the source PCM format already matches the consumer's
        ``pcm_format``, the provider's bytes are paced in Python and forwarded
        directly — no ffmpeg in the data path.

        Slow path: when formats differ, ffmpeg resamples/recodes the stream
        (with ``-re`` for rate pacing) via ``get_media_stream``.

        :param queue_item: The AudioSource queue item to stream.
        :param pcm_format: Output PCM format the consumer wants.
        :param raise_on_error: Re-raise stream errors instead of swallowing them.
        """
        streamdetails = queue_item.streamdetails
        assert streamdetails
        logger = self.logger.getChild("audio_source_stream")
        bytes_received = 0
        try:
            async for chunk in self._iter_audio_source_pcm(streamdetails, pcm_format):
                bytes_received += len(chunk)
                yield chunk
        except AudioError as err:
            streamdetails.stream_error = True
            # revoke availability when the stream never produced any audio
            if bytes_received == 0:
                queue_item.available = False
            if raise_on_error:
                raise
            logger.error(
                "AudioError while streaming AudioSource %s (%s): %s",
                queue_item.name,
                streamdetails.uri,
                err,
            )
        except asyncio.CancelledError:
            raise
        except Exception as err:
            streamdetails.stream_error = True
            if raise_on_error:
                raise
            logger.exception(
                "Unexpected error while streaming AudioSource %s (%s): %s",
                queue_item.name,
                streamdetails.uri,
                err,
            )
        finally:
            streamdetails.seconds_streamed = bytes_received / pcm_format.pcm_sample_size

    async def get_queue_item_stream(
        self,
        queue_item: QueueItem,
        pcm_format: AudioFormat,
        seek_position: int = 0,
        playback_speed: float = 1.0,
        raise_on_error: bool = True,
        normalization_override: VolumeNormalizationMode | None = None,
        session_id: str | None = None,
    ) -> AsyncGenerator[bytes]:
        """
        Get the (PCM) audio stream for a single queue item.

        Audio is always served from the AudioBuffer which stores raw decoded PCM.
        Volume normalization and other filters are applied on-the-fly when reading
        from the buffer.

        AudioSource items dispatch to ``get_audio_source_stream`` instead: they
        are realtime and bypass the buffering/normalization/filter machinery.

        :param normalization_override: Force this volume normalization mode instead of
            re-evaluating it from the (possibly just-updated) loudness measurement. Used by
            the crossfade path to keep a track's replayed intro and its body on the same mode.
        :param session_id: Queue session that owns processing-detail updates.
        """
        if queue_item.media_type == MediaType.AUDIO_SOURCE:
            async for chunk in self.get_audio_source_stream(
                queue_item=queue_item,
                pcm_format=pcm_format,
                raise_on_error=raise_on_error,
            ):
                yield chunk
            return

        streamdetails = queue_item.streamdetails
        assert streamdetails
        filter_params: list[str] = []

        logger = self.logger.getChild("queue_item_stream")

        if normalization_override is not None:
            # crossfade path pins the body to the intro's mode; skip hydration/re-eval that could flip it
            streamdetails.volume_normalization_mode = normalization_override
        else:
            # hydrate loudness from audio analysis (just-in-time, so that a measurement
            # completed during a previous play is picked up here). A live analyzer run
            # may have already populated streamdetails.loudness in memory — don't clobber
            # that, and don't clobber a value set upstream by the music provider.
            if streamdetails.loudness is None:
                if analysis := await self.mass.streams.audio_analysis.get_audio_analysis(
                    streamdetails.item_id,
                    streamdetails.provider,
                    media_type=streamdetails.media_type,
                    # use the authoritative EBU R128 value, not another provider's loudness proxy
                    priority=(LOUDNESS_ANALYSIS_DOMAIN,),
                ):
                    if analysis.loudness_integrated is not None:
                        streamdetails.loudness = round(analysis.loudness_integrated, 2)
                    if analysis.loudness_album is not None and streamdetails.loudness_album is None:
                        streamdetails.loudness_album = round(analysis.loudness_album, 2)

            # re-evaluate normalization mode: the background loudness analyzer may have
            # updated streamdetails.loudness since get_stream_details was called
            if streamdetails.queue_id:
                volume_normalization_enabled = (
                    self.mass.config.get_effective_player_queue_config_value(
                        streamdetails.queue_id, CONF_VOLUME_NORMALIZATION, CONF_VALUE_ENABLED
                    )
                    != CONF_VALUE_DISABLED
                )
                streamdetails.volume_normalization_mode = get_normalization_mode(
                    self._get_volume_normalization_preference(streamdetails),
                    volume_normalization_enabled,
                    streamdetails,
                )

        # handle volume normalization
        gain_correct: float | None = None
        if streamdetails.volume_normalization_mode == VolumeNormalizationMode.DYNAMIC:
            filter_rule = (
                f"loudnorm=I={streamdetails.target_loudness}"
                ":TP=-2.0:LRA=10.0:offset=0.0:print_format=json"
            )
            filter_params.append(filter_rule)
        elif streamdetails.volume_normalization_mode == VolumeNormalizationMode.FIXED_GAIN:
            config_key = (
                CONF_VOLUME_NORMALIZATION_FIXED_GAIN_TRACKS
                if streamdetails.media_type == MediaType.TRACK
                else CONF_VOLUME_NORMALIZATION_FIXED_GAIN_RADIO
            )
            gain_value = self.mass.streams.get_config_value(config_key, return_type=float)
            gain_correct = round(gain_value, 2)
            filter_params.append(f"volume={gain_correct}dB")
        elif streamdetails.volume_normalization_mode == VolumeNormalizationMode.MEASUREMENT_ONLY:
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

        # handle playback speed
        if playback_speed != 1.0:
            filter_params.append(f"atempo={playback_speed}")

        # handle optional fade-in
        if streamdetails.fade_in:
            filter_params.insert(0, "afade=type=in:start_time=0:duration=3")

        logger.log(
            VERBOSE_LOG_LEVEL,
            "Starting queue item stream for %s (%s)"
            " - using fade-in: %s"
            " - using volume normalization: %s"
            " - using playback speed: %s",
            queue_item.name,
            streamdetails.uri,
            streamdetails.fade_in,
            streamdetails.volume_normalization_mode,
            playback_speed,
        )

        # get or create the AudioBuffer (stores raw decoded PCM)
        seek_position_ms = int(seek_position * 1000)
        audio_buffer = await AudioBuffer.get_buffer(
            mass=self.mass,
            streamdetails=streamdetails,
            seek_position_ms=seek_position_ms,
            reason="streaming",
        )
        if (
            streamdetails.queue_id
            and (queue_data := self.mass.player_queues.queue_data_or_none(streamdetails.queue_id))
            and (processing_session_id := session_id or queue_data.session_id)
        ):
            self.mass.streams.audio_processing.update_item_runtime(
                queue_id=streamdetails.queue_id,
                session_id=processing_session_id,
                queue_item_id=queue_item.queue_item_id,
                input_format=audio_buffer.pcm_format,
                pcm_format=pcm_format,
                normalization=get_normalization_details(streamdetails, gain_correct),
                playback_speed=playback_speed,
                alters_audio=streamdetails.fade_in,
            )
        # read from buffer with filters applied (volume normalization, speed, fade-in, etc.)
        # if no processing needed, this yields directly from the buffer
        media_stream_gen = audio_buffer.get_stream(
            output_format=pcm_format,
            seek_position_ms=seek_position_ms,
            filter_params=filter_params or None,
        )

        first_chunk_received = False
        bytes_received = 0
        finished = False
        next_buffer_triggered = False
        stream_started_at = asyncio.get_event_loop().time()
        try:
            async for chunk in media_stream_gen:
                bytes_received += len(chunk)
                if not first_chunk_received:
                    first_chunk_received = True
                    logger.log(
                        VERBOSE_LOG_LEVEL,
                        "First audio chunk received for %s (%s) after %.2f seconds",
                        queue_item.name,
                        streamdetails.uri,
                        asyncio.get_event_loop().time() - stream_started_at,
                    )
                # trigger pre-buffering of the next item well before end
                # to ensure the raw PCM is ready when the next item needs to be streamed.
                # tracks and sound effects are finite files that fill and close immediately;
                # live sources (radio, audio_source) open an upstream connection that would
                # sit idle and likely time out before the player actually consumes it.
                if (
                    not next_buffer_triggered
                    and streamdetails.duration
                    and (queue := self.mass.player_queues.get_active_queue(queue_item.queue_id))
                    and queue.next_item
                    and queue.next_item.queue_item_id != queue_item.queue_item_id
                    and queue.next_item.media_type in (MediaType.TRACK, MediaType.SOUND_EFFECT)
                    and (bytes_received / pcm_format.pcm_sample_size + seek_position)
                    >= streamdetails.duration - 60
                ):
                    next_buffer_triggered = True
                    self.mass.player_queues.prepare_next_audio_buffer(queue_item.queue_id)
                yield chunk
                del chunk
            # if we received no audio and the buffer has a producer error,
            # the error was swallowed by the FFmpeg stdin feeder - re-raise it
            if bytes_received == 0 and audio_buffer.has_error:
                raise AudioError("Failed to stream audio") from audio_buffer._producer_error
            finished = True
        except AudioError as err:
            streamdetails.stream_error = True
            # revoke availability when the stream never produced any audio
            if bytes_received == 0:
                queue_item.available = False
            if raise_on_error:
                raise
            logger.error(
                "AudioError while streaming queue item %s (%s): %s",
                queue_item.name,
                streamdetails.uri,
                err,
            )
        except asyncio.CancelledError:
            raise
        except Exception as err:
            streamdetails.stream_error = True
            if raise_on_error:
                raise
            logger.exception(
                "Unexpected error while streaming queue item %s (%s): %s",
                queue_item.name,
                streamdetails.uri,
                err,
            )
        finally:
            seconds_streamed = bytes_received / pcm_format.pcm_sample_size
            streamdetails.seconds_streamed = seconds_streamed
            logger.log(
                VERBOSE_LOG_LEVEL,
                "stream %s for %s in %.2f seconds - seconds streamed/buffered: %.2f",
                "aborted" if not finished else "finished",
                streamdetails.uri,
                asyncio.get_event_loop().time() - stream_started_at,
                seconds_streamed,
            )
            self._notify_provider_streamed(streamdetails, finished, seconds_streamed)

    async def get_queue_item_stream_with_smartfade(
        self,
        player: Player,
        queue_item: QueueItem,
        pcm_format: AudioFormat,
        crossfade_mode: CrossfadeMode = CrossfadeMode.SMART_CROSSFADE,
        standard_crossfade_duration: int = 10,
        session_id: str | None = None,
    ) -> AsyncGenerator[bytes]:
        """
        Return one queue item with a crossfade into the next item.

        :param player: Player consuming the stream.
        :param queue_item: Queue item to stream.
        :param pcm_format: Shared PCM format.
        :param crossfade_mode: Effective crossfade mode.
        :param standard_crossfade_duration: Configured standard crossfade duration.
        :param session_id: Queue session that owns processing-detail updates.
        """
        queue = self.mass.player_queues.get(queue_item.queue_id)
        if not queue:
            raise RuntimeError(f"Queue {queue_item.queue_id} not found")

        streamdetails = queue_item.streamdetails
        assert streamdetails
        crossfade_data = self._crossfade_data.get(queue.queue_id)

        if crossfade_data and streamdetails.seek_position > 0:
            # don't do crossfade when seeking into track
            self.logger.debug(
                "Discarding crossfade data for queue %s - seeking into track (pos=%s)",
                queue.display_name,
                streamdetails.seek_position,
            )
            crossfade_data = None
        if crossfade_data and (crossfade_data.queue_item_id != queue_item.queue_item_id):
            # edge case alert: the next item changed just while we were preloading/crossfading
            self.logger.warning(
                "Skipping crossfade data for queue %s - next item changed!"
                " (expected queue_item_id=%s, got=%s)",
                queue.display_name,
                crossfade_data.queue_item_id,
                queue_item.queue_item_id,
            )
            crossfade_data = None
            self._crossfade_data.pop(queue.queue_id, None)
        elif not crossfade_data:
            self.logger.debug(
                "No crossfade data available for queue %s (queue_item_id=%s)",
                queue.display_name,
                queue_item.queue_item_id,
            )

        self.logger.debug(
            "Start Streaming queue track: %s (%s) for queue %s on player %s"
            "- crossfade mode: %s "
            "- crossfading from previous track: %s ",
            queue_item.streamdetails.uri if queue_item.streamdetails else "Unknown URI",
            queue_item.name,
            queue.display_name,
            player.name,
            crossfade_mode,
            "true" if crossfade_data else "false",
        )

        buffer = bytearray()
        bytes_written = 0
        # calculate crossfade buffer size
        crossfade_buffer_duration = (
            SMART_CROSSFADE_DURATION
            if crossfade_mode == CrossfadeMode.SMART_CROSSFADE
            else standard_crossfade_duration
        )
        crossfade_buffer_duration = min(
            crossfade_buffer_duration,
            int(streamdetails.duration / 2)
            if streamdetails.duration
            else crossfade_buffer_duration,
        )
        # skip crossfade if buffer would be too small to be meaningful
        if crossfade_buffer_duration < 5:
            crossfade_buffer_duration = 0
        # Ensure crossfade buffer size is aligned to frame boundaries
        # Frame size = bytes_per_sample * channels
        bytes_per_sample = pcm_format.bit_depth // 8
        frame_size = bytes_per_sample * pcm_format.channels
        crossfade_buffer_size = int(pcm_format.pcm_sample_size * crossfade_buffer_duration)
        # Round down to nearest frame boundary
        crossfade_buffer_size = (crossfade_buffer_size // frame_size) * frame_size
        fade_out_data: bytes | None = None
        uncredited_tail_bytes = 0

        # pin the body to DYNAMIC when the intro was baked DYNAMIC,
        # else a late measurement flips it and causes a volume jump
        norm_override: VolumeNormalizationMode | None = None
        if crossfade_data and crossfade_data.normalization_mode == VolumeNormalizationMode.DYNAMIC:
            norm_override = VolumeNormalizationMode.DYNAMIC

        if crossfade_data:
            # reported media-time (TRIM + CF) is decoupled from the raw buffer seek below (X)
            streamdetails.seek_position = crossfade_data.elapsed_time_offset
            # yield the POST portion (resample if previous track's format differs)
            if crossfade_data.pcm_format != pcm_format:
                async for _chunk in resample_pcm_audio(
                    crossfade_data.data, crossfade_data.pcm_format, pcm_format
                ):
                    yield _chunk
                    bytes_written += len(_chunk)
            else:
                for pcm_slice in iter_pcm_slices(crossfade_data.data, pcm_format, 1000):
                    yield pcm_slice
                    await asyncio.sleep(0)
                bytes_written += len(crossfade_data.data)
            # skip past the audio already consumed by the crossfade
            fade_in_duration_seconds = (
                crossfade_data.fade_in_size / crossfade_data.fade_in_pcm_format.pcm_sample_size
            )
            discard_seconds = int(fade_in_duration_seconds)
            fractional_seconds = fade_in_duration_seconds - discard_seconds
            discard_leftover = int(fractional_seconds * pcm_format.pcm_sample_size)
            discard_leftover = (discard_leftover // frame_size) * frame_size
            crossfade_data = None
            self._crossfade_data.pop(queue.queue_id, None)
        else:
            discard_seconds = int(streamdetails.seek_position)
            discard_leftover = 0

        # Yield the first WARMUP_DURATION worth of audio immediately so playback starts
        # right away. After that, start accumulating the crossfade holdback buffer.
        warmup_size = int(pcm_format.pcm_sample_size * WARMUP_DURATION)
        warmup_bytes = 0
        total_chunks_received = 0
        playback_speed = cast("float", queue_item.extra_attributes.get("playback_speed", 1.0))
        async for chunk in self.get_queue_item_stream(
            queue_item,
            pcm_format,
            seek_position=discard_seconds,
            playback_speed=playback_speed,
            normalization_override=norm_override,
            session_id=session_id,
        ):
            total_chunks_received += 1
            if discard_leftover:
                chunk = chunk[discard_leftover:]  # noqa: PLW2901
                discard_leftover = 0

            if warmup_bytes < warmup_size:
                # warmup: yield directly, don't buffer
                yield chunk
                warmup_bytes += len(chunk)
                bytes_written += len(chunk)
                del chunk
                continue

            buffer.extend(chunk)
            del chunk
            if len(buffer) < crossfade_buffer_size:
                await asyncio.sleep(0)
                continue
            # yield everything above the crossfade buffer
            while len(buffer) > crossfade_buffer_size:
                yield bytes(buffer[: pcm_format.pcm_sample_size])
                bytes_written += pcm_format.pcm_sample_size
                del buffer[: pcm_format.pcm_sample_size]
                await asyncio.sleep(0)

        #### HANDLE END OF TRACK

        # get next track for crossfade
        crossfade_start_time = asyncio.get_event_loop().time()
        next_queue_item: QueueItem | None
        try:
            self.logger.debug(
                "Preloading NEXT track for crossfade for queue %s", queue.display_name
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
        except QueueEmpty:
            # end of queue reached, no next item
            next_queue_item = None

        crossfade_allowed = False
        if next_queue_item and next_queue_item.streamdetails:
            next_pcm = await self.select_pcm_format(
                player=player,
                streamdetails=next_queue_item.streamdetails,
                crossfade_enabled=True,
            )
            crossfade_allowed = self.crossfade_allowed(
                queue_item,
                crossfade_mode=crossfade_mode,
                player_id=player.player_id,
                flow_mode=False,
                next_queue_item=next_queue_item,
                sample_rate=pcm_format.sample_rate,
                next_sample_rate=next_pcm.sample_rate,
            )
        if not crossfade_allowed:
            # no crossfade enabled/allowed, just yield the buffer last part
            bytes_written += len(buffer)
            for pcm_slice in iter_pcm_slices(bytes(buffer), pcm_format, 1000):
                yield pcm_slice
                await asyncio.sleep(0)
        else:
            assert next_queue_item is not None
            # the remaining buffer is the fade-out tail of the current track
            fade_out_data = bytes(buffer)
            buffer = bytearray()
            # initialized before the try block — the except handler reads these
            first_part_written = 0
            second_part_buf = bytearray()
            try:
                # wrap the next track's stream in a counting generator that caps
                # at crossfade_buffer_size and tracks how many bytes were consumed
                fade_in_bytes_consumed = 0

                _next_item = next_queue_item

                async def _limited_fade_in() -> AsyncGenerator[bytes]:
                    nonlocal fade_in_bytes_consumed
                    async for chunk in self.get_queue_item_stream(
                        _next_item,
                        pcm_format,
                        playback_speed=cast(
                            "float", _next_item.extra_attributes.get("playback_speed", 1.0)
                        ),
                        session_id=session_id,
                    ):
                        remaining = crossfade_buffer_size - fade_in_bytes_consumed
                        if remaining <= 0:
                            break
                        if len(chunk) > remaining:
                            fade_in_bytes_consumed += remaining
                            yield chunk[:remaining]
                            break
                        fade_in_bytes_consumed += len(chunk)
                        yield chunk

                smart_fade = await self.smart_fades_mixer.build(
                    fade_in_streamdetails=cast("StreamDetails", next_queue_item.streamdetails),
                    fade_out_streamdetails=streamdetails,
                    pcm_format=pcm_format,
                    standard_crossfade_duration=standard_crossfade_duration,
                    mode=crossfade_mode,
                    fade_out_data=fade_out_data,
                    fade_in_bytes_len=crossfade_buffer_size,
                )
                crossfade_timing = smart_fade.timing_info
                # Split mix output at end-of-overlap: PRE+CF to A, POST to B's intro.
                fadeout_share_bytes = int(
                    (crossfade_timing.pre_crossfade_duration + crossfade_timing.crossfade_duration)
                    * pcm_format.pcm_sample_size
                )
                fadeout_share_bytes = (fadeout_share_bytes // frame_size) * frame_size
                async for mix_chunk in self.smart_fades_mixer.mix(
                    smart_fade,
                    fade_in_part=_limited_fade_in(),
                    fade_out_part=fade_out_data,
                    pcm_format=pcm_format,
                ):
                    if first_part_written < fadeout_share_bytes:
                        # split this chunk so A gets exactly fadeout_share_bytes
                        remaining = fadeout_share_bytes - first_part_written
                        if len(mix_chunk) > remaining:
                            yield mix_chunk[:remaining]
                            first_part_written += remaining
                            bytes_written += remaining
                            second_part_buf.extend(mix_chunk[remaining:])
                        else:
                            yield mix_chunk
                            first_part_written += len(mix_chunk)
                            bytes_written += len(mix_chunk)
                    else:
                        second_part_buf.extend(mix_chunk)
                # tail consumed by the mix but not credited to bytes_written
                uncredited_tail_bytes = len(fade_out_data) - first_part_written
                self._crossfade_data[queue_item.queue_id] = CrossfadeData(
                    data=bytes(second_part_buf),
                    fade_in_size=fade_in_bytes_consumed,
                    pcm_format=pcm_format,
                    fade_in_pcm_format=pcm_format,
                    queue_item_id=next_queue_item.queue_item_id,
                    elapsed_time_offset=(
                        crossfade_timing.fadein_trimmed_duration
                        + crossfade_timing.crossfade_duration
                    ),
                    normalization_mode=cast(
                        "StreamDetails", next_queue_item.streamdetails
                    ).volume_normalization_mode,
                )
                crossfade_elapsed = asyncio.get_event_loop().time() - crossfade_start_time
                self.logger.debug(
                    "Stored crossfade data for queue %s"
                    " - next queue_item_id: %s (preparation took %.1fs)",
                    queue.display_name,
                    next_queue_item.queue_item_id,
                    crossfade_elapsed,
                )
            except Exception as err:
                if first_part_written or second_part_buf:
                    # partial mix already played — concat'd fade_out_data would duplicate audio
                    raise
                # crossfade failed, fall back to just yielding the fade_out_data
                self.logger.warning(
                    "Crossfade failed for queue %s: %s",
                    queue.display_name,
                    err,
                )
                next_queue_item = None
                for pcm_slice in iter_pcm_slices(fade_out_data, pcm_format, 1000):
                    yield pcm_slice
                    await asyncio.sleep(0)
                bytes_written += len(fade_out_data)
                del fade_out_data
        # make sure the buffer gets cleaned up
        del buffer
        # update duration details based on the actual pcm data we sent
        # this also accounts for crossfade and silence stripping
        seconds_streamed = bytes_written / pcm_format.pcm_sample_size
        streamdetails.seconds_streamed = seconds_streamed
        uncredited_tail_seconds = uncredited_tail_bytes / pcm_format.pcm_sample_size
        # streamdetails.duration is in media-time; seconds_streamed is stream-time
        # (post-atempo), so we scale by playback_speed to recover media-time.
        streamdetails.duration = int(
            streamdetails.seek_position
            + (seconds_streamed + uncredited_tail_seconds) * playback_speed
        )
        # propagate accurate duration to queue_item so UI displays it
        queue_item.duration = streamdetails.duration
        self.logger.debug(
            "Finished Streaming queue track: %s (%s) on queue %s "
            "- crossfade data prepared for next track: %s",
            streamdetails.uri,
            queue_item.name,
            queue.display_name,
            next_queue_item.name if next_queue_item else "N/A",
        )

    async def get_queue_flow_stream(
        self,
        queue: PlayerQueue,
        start_queue_item: QueueItem,
        pcm_format: AudioFormat,
        session_id: str | None = None,
        protocol_player: Player | None = None,
    ) -> AsyncGenerator[bytes]:
        """
        Get a flow stream of all tracks in the queue as raw PCM audio.

        yields chunks of exactly 1 second of audio in the given pcm_format.

        :param queue: Queue being streamed.
        :param start_queue_item: First queue item in the flow stream.
        :param pcm_format: Shared PCM format for the complete flow stream.
        :param session_id: Queue session that owns processing-detail updates.
        :param protocol_player: The protocol player actually consuming the flow stream.
            Must be the same player that was used to select ``pcm_format`` so
            restart decisions are made against the correct supported sample rates
            and flow mode configuration. Falls back to the queue's player when omitted.
        """
        # ruff: noqa: PLR0915
        assert pcm_format.content_type.is_pcm()
        queue_track = None
        last_fadeout_part: bytes = b""
        last_streamdetails: StreamDetails | None = None
        last_play_log_entry: PlayLogEntry | None = None
        # Snapshot the queue's current session_id. PlayerQueues rotates this on
        # every new stream session, so if a newer producer takes over the queue
        # (rapid track switch, sync-group reform, dynamic leader handoff) the
        # snapshot will no longer match and we exit cleanly on the next yield or
        # playlog append — preventing two producers from writing to the same
        # pq_data.flow_mode_stream_log.
        pq_data = self.mass.player_queues.queue_data(queue.queue_id)
        flow_session_id = session_id or pq_data.session_id
        if flow_session_id is None or pq_data.session_id != flow_session_id:
            self.logger.debug(
                "Ignoring stale flow stream for queue %s (session %s, active %s)",
                queue.display_name,
                flow_session_id,
                pq_data.session_id,
            )
            return
        queue.flow_mode = True
        pq_data.flow_mode_stream_log = []
        if not start_queue_item:
            # this can happen in some (edge case) race conditions
            return
        pcm_sample_size = pcm_format.pcm_sample_size
        if start_queue_item.media_type != MediaType.TRACK:
            # no crossfade on non-tracks
            crossfade_mode = CrossfadeMode.DISABLED
            standard_crossfade_duration = 0
        else:
            crossfade_mode = self.mass.streams.get_crossfade_mode(queue)
            # crossfade duration is a global (queue controller) setting; fallback matches
            # CONF_ENTRY_CROSSFADE_DURATION's default
            standard_crossfade_duration = self.mass.config.get_raw_core_config_value(
                CONF_PLAYER_QUEUES, CONF_CROSSFADE_DURATION, 8
            )
        flow_mode_sample_rate_conf, flow_supported_sample_rates = self._flow_restart_context(
            queue.queue_id, protocol_player
        )
        # note: get_crossfade_mode() already falls back to standard when smart fades aren't
        # available (no analysis provider / minimal buffer), so crossfade_mode is safe to use.
        self.logger.info(
            "Start Queue Flow stream for Queue %s - crossfade: %s %s",
            queue.display_name,
            crossfade_mode,
            f"({standard_crossfade_duration}s)"
            if crossfade_mode == CrossfadeMode.STANDARD_CROSSFADE
            else "",
        )
        total_chunks_received = 0

        def _superseded() -> bool:
            """Return True if a newer stream session has taken over this queue."""
            return pq_data.session_id != flow_session_id

        queue_exhausted = False
        while True:
            # bail out early if a newer producer has taken over this queue,
            # so we don't append another entry to a stream log we no longer own
            if _superseded():
                self.logger.debug(
                    "Flow stream for queue %s superseded (session %s -> %s) "
                    "- exiting before next track",
                    queue.display_name,
                    flow_session_id,
                    pq_data.session_id,
                )
                return
            # get (next) queue item to stream
            if queue_track is None:
                queue_track = start_queue_item
            else:
                try:
                    queue_track = await self.mass.player_queues.load_next_queue_item(
                        queue.queue_id, queue_track.queue_item_id
                    )
                except QueueEmpty:
                    queue_exhausted = True
                    break

            if self._flow_stream_needs_restart(
                queue_track,
                pcm_format,
                flow_supported_sample_rates,
                flow_mode_sample_rate_conf,
                is_first_track=queue_track is start_queue_item,
            ):
                break

            if queue_track.streamdetails is None:
                self.logger.error(
                    "No StreamDetails for queue item %s (%s) on queue %s - skipping track",
                    queue_track.queue_item_id,
                    queue_track.name,
                    queue.display_name,
                )
                continue
            if flow_session_id is not None:
                self.mass.streams.audio_processing.update_item_context(
                    queue_id=queue.queue_id,
                    session_id=flow_session_id,
                    queue_item_id=queue_track.queue_item_id,
                    queue_processing=AudioQueueProcessing(
                        pcm_format=pcm_format,
                        playback_speed=cast(
                            "float",
                            queue_track.extra_attributes.get("playback_speed", 1.0),
                        ),
                        crossfade_mode=crossfade_mode,
                        overlay_active=overlay_active(queue),
                    ),
                    alters_audio=queue_track.streamdetails.fade_in,
                )

            self.logger.debug(
                "Start Streaming queue track: %s (%s) for queue %s",
                queue_track.streamdetails.uri,
                queue_track.name,
                queue.display_name,
            )
            # last chance to bail before mutating the stream log: a newer producer
            # may have taken over while we were awaiting load_next_queue_item
            if _superseded():
                self.logger.debug(
                    "Flow stream for queue %s superseded - exiting before playlog append",
                    queue.display_name,
                )
                return
            track_playback_speed = cast(
                "float", queue_track.extra_attributes.get("playback_speed", 1.0)
            )
            # calculate crossfade buffer size
            crossfade_buffer_duration = (
                SMART_CROSSFADE_DURATION
                if crossfade_mode == CrossfadeMode.SMART_CROSSFADE
                else standard_crossfade_duration
            )
            crossfade_buffer_duration = min(
                crossfade_buffer_duration,
                int(queue_track.streamdetails.duration / 2)
                if queue_track.streamdetails.duration
                else crossfade_buffer_duration,
            )
            # skip crossfade if buffer would be too small to be meaningful
            if crossfade_buffer_duration < 5:
                crossfade_buffer_duration = 0
            # Ensure crossfade buffer size is aligned to frame boundaries
            # Frame size = bytes_per_sample * channels
            bytes_per_sample = pcm_format.bit_depth // 8
            frame_size = bytes_per_sample * pcm_format.channels
            crossfade_buffer_size = int(pcm_format.pcm_sample_size * crossfade_buffer_duration)
            # Round down to nearest frame boundary
            crossfade_buffer_size = (crossfade_buffer_size // frame_size) * frame_size
            warmup_size = int(pcm_format.pcm_sample_size * WARMUP_DURATION)

            # raw_seek_position feeds the PCM buffer; streamdetails.seek_position
            # (overwritten below) only drives reported elapsed time.
            raw_seek_position = queue_track.streamdetails.seek_position
            # Build eagerly so seek_position is set before PlayLogEntry is appended —
            # consumer-paced mix() would otherwise let the queue briefly report 0.
            crossfade_smart_fade: SmartFade | None = None
            if (
                last_fadeout_part
                and last_streamdetails
                and crossfade_buffer_size > 0
                and crossfade_mode != CrossfadeMode.DISABLED
            ):
                crossfade_smart_fade = await self.smart_fades_mixer.build(
                    fade_in_streamdetails=queue_track.streamdetails,
                    fade_out_streamdetails=last_streamdetails,
                    pcm_format=pcm_format,
                    standard_crossfade_duration=standard_crossfade_duration,
                    mode=crossfade_mode,
                    fade_out_data=last_fadeout_part,
                    fade_in_bytes_len=crossfade_buffer_size,
                )
                timing_info = crossfade_smart_fade.timing_info
                queue_track.streamdetails.seek_position = (
                    raw_seek_position
                    + timing_info.fadein_trimmed_duration
                    + timing_info.crossfade_duration
                )
            # append to play log so the queue controller can work out which track is playing
            play_log_entry = PlayLogEntry(queue_track.queue_item_id)
            pq_data.flow_mode_stream_log.append(play_log_entry)

            bytes_written = 0
            crossfade_buffer = bytearray()
            warmup_bytes = 0
            first_chunk_received = False

            async for chunk in self.get_queue_item_stream(
                queue_track,
                pcm_format=pcm_format,
                seek_position=int(raw_seek_position),
                playback_speed=cast(
                    "float", queue_track.extra_attributes.get("playback_speed", 1.0)
                ),
                raise_on_error=False,
                session_id=flow_session_id,
            ):
                # if a newer producer has taken over this queue, stop sending
                # audio and exit cleanly before the outer-loop end-of-track
                # bookkeeping mutates seconds_streamed / duration on the log
                if _superseded():
                    self.logger.debug(
                        "Flow stream for queue %s superseded - stopping chunk yield",
                        queue.display_name,
                    )
                    return
                total_chunks_received += 1
                if not first_chunk_received:
                    first_chunk_received = True
                    # inform the queue that the track is now loaded in the buffer
                    # so the next track can be preloaded
                    self.mass.player_queues.track_loaded_in_buffer(
                        queue.queue_id, queue_track.queue_item_id
                    )

                if crossfade_mode == CrossfadeMode.DISABLED:
                    # no cross/smart fade: yield chunks directly without intermediate buffer
                    yield chunk
                    bytes_written += len(chunk)
                    del chunk
                    continue

                # Warmup: yield chunks directly until we have streamed WARMUP_DURATION
                # worth of audio, so playback starts immediately. Skip warmup when
                # crossfade data from the previous track is pending — we need a full
                # buffer for the mix.
                if warmup_bytes < warmup_size and not last_fadeout_part:
                    yield chunk
                    warmup_bytes += len(chunk)
                    bytes_written += len(chunk)
                    del chunk
                    continue

                # smart fades enabled: accumulate chunks in crossfade buffer
                crossfade_buffer.extend(chunk)
                del chunk
                if len(crossfade_buffer) < crossfade_buffer_size:
                    await asyncio.sleep(0)
                    continue

                # handle crossfade of previous track and new track
                if (
                    last_fadeout_part
                    and last_streamdetails
                    and crossfade_smart_fade is not None
                    and last_play_log_entry is not None
                ):
                    fadein_part = bytes(crossfade_buffer[:crossfade_buffer_size])
                    remaining_bytes = bytes(crossfade_buffer[crossfade_buffer_size:])
                    try:
                        crossfade_bytes_written = 0
                        async for mix_chunk in self.smart_fades_mixer.mix(
                            crossfade_smart_fade,
                            fade_in_part=fadein_part,
                            fade_out_part=last_fadeout_part,
                            pcm_format=pcm_format,
                        ):
                            yield mix_chunk
                            crossfade_bytes_written += len(mix_chunk)
                    except Exception as mix_err:
                        if crossfade_bytes_written:
                            # partial mix already played — concat'd tail would duplicate audio
                            raise
                        self.logger.warning(
                            "Crossfade mixer failed for %s, falling back to simple concat: %s",
                            queue_track.name,
                            mix_err,
                        )
                        for pcm_slice in iter_pcm_slices(last_fadeout_part, pcm_format, 1000):
                            yield pcm_slice
                            await asyncio.sleep(0)
                        # full tail was pre-counted and is now yielded as-is
                        crossfade_bytes_written = 0
                        remaining_bytes = bytes(crossfade_buffer)
                        # mix failed — undo the eager seek_position
                        queue_track.streamdetails.seek_position = raw_seek_position
                    if crossfade_bytes_written:
                        # Split mix output at end-of-overlap: PRE+CF to A, POST to B.
                        fadeout_share_seconds = (
                            timing_info.pre_crossfade_duration + timing_info.crossfade_duration
                        )
                        fadeout_share = int(fadeout_share_seconds * pcm_sample_size)
                        fadeout_share = (fadeout_share // frame_size) * frame_size
                        fadeout_share = min(fadeout_share, crossfade_bytes_written)
                        fadein_share = crossfade_bytes_written - fadeout_share
                        bytes_written += fadein_share
                        if last_play_log_entry:
                            assert last_play_log_entry.seconds_streamed is not None
                            # correct pre-counted full tail to the timing-based share
                            last_play_log_entry.seconds_streamed += (
                                fadeout_share - len(last_fadeout_part)
                            ) / pcm_sample_size
                    if remaining_bytes:
                        for pcm_slice in iter_pcm_slices(remaining_bytes, pcm_format, 1000):
                            yield pcm_slice
                            await asyncio.sleep(0)
                        bytes_written += len(remaining_bytes)
                        del remaining_bytes
                    last_fadeout_part = b""
                    last_streamdetails = None
                    crossfade_buffer = bytearray()
                    warmup_bytes = 0

                # yield everything above the crossfade buffer size
                while len(crossfade_buffer) > crossfade_buffer_size:
                    yield bytes(crossfade_buffer[:pcm_sample_size])
                    bytes_written += pcm_sample_size
                    del crossfade_buffer[:pcm_sample_size]
                    await asyncio.sleep(0)

            #### HANDLE END OF TRACK
            if not first_chunk_received:
                self.logger.warning(
                    "Track %s (%s) on queue %s produced no audio data - skipping",
                    queue_track.name,
                    queue_track.streamdetails.uri if queue_track.streamdetails else "unknown",
                    queue.display_name,
                )
                queue_track.streamdetails.stream_error = True
                play_log_entry.seconds_streamed = 0
                continue
            if last_fadeout_part:
                # edge case: we did not get enough data to make the crossfade
                # attribute these bytes to the previous track (they are its tail)
                for pcm_slice in iter_pcm_slices(last_fadeout_part, pcm_format, 1000):
                    yield pcm_slice
                    await asyncio.sleep(0)
                # no crossfade happened — undo the eager seek_position
                queue_track.streamdetails.seek_position = raw_seek_position
                # full tail was pre-counted and is now yielded as-is
                last_fadeout_part = b""
            if self.crossfade_allowed(
                queue_track,
                crossfade_mode=crossfade_mode,
                player_id=queue.queue_id,
                flow_mode=True,
            ):
                last_fadeout_part = bytes(crossfade_buffer[-crossfade_buffer_size:])
                last_streamdetails = queue_track.streamdetails
                last_play_log_entry = play_log_entry
                remaining_bytes = bytes(crossfade_buffer[:-crossfade_buffer_size])
                if remaining_bytes:
                    for pcm_slice in iter_pcm_slices(remaining_bytes, pcm_format, 1000):
                        yield pcm_slice
                        await asyncio.sleep(0)
                    bytes_written += len(remaining_bytes)
                del remaining_bytes
            elif crossfade_mode != CrossfadeMode.DISABLED and crossfade_buffer:
                bytes_written += len(crossfade_buffer)
                for pcm_slice in iter_pcm_slices(bytes(crossfade_buffer), pcm_format, 1000):
                    yield pcm_slice
                    await asyncio.sleep(0)
            crossfade_buffer = bytearray()

            # update duration details based on the actual pcm data we sent
            # this also accounts for crossfade and silence stripping
            seconds_streamed = bytes_written / pcm_sample_size
            queue_track.streamdetails.seconds_streamed = seconds_streamed
            # the held-back crossfade tail still counts as this track's media-time
            tail_seconds = len(last_fadeout_part) / pcm_sample_size
            # streamdetails.duration is in media-time; seconds_streamed is stream-time
            # (post-atempo), so we scale by the track's playback_speed to recover media-time.
            queue_track.streamdetails.duration = int(
                queue_track.streamdetails.seek_position
                + (seconds_streamed + tail_seconds) * track_playback_speed
            )
            # propagate accurate duration to queue_item so UI displays it
            queue_track.duration = queue_track.streamdetails.duration
            play_log_entry.seconds_streamed = seconds_streamed
            play_log_entry.duration = queue_track.streamdetails.duration
            if last_play_log_entry is play_log_entry and last_fadeout_part:
                # Pre-count the full crossfade tail so the queue index calculation
                # doesn't undercount while waiting for the next track's crossfade mix.
                # This will be corrected to crossfade_total/2 once the mix completes.
                assert play_log_entry.seconds_streamed is not None
                play_log_entry.seconds_streamed += len(last_fadeout_part) / pcm_sample_size
            self.logger.debug(
                "Finished Streaming queue track: %s (%s) on queue %s",
                queue_track.streamdetails.uri,
                queue_track.name,
                queue.display_name,
            )
        #### HANDLE END OF QUEUE FLOW STREAM
        # skip end-of-queue bookkeeping if a newer producer has superseded us;
        # the new producer owns queue_buffer_completed and the play log now
        if _superseded():
            self.logger.debug(
                "Flow stream for queue %s superseded - skipping end-of-queue handling",
                queue.display_name,
            )
            return
        # end of queue flow: make sure we yield the last_fadeout_part
        if last_fadeout_part:
            for pcm_slice in iter_pcm_slices(last_fadeout_part, pcm_format, 1000):
                yield pcm_slice
                await asyncio.sleep(0)
            # correct seconds streamed - the duration already includes the tail
            last_part_seconds = len(last_fadeout_part) / pcm_sample_size
            streamdetails = queue_track.streamdetails
            assert streamdetails is not None
            streamdetails.seconds_streamed = (
                streamdetails.seconds_streamed or 0
            ) + last_part_seconds
            # also update the play log entry so elapsed time tracking stays in sync
            if last_play_log_entry:
                assert last_play_log_entry.seconds_streamed is not None
                # full tail was pre-counted and is now yielded as-is
                last_play_log_entry.duration = streamdetails.duration
            last_fadeout_part = b""
        self.logger.info("Finished Queue Flow stream for Queue %s", queue.display_name)
        # only signal completion if we are still the active producer — a later
        # producer would (incorrectly) see this as its own completion otherwise
        if not _superseded():
            # inform the queue controller that all audio data has been generated
            # so it can handle the case where new items were added after the flow stream ended
            self.mass.player_queues.queue_buffer_completed(queue.queue_id, queue_exhausted)

    async def get_overlay_mixed_stream(
        self,
        queue: PlayerQueue,
        audio_input: AsyncGenerator[bytes],
        pcm_format: AudioFormat,
    ) -> AsyncGenerator[bytes]:
        """
        Mix the queue's audio overlay (looping sound effect) into the given PCM stream.

        The mixed output has the exact same PCM format, duration and chunking as the
        input stream. If the overlay source can not be resolved, the original stream
        is passed through unchanged so playback is never interrupted.

        :param queue: The PlayerQueue holding the overlay source and volume.
        :param audio_input: The audio stream (raw PCM in ``pcm_format``) to mix into.
        :param pcm_format: PCM format of both the input and the mixed output.
        """
        overlay_input = await self._resolve_overlay_input(queue)
        if overlay_input is None:
            # overlay source unavailable: degrade gracefully to music-only
            async for chunk in audio_input:
                yield chunk
            return
        async for chunk in get_ffmpeg_overlay_stream(
            audio_input=audio_input,
            overlay_input=overlay_input,
            pcm_format=pcm_format,
            overlay_volume=queue.overlay_volume,
            chunk_size=pcm_format.pcm_sample_size,
        ):
            yield chunk

    def crossfade_allowed(
        self,
        queue_item: QueueItem,
        crossfade_mode: CrossfadeMode,
        player_id: str,
        flow_mode: bool = False,
        next_queue_item: QueueItem | None = None,
        sample_rate: int | None = None,
        next_sample_rate: int | None = None,
    ) -> bool:
        """Get the crossfade config for a queue item."""
        if crossfade_mode == CrossfadeMode.DISABLED:
            return False
        if not (self.mass.player_queues.get(queue_item.queue_id)):
            return False  # just a guard
        if not (self.mass.players.get_player(player_id)):
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
                "streams", CONF_ALLOW_CROSSFADE_SAME_ALBUM, False
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
                player_id,
                CONF_ENTRY_CROSSFADE_DIFFERENT_SAMPLE_RATES.key,
                CONF_ENTRY_CROSSFADE_DIFFERENT_SAMPLE_RATES.default_value,
            )
        ):
            self.logger.debug(
                "Skipping crossfade: player(protocol) does not support gapless playback "
                "with different sample rates (%s vs %s)",
                sample_rate,
                next_sample_rate,
            )
            return False

        return True

    def clear_crossfade_data(self, queue_id: str) -> None:
        """
        Clear any pending crossfade data for a queue.

        :param queue_id: The queue ID to clear crossfade data for.
        """
        if queue_id in self._crossfade_data:
            self.logger.debug("Clearing crossfade data for queue %s", queue_id)
            del self._crossfade_data[queue_id]

    async def get_shoutcast_stream(
        self, url: str, streamdetails: StreamDetails
    ) -> AsyncGenerator[bytes]:
        """
        Yield audio from a legacy Shoutcast server, with ICY metadata parsed inline.

        :param url: Shoutcast stream URL.
        :param streamdetails: StreamDetails to update with ICY metadata as it arrives.
        """
        self.logger.debug("Start streaming from legacy Shoutcast server: %s", url)

        parsed = urlparse(url)
        host = parsed.hostname
        port = parsed.port or 80
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"

        try:
            # Open raw socket connection
            reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=30)
        except TimeoutError as err:
            raise AudioError(f"Timeout connecting to Shoutcast stream {url}") from err
        except (OSError, ConnectionError) as err:
            raise AudioError(f"Failed to connect to Shoutcast stream {url}") from err

        try:
            # Send HTTP request with ICY metadata header
            request = (
                f"GET {path} HTTP/1.1\r\n"
                f"Host: {host}\r\n"
                f"User-Agent: {HTTP_HEADERS['User-Agent']}\r\n"
                f"Icy-MetaData: 1\r\n\r\n"
            )
            writer.write(request.encode())
            await writer.drain()

            # Read and parse response line
            try:
                response_line = await asyncio.wait_for(reader.readline(), timeout=10)
            except TimeoutError as err:
                raise AudioError("Timeout reading Shoutcast response") from err

            if not response_line.startswith(b"ICY"):
                raise InvalidDataError("Invalid Shoutcast response")

            # Read headers until empty line
            headers: dict[str, str] = {}
            while True:
                try:
                    line = await asyncio.wait_for(reader.readline(), timeout=5)
                except TimeoutError as err:
                    raise AudioError("Timeout reading Shoutcast headers") from err

                if line in (b"\r\n", b"\n", b""):
                    break

                if b":" in line:
                    try:
                        key, value = line.decode("latin-1", errors="ignore").split(":", 1)
                        headers[key.strip().lower()] = value.strip()
                    except UnicodeDecodeError, ValueError:
                        continue

            # Get metadata interval
            meta_int_str = headers.get("icy-metaint")
            if not meta_int_str:
                raise InvalidDataError("No icy-metaint header in Shoutcast response")

            try:
                meta_int = int(meta_int_str)
            except ValueError as err:
                raise InvalidDataError("Invalid icy-metaint value") from err

            self.logger.debug("Connected to Shoutcast stream %s (icy-metaint: %s)", url, meta_int)

            # Stream audio data with metadata parsing
            while True:
                try:
                    # Read audio chunk
                    audio_chunk = await reader.readexactly(meta_int)
                    yield audio_chunk

                    # Read metadata length
                    meta_byte = await reader.readexactly(1)
                    if meta_byte == b"\x00":
                        continue

                    meta_length = ord(meta_byte) * 16
                    meta_data = await reader.readexactly(meta_length)
                    self._parse_icy_metadata(meta_data, streamdetails)

                except asyncio.exceptions.IncompleteReadError:
                    # End of stream
                    break

        finally:
            writer.close()
            await writer.wait_closed()

    # --- Private methods ---

    def _notify_provider_streamed(
        self, streamdetails: StreamDetails, finished: bool, seconds_streamed: float
    ) -> None:
        """Report a (mostly) streamed item back to the provider that owns it."""
        if not finished and seconds_streamed < 90:
            return
        provider = self.mass.get_provider(streamdetails.provider)
        # plugin providers serve playable items too, but on_streamed is MusicProvider-only
        if provider is None or provider.type != ProviderType.MUSIC:
            return
        music_prov = cast("MusicProvider", provider)
        self.mass.create_task(music_prov.on_streamed(streamdetails))

    def _get_volume_normalization_preference(
        self, streamdetails: StreamDetails
    ) -> VolumeNormalizationMode:
        """Return the configured normalization preference for the stream's media type."""
        conf_key = (
            CONF_VOLUME_NORMALIZATION_RADIO
            if streamdetails.media_type == MediaType.RADIO
            else CONF_VOLUME_NORMALIZATION_TRACKS
        )
        return VolumeNormalizationMode(
            self.mass.streams.get_config_value(conf_key, return_type=str)
        )

    def _update_radio_stream_metadata(
        self,
        streamdetails: StreamDetails,
        artist: str | None,
        title: str,
        image_url: str | None = None,
        album: str | None = None,
    ) -> None:
        """
        Update radio stream metadata and trigger artwork lookup.

        :param streamdetails: The stream details to update.
        :param artist: Artist name (will be normalized).
        :param title: Track title (will be cleaned for display).
        :param image_url: Optional image URL from stream metadata.
        :param album: Optional album name.
        """
        station_image_url = image_url or self.mass.metadata.get_radio_stream_station_image(
            streamdetails
        )
        artist_normalized = (
            self.mass.metadata.normalize_radio_artist_name(artist) if artist else None
        )
        display_title, _ = parse_title_and_version(title, strip_for_display=True)

        streamdetails.stream_metadata = StreamMetadata(
            title=display_title,
            artist=artist_normalized,
            album=album,
            image_url=station_image_url,
        )
        streamdetails.stream_metadata_last_updated = time.time()
        if streamdetails.queue_id:
            self.mass.player_queues.signal_update(streamdetails.queue_id)

        # Fetch artwork in background (track, album then artist)
        if artist and title and not image_url:
            self.mass.call_later(
                0.2,
                self.mass.metadata.update_radio_stream_artwork,
                streamdetails,
                task_id=f"update_radio_artwork_{streamdetails.queue_id}",
            )

    async def _cache_radio_result(
        self,
        url: str,
        stream_type: StreamType,
        resolved_url: str | None = None,
    ) -> tuple[str, StreamType]:
        """Cache and return a radio stream resolution result."""
        result = (resolved_url or url, stream_type)
        await self.mass.cache.set(
            url,
            result,
            expiration=3600 * 3,
            provider=CACHE_PROVIDER,
            category=CACHE_CATEGORY_RESOLVED_RADIO_URL,
        )
        return result

    async def _handle_client_error_for_radio_stream(
        self, url: str, err: aiohttp.ClientError, fallback_stream_type: StreamType
    ) -> tuple[str, StreamType]:
        """Handle aiohttp client errors during radio stream resolution."""
        # Prefer the final post-redirect URL: aiohttp follows redirects before raising,
        # but the original url may just point at a redirector rather than the ICY endpoint.
        request_info = getattr(err, "request_info", None)
        validate_url = str(request_info.url) if request_info is not None else url

        # Check if this is a Shoutcast/ICY response that aiohttp can't parse
        if isinstance(err, aiohttp.ClientResponseError) and "ICY" in str(err).upper():
            self.logger.debug(
                "ICY response detected for %s, validating Shoutcast stream", validate_url
            )
            if await self._validate_shoutcast_stream(validate_url):
                return await self._cache_radio_result(
                    url, StreamType.SHOUTCAST, resolved_url=validate_url
                )
            self.logger.warning(
                "ICY response detected but Shoutcast validation failed for %s", validate_url
            )
            return await self._cache_radio_result(
                url, fallback_stream_type, resolved_url=validate_url
            )

        # Other aiohttp errors - might still be Shoutcast, check it
        self.logger.debug("aiohttp error for %s, checking if legacy Shoutcast stream", validate_url)
        if await self._validate_shoutcast_stream(validate_url):
            return await self._cache_radio_result(
                url, StreamType.SHOUTCAST, resolved_url=validate_url
            )

        # Unknown error - still try to stream
        self.logger.warning(
            "Failed to parse radio URL %s: %s - attempting direct stream", validate_url, str(err)
        )
        return await self._cache_radio_result(url, fallback_stream_type, resolved_url=validate_url)

    async def _iter_audio_source_pcm(
        self,
        streamdetails: StreamDetails,
        pcm_format: AudioFormat,
    ) -> AsyncGenerator[bytes]:
        """Yield PCM for an AudioSource, bypassing ffmpeg when formats match."""
        if _pcm_formats_match(streamdetails.audio_format, pcm_format):
            source_gen = self._open_audio_source_generator(streamdetails)
            async for chunk in realtime_pcm_pacer(source_gen, pcm_format):
                yield chunk
            return
        # format mismatch → fall back to ffmpeg for resampling (still small chunks)
        async for chunk in self.get_media_stream(
            streamdetails=streamdetails,
            pcm_format=pcm_format,
            filter_params=None,
            chunk_seconds=AUDIO_SOURCE_CHUNK_SECONDS,
        ):
            yield chunk

    def _open_audio_source_generator(self, streamdetails: StreamDetails) -> AsyncGenerator[bytes]:
        """Open the raw PCM generator for an AudioSource (CUSTOM or NAMED_PIPE)."""
        if streamdetails.stream_type == StreamType.CUSTOM:
            provider = self.mass.get_provider(streamdetails.provider)
            if provider is None:
                raise ProviderUnavailableError(
                    f"Provider {streamdetails.provider} for stream is no longer available"
                )
            provider = cast("MusicProvider | PluginProvider", provider)
            return provider.get_audio_stream(streamdetails)
        if streamdetails.stream_type == StreamType.NAMED_PIPE:
            assert isinstance(streamdetails.path, str)  # for type checking
            return read_named_pipe(streamdetails.path)
        raise AudioError(f"Unsupported stream_type {streamdetails.stream_type} for AudioSource")

    def _parse_icy_metadata(self, meta_data: bytes, streamdetails: StreamDetails) -> None:
        """
        Parse ICY metadata and update streamdetails.

        Sets the cleaned stream title and, when the title parses as "Artist - Track",
        triggers a radio-artwork metadata update.

        :param meta_data: Raw metadata bytes from an ICY stream chunk.
        :param streamdetails: StreamDetails to update with parsed title and metadata.
        """
        if not meta_data:
            return

        meta_data = meta_data.rstrip(b"\0")
        # Match StreamTitle, handling apostrophes in titles
        stream_title_re = re.search(rb"StreamTitle='(.*?)';", meta_data)

        if not stream_title_re:
            self.logger.log(
                VERBOSE_LOG_LEVEL,
                "ICY metadata does not contain StreamTitle field. Raw: %s",
                meta_data.decode("utf-8", errors="replace")[:200],
            )
            return

        try:
            # in 99% of the cases the stream title is utf-8 encoded
            stream_title = stream_title_re.group(1).decode("utf-8")
        except UnicodeDecodeError:
            # fallback to iso-8859-1
            stream_title = stream_title_re.group(1).decode("iso-8859-1", errors="replace")

        cleaned_stream_title = clean_stream_title(stream_title)

        if not cleaned_stream_title:
            return

        if cleaned_stream_title == streamdetails.stream_title:
            return

        self.logger.log(VERBOSE_LOG_LEVEL, "ICY Radio streamtitle original: %s", stream_title)
        self.logger.log(
            VERBOSE_LOG_LEVEL, "ICY Radio streamtitle cleaned: %s", cleaned_stream_title
        )
        streamdetails.stream_title = cleaned_stream_title

        # Prefer station-provided cover art from the ICY 'StreamUrl' field (when it is
        # an image) over the MusicBrainz artwork lookup in _update_radio_stream_metadata.
        image_url = self._parse_icy_image_url(meta_data)

        # Parse the original title for structured fields first so stations that announce
        # an album can refine the artwork lookup; fall back to the "Artist - Track" split.
        album: str | None = None
        if parsed := parse_quoted_stream_title(stream_title):
            track_name, artist_name_raw, album = parsed
        elif " - " in cleaned_stream_title:
            artist_name_raw, track_name = (
                part.strip() for part in cleaned_stream_title.split(" - ", 1)
            )
        else:
            return

        if artist_name_raw and track_name:
            self.logger.debug(
                "ICY metadata: artist='%s', track='%s', album='%s'",
                artist_name_raw,
                track_name,
                album,
            )
            self._update_radio_stream_metadata(
                streamdetails,
                artist=artist_name_raw,
                title=track_name,
                album=album,
                image_url=image_url,
            )

    def _parse_icy_image_url(self, meta_data: bytes) -> str | None:
        """
        Return a PNG or JPEG cover-art URL from the ICY 'StreamUrl' field, if present.

        :param meta_data: Raw metadata bytes from an ICY stream chunk.
        """
        # The trailing semicolon is optional to match sources that omit it.
        stream_url_re = re.search(rb"StreamUrl='([^']*)'", meta_data)
        if not stream_url_re:
            return None
        try:
            image_url = stream_url_re.group(1).decode("utf-8").strip()
        except UnicodeDecodeError:
            return None
        if not image_url:
            return None
        # StreamUrl is not a standardized artwork field (reference clients such as VLC
        # ignore it and it conventionally holds a station website link), so only accept
        # values that point at a PNG or JPEG image.
        parsed = urlparse(image_url)
        if parsed.scheme not in ("http", "https"):
            return None
        if not parsed.path.lower().endswith((".png", ".jpg", ".jpeg")):
            return None
        self.logger.debug("ICY metadata: StreamUrl image='%s'", image_url)
        return image_url

    async def _validate_shoutcast_stream(self, url: str) -> bool:
        """
        Return True if the URL responds with a legacy Shoutcast "ICY 200 OK" line.

        :param url: The URL to validate.
        """
        try:
            parsed = urlparse(url)
            host = parsed.hostname
            port = parsed.port or 80
            path = parsed.path or "/"
            if parsed.query:
                path = f"{path}?{parsed.query}"

            # Open raw socket connection with timeout
            reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=10)
            try:
                # Send minimal HTTP request with ICY metadata header
                request = f"GET {path} HTTP/1.1\r\nHost: {host}\r\nIcy-MetaData: 1\r\n\r\n"
                writer.write(request.encode())
                await writer.drain()

                # Read just the response line
                response_line = await asyncio.wait_for(reader.readline(), timeout=5)
            finally:
                writer.close()
                await writer.wait_closed()

            # Check if response starts with "ICY"
            decoded_line = response_line.decode("latin-1", errors="ignore").strip()
            return decoded_line.startswith("ICY")

        except TimeoutError:
            self.logger.debug("Timeout during Shoutcast validation for %s", url)
            return False
        except OSError, ConnectionError:
            self.logger.debug("Connection failed during Shoutcast validation for %s", url)
            return False
        except UnicodeDecodeError:
            self.logger.debug("Invalid response encoding during Shoutcast validation for %s", url)
            return False

    def _resolve_player_dsp_config(self, player: Player) -> DSPConfig:
        """
        Resolve the effective DSP config for a player.

        Single source of truth shared by every code path that needs to know
        whether DSP will run for this player. Protocol wrappers defer to their
        parent player; single-leg ``player_group`` instances that don't expose
        ``MULTI_DEVICE_DSP`` defer to their first member; players whose grouping
        context prevents DSP get a disabled config back regardless.

        :param player: The player to resolve DSP config for.
        """
        dsp_player_id = self._resolve_player_dsp_config_id(player)
        dsp = self.mass.config.get_player_dsp_config(dsp_player_id)
        if is_grouping_preventing_dsp(player):
            dsp.enabled = False
        elif player.provider.domain == "player_group" and (
            PlayerFeature.MULTI_DEVICE_DSP not in player.state.supported_features
        ):
            if not player.state.group_members:
                dsp.enabled = False
        return dsp

    def _resolve_player_dsp_config_id(self, player: Player) -> str:
        """
        Return the player identifier that supplies the effective DSP config.

        :param player: Player whose DSP config source should be resolved.
        """
        dsp_player_id = player.protocol_parent_id or player.player_id
        if (
            not is_grouping_preventing_dsp(player)
            and player.provider.domain == "player_group"
            and PlayerFeature.MULTI_DEVICE_DSP not in player.state.supported_features
            and player.state.group_members
        ):
            child_player = self.mass.players.get_player(player.state.group_members[0])
            assert child_player is not None
            dsp_player_id = child_player.player_id
        return dsp_player_id

    def _get_output_channels(self, player: Player | None, player_id: str) -> str:
        """
        Return the configured output channels for the rendering player.

        The value may be stored on the rendering player(protocol) itself (the
        protocol section of the config UI) or on its visible parent player (the
        native section); the rendering player's own stored value wins.
        """
        parent_id = player.protocol_parent_id if player and player.protocol_parent_id else player_id
        parent_value = self.mass.config.get_raw_player_config_value(
            parent_id, CONF_OUTPUT_CHANNELS, "stereo"
        )
        return self.mass.config.get_raw_player_config_value(
            player.player_id if player else player_id, CONF_OUTPUT_CHANNELS, parent_value
        )

    def _pick_pcm_bit_depth(
        self,
        players: Iterable[Player],
        streamdetails: StreamDetails | None,
        crossfade_enabled: bool,
        overlay_active: bool = False,
    ) -> tuple[ContentType, int]:
        """
        Return ``(content_type, bit_depth)`` for an internal PCM stream.

        F32 is chosen when audio processing (crossfade, audio overlay, volume
        normalization, DSP) will run on the stream — those need the extra
        headroom to avoid clipping and precision loss. Otherwise the source's
        native bit depth is reused so we don't waste memory upcasting a 16-bit
        stream to 32-bit just to pass it through. When the source is unknown
        (no streamdetails) we fall back to F32 conservatively.
        """
        if streamdetails is None:
            return INTERNAL_PCM_FORMAT.content_type, INTERNAL_PCM_FORMAT.bit_depth
        needs_headroom = (
            crossfade_enabled
            or overlay_active
            or streamdetails.volume_normalization_mode != VolumeNormalizationMode.DISABLED
            or any(self._resolve_player_dsp_config(player).enabled for player in players)
        )
        if needs_headroom:
            return INTERNAL_PCM_FORMAT.content_type, INTERNAL_PCM_FORMAT.bit_depth
        bit_depth = streamdetails.audio_format.bit_depth
        return ContentType.from_bit_depth(bit_depth), bit_depth

    def _select_audio_source_pcm_format(
        self,
        player: Player,
        streamdetails: StreamDetails,
        supported_sample_rates: Iterable[int] | None = None,
    ) -> AudioFormat:
        """
        Return a passthrough PCM format for a realtime AudioSource item.

        The format matches the source's native sample rate, bit depth and
        channel count whenever the player can accept them; if the player does
        not support the source's sample rate, it is snapped down to the
        closest supported rate. No F32 widening — realtime sources skip every
        processing stage that would otherwise need it. Surround sources are
        still folded down to stereo, which every output path requires anyway.

        :param player: The player requesting the stream.
        :param streamdetails: Stream details for the AudioSource item.
        :param supported_sample_rates: Rates shared by every output player, if applicable.
        """
        resolved_sample_rates = (
            list(supported_sample_rates)
            if supported_sample_rates is not None
            else [sample_rate for sample_rate, _ in player.get_supported_sample_rates()]
        )
        source_rate = streamdetails.audio_format.sample_rate
        if source_rate in resolved_sample_rates:
            output_sample_rate = source_rate
        else:
            output_sample_rate = max(
                (rate for rate in resolved_sample_rates if rate <= source_rate),
                default=min(resolved_sample_rates),
            )
        bit_depth = streamdetails.audio_format.bit_depth
        return AudioFormat(
            content_type=ContentType.from_bit_depth(bit_depth),
            sample_rate=output_sample_rate,
            bit_depth=bit_depth,
            # a realtime source may announce more channels than anything downstream can
            # carry (a VBAN stream can be configured up to 8), and player handoff formats
            # copy this count straight through, so fold it here
            channels=min(streamdetails.audio_format.channels, 2),
        )

    def _flow_restart_context(
        self, queue_id: str, protocol_player: Player | None
    ) -> tuple[str, list[int]]:
        """
        Resolve the flow mode config and supported sample rates for restart decisions.

        Prefers the protocol player actually consuming the flow stream over the
        queue's (wrapper) player, whose config may lack the audio specific entries.
        """
        if protocol_player is None:
            protocol_player = self.mass.players.get_player(queue_id)
        if protocol_player is None:
            flow_mode_sample_rate_conf = self.mass.config.get_raw_player_config_value(
                queue_id, CONF_FLOW_MODE_SAMPLE_RATE, FLOW_MODE_SAMPLE_RATE_SMART
            )
            return flow_mode_sample_rate_conf, []
        flow_mode_sample_rate_conf = cast(
            "str",
            protocol_player.config.get_value(
                CONF_FLOW_MODE_SAMPLE_RATE, FLOW_MODE_SAMPLE_RATE_SMART
            ),
        )
        supported_sample_rates = sorted(
            {sr for sr, _ in protocol_player.get_supported_sample_rates()}
        )
        return flow_mode_sample_rate_conf, supported_sample_rates

    def _flow_stream_needs_restart(
        self,
        queue_track: QueueItem,
        pcm_format: AudioFormat,
        supported_sample_rates: list[int],
        flow_mode_sample_rate_conf: str,
        is_first_track: bool,
    ) -> bool:
        """
        Return True if the upcoming queue track requires exiting the flow stream.

        Covers every case where the flow loop should break and hand control back to
        the queue controller for restart:

        - Live media (radio, audio sources): cannot be played inside a flow,
          the controller will fall back to a single-item stream.
        - Sample rate mismatch ('smart' / 'bit_perfect' modes only): the next
          track's sample rate (snapped up to the closest supported player rate,
          mirroring select_flow_pcm_format's anchoring logic) is incompatible with
          the current flow rate, so a new flow must be opened.

        The first (anchor) track is always allowed to continue for the sample
        rate check; select_flow_pcm_format has already snapped the flow rate to it.

        :param queue_track: The upcoming queue item.
        :param pcm_format: The current flow stream's PCM format.
        :param supported_sample_rates: Sorted list of the player's supported rates.
        :param flow_mode_sample_rate_conf: The flow mode sample rate config value.
        :param is_first_track: Whether this is the first track of the flow stream.
        """
        # live audio (radio, plugin or audio source) cannot be flowed; let the
        # queue controller fall back to single-item streaming for this item
        if queue_track.media_type in (MediaType.RADIO, MediaType.AUDIO_SOURCE):
            self.logger.info(
                "Live media item %s (%s, %s) encountered in flow stream "
                "- breaking out to single item stream",
                queue_track.queue_item_id,
                queue_track.name,
                queue_track.media_type,
            )
            return True

        if is_first_track or queue_track.streamdetails is None:
            return False
        raw_next_rate = queue_track.streamdetails.audio_format.sample_rate
        if not raw_next_rate or not supported_sample_rates:
            return False
        effective_next_rate = _snap_supported_rate_up(raw_next_rate, supported_sample_rates)

        # branch order mirrors select_flow_pcm_format: fixed-rate modes resample
        # everything to the chosen rate (no restart); bit_perfect restarts on any
        # mismatch; anything else falls through to smart-anchor behavior so
        # unknown/legacy config values don't silently pin the flow forever.
        if flow_mode_sample_rate_conf in (
            FLOW_MODE_SAMPLE_RATE_48000,
            FLOW_MODE_SAMPLE_RATE_96000,
            FLOW_MODE_SAMPLE_RATE_HIGHEST,
        ):
            needs_restart = False
        elif flow_mode_sample_rate_conf == FLOW_MODE_SAMPLE_RATE_BIT_PERFECT:
            needs_restart = effective_next_rate != pcm_format.sample_rate
        else:
            needs_restart = effective_next_rate > pcm_format.sample_rate

        if needs_restart:
            self.logger.info(
                "Track %s (%s) sample rate %s (snapped to %s) incompatible with flow rate %s "
                "(mode: %s) - breaking out to restart flow stream",
                queue_track.queue_item_id,
                queue_track.name,
                raw_next_rate,
                effective_next_rate,
                pcm_format.sample_rate,
                flow_mode_sample_rate_conf,
            )
        return needs_restart

    @asynccontextmanager
    async def _connect_radio_stream(self, url: str, **kwargs: Any) -> AsyncGenerator[Any]:
        """
        Connect to a radio stream URL with fallback for legacy SSL/TLS configurations.

        Some radio servers use outdated TLS configurations that reject modern
        cipher suites. Since radio streams are public broadcast content,
        relaxing cipher requirements is acceptable.

        :param url: The radio stream URL to connect to.
        :param kwargs: Additional keyword arguments passed to aiohttp get().
        """
        request_url = encoded_request_url(url)
        try:
            async with self.mass.http_session_no_ssl.get(request_url, **kwargs) as resp:
                yield resp
        except ClientConnectorSSLError:
            self.logger.info(
                "SSL handshake failed for %s, retrying with permissive cipher configuration", url
            )
            insecure_ssl_context = ssl_util.client_context_no_verify(
                ssl_util.SSLCipherList.INSECURE
            )
            async with self.mass.http_session_no_ssl.get(
                request_url, ssl=insecure_ssl_context, **kwargs
            ) as resp:
                yield resp

    async def _update_hls_radio_metadata(
        self,
        streamdetails: StreamDetails,
        elapsed_time: int,
    ) -> None:
        """
        Update HLS radio stream metadata by fetching the playlist.

        Fetches the HLS playlist and extracts metadata from EXTINF lines.

        :param streamdetails: StreamDetails object to update with metadata
        :param elapsed_time: Current playback position in seconds (unused for live radio)
        """
        mass = self.mass
        try:
            # Get the actual media playlist URL from cache or resolve it
            # We cache the media_playlist_url in streamdetails.data to avoid re-resolving
            if streamdetails.data is None:
                streamdetails.data = {}
            media_playlist_url = streamdetails.data.get("hls_media_playlist_url")
            if not media_playlist_url:
                try:
                    assert isinstance(streamdetails.path, str)  # for type checking
                    substream = await self.get_hls_substream(streamdetails.path)
                    media_playlist_url = substream.path
                    streamdetails.data["hls_media_playlist_url"] = media_playlist_url
                except Exception as err:
                    self.logger.warning(
                        "Failed to resolve HLS substream for metadata monitoring: %s", err
                    )
                    return

            # Fetch the media playlist
            timeout = ClientTimeout(total=0, connect=10, sock_read=30)
            try:
                async with mass.http_session_no_ssl.get(
                    encoded_request_url(media_playlist_url), timeout=timeout
                ) as resp:
                    resp.raise_for_status()
                    playlist_content = await resp.text()
            except ClientResponseError as err:
                # Session token likely expired (410/403) — drop cache so next poll re-resolves
                if err.status in (403, 410):
                    streamdetails.data.pop("hls_media_playlist_url", None)
                raise

            # Parse the playlist and look for EXTINF metadata
            # The most recent segment usually has the current metadata
            lines = playlist_content.strip().split("\n")
            for line in reversed(lines):
                if line.startswith("#EXTINF:"):
                    # Extract metadata from EXTINF line
                    metadata = parse_extinf_metadata(line)

                    # Build stream title from title and artist
                    title = metadata.get("title", "")
                    artist = metadata.get("artist", "")
                    image_url = (
                        metadata.get("image") or metadata.get("artwork") or metadata.get("cover")
                    )
                    if not artist and " - " in title:
                        artist, title = title.split(" - ", 1)
                    if title or artist:
                        # Format as "Artist - Title"
                        if artist and title:
                            stream_title = f"{artist} - {title}"
                        elif title:
                            stream_title = title
                        else:
                            stream_title = artist

                        # Clean the stream title
                        cleaned_title = clean_stream_title(stream_title)

                        # Only update if changed
                        if cleaned_title != streamdetails.stream_title and cleaned_title:
                            self.logger.log(
                                VERBOSE_LOG_LEVEL, "HLS Radio metadata updated: %s", cleaned_title
                            )
                            streamdetails.stream_title = cleaned_title
                            self._update_radio_stream_metadata(
                                streamdetails,
                                artist=artist or None,
                                title=title or cleaned_title,
                                image_url=image_url,
                            )

                    # Only check the most recent EXTINF
                    break

        except Exception as err:
            self.logger.debug("Error fetching HLS metadata: %s", err)

    @staticmethod
    def _normalize_reconnecting_urls(url: str | list[MultiPartPath]) -> list[str]:
        """Normalize a single URL or a sequence into a non-empty list."""
        if isinstance(url, str):
            return [url]
        if not url:
            msg = "Radio stream requires at least one URL"
            raise InvalidDataError(msg)
        return [part.path for part in url]

    async def _resolve_overlay_input(self, queue: PlayerQueue) -> str | None:
        """
        Resolve the queue's overlay source to a file path or URL for ffmpeg.

        Returns None (with a warning logged) when the source can not be resolved,
        so the caller can degrade to music-only playback.
        """
        if not (mapping := queue.overlay_source):
            return None
        try:
            provider = self.mass.get_provider(mapping.provider)
            if provider is None:
                raise MediaNotFoundError(f"Provider {mapping.provider} is not available")
            stream_prov = cast("MusicProvider | PluginProvider", provider)
            streamdetails = await stream_prov.get_stream_details(
                mapping.item_id, MediaType.SOUND_EFFECT
            )
        except Exception as err:
            self.logger.warning(
                "Audio overlay source %s is unavailable (%s) - continuing without overlay",
                mapping.uri,
                str(err) or err.__class__.__name__,
            )
            return None
        if streamdetails.stream_type not in (StreamType.LOCAL_FILE, StreamType.HTTP) or not (
            isinstance(streamdetails.path, str)
        ):
            self.logger.warning(
                "Audio overlay source %s uses unsupported stream type %s "
                "- continuing without overlay",
                mapping.uri,
                streamdetails.stream_type,
            )
            return None
        if streamdetails.stream_type == StreamType.LOCAL_FILE and not await aiofiles.os.path.isfile(
            streamdetails.path
        ):
            # guard against stale sources: feeding a missing file to the mixer would
            # kill the whole (music) stream instead of just the overlay
            self.logger.warning(
                "Audio overlay source %s does not exist - continuing without overlay",
                streamdetails.path,
            )
            return None
        return streamdetails.path
