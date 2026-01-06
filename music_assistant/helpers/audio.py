"""Various helpers for audio streaming and manipulation."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import struct
import time
from collections.abc import AsyncGenerator
from io import BytesIO
from typing import TYPE_CHECKING, Final, cast

import aiofiles
import shortuuid
from aiohttp import ClientTimeout
from music_assistant_models.dsp import DSPConfig, DSPDetails, DSPState
from music_assistant_models.enums import (
    ContentType,
    MediaType,
    PlayerFeature,
    PlayerType,
    StreamType,
    VolumeNormalizationMode,
)
from music_assistant_models.errors import (
    AudioError,
    InvalidDataError,
    MediaNotFoundError,
    MusicAssistantError,
    ProviderUnavailableError,
)
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import MultiPartPath

from music_assistant.constants import (
    CONF_ENTRY_OUTPUT_LIMITER,
    CONF_OUTPUT_CHANNELS,
    CONF_VOLUME_NORMALIZATION,
    CONF_VOLUME_NORMALIZATION_RADIO,
    CONF_VOLUME_NORMALIZATION_TARGET,
    CONF_VOLUME_NORMALIZATION_TRACKS,
    MASS_LOGGER_NAME,
    VERBOSE_LOG_LEVEL,
)
from music_assistant.controllers.players.sync_groups import SyncGroupPlayer
from music_assistant.helpers.json import JSON_DECODE_EXCEPTIONS, json_loads
from music_assistant.helpers.throttle_retry import BYPASS_THROTTLER
from music_assistant.helpers.util import clean_stream_title, remove_file

from .audio_buffer import AudioBuffer
from .dsp import filter_to_ffmpeg_params
from .ffmpeg import FFMpeg, get_ffmpeg_args, get_ffmpeg_stream
from .playlists import IsHLSPlaylist, PlaylistItem, fetch_playlist, parse_m3u
from .process import AsyncProcess, communicate
from .util import detect_charset

if TYPE_CHECKING:
    from music_assistant_models.config_entries import CoreConfig, PlayerConfig
    from music_assistant_models.queue_item import QueueItem
    from music_assistant_models.streamdetails import StreamDetails

    from music_assistant.mass import MusicAssistant
    from music_assistant.models.music_provider import MusicProvider
    from music_assistant.models.player import Player

LOGGER = logging.getLogger(f"{MASS_LOGGER_NAME}.audio")

# ruff: noqa: PLR0915

HTTP_HEADERS = {"User-Agent": "Lavf/60.16.100.MusicAssistant"}
HTTP_HEADERS_ICY = {**HTTP_HEADERS, "Icy-MetaData": "1"}

SLOW_PROVIDERS = ("tidal", "ytmusic", "apple_music")

CACHE_CATEGORY_RESOLVED_RADIO_URL: Final[int] = 100
CACHE_PROVIDER: Final[str] = "audio"


def align_audio_to_frame_boundary(audio_data: bytes, pcm_format: AudioFormat) -> bytes:
    """Align audio data to frame boundaries by truncating incomplete frames.

    :param audio_data: Raw PCM audio data to align.
    :param pcm_format: AudioFormat of the audio data.
    """
    bytes_per_sample = pcm_format.bit_depth // 8
    frame_size = bytes_per_sample * pcm_format.channels
    valid_bytes = (len(audio_data) // frame_size) * frame_size
    if valid_bytes != len(audio_data):
        LOGGER.debug(
            "Truncating %d bytes from audio buffer to align to frame boundary",
            len(audio_data) - valid_bytes,
        )
        return audio_data[:valid_bytes]
    return audio_data


async def strip_silence(
    mass: MusicAssistant,  # noqa: ARG001
    audio_data: bytes,
    pcm_format: AudioFormat,
    reverse: bool = False,
) -> bytes:
    """Strip silence from begin or end of pcm audio using ffmpeg."""
    args = ["ffmpeg", "-hide_banner", "-loglevel", "quiet"]
    args += [
        "-acodec",
        pcm_format.content_type.name.lower(),
        "-f",
        pcm_format.content_type.value,
        "-ac",
        str(pcm_format.channels),
        "-ar",
        str(pcm_format.sample_rate),
        "-i",
        "-",
    ]
    # filter args
    if reverse:
        args += [
            "-af",
            "areverse,atrim=start=0.2,silenceremove=start_periods=1:start_silence=0.1:start_threshold=0.02,areverse",
        ]
    else:
        args += [
            "-af",
            "atrim=start=0.2,silenceremove=start_periods=1:start_silence=0.1:start_threshold=0.02",
        ]
    # output args
    args += ["-f", pcm_format.content_type.value, "-"]
    _returncode, stripped_data, _stderr = await communicate(args, audio_data)

    # return stripped audio
    bytes_stripped = len(audio_data) - len(stripped_data)
    if LOGGER.isEnabledFor(VERBOSE_LOG_LEVEL):
        seconds_stripped = round(bytes_stripped / pcm_format.pcm_sample_size, 2)
        location = "end" if reverse else "begin"
        LOGGER.log(
            VERBOSE_LOG_LEVEL,
            "stripped %s seconds of silence from %s of pcm audio. bytes stripped: %s",
            seconds_stripped,
            location,
            bytes_stripped,
        )
    return stripped_data


def get_player_dsp_details(
    mass: MusicAssistant, player: Player, group_preventing_dsp: bool = False
) -> DSPDetails:
    """Return DSP details of single a player.

    This will however not check if the queried player is part of a group.
    The caller is responsible for passing the result of is_grouping_preventing_dsp of
    the leader/PlayerGroup as the group_preventing_dsp argument in such cases.
    """
    dsp_config = mass.config.get_player_dsp_config(player.player_id)
    dsp_state = DSPState.ENABLED if dsp_config.enabled else DSPState.DISABLED
    if dsp_state == DSPState.ENABLED and (
        group_preventing_dsp or is_grouping_preventing_dsp(player)
    ):
        dsp_state = DSPState.DISABLED_BY_UNSUPPORTED_GROUP
        dsp_config = DSPConfig(enabled=False)
    elif dsp_state == DSPState.DISABLED:
        # DSP is disabled by the user, remove all filters
        dsp_config = DSPConfig(enabled=False)

    # remove disabled filters
    dsp_config.filters = [x for x in dsp_config.filters if x.enabled]

    output_limiter = is_output_limiter_enabled(mass, player)
    return DSPDetails(
        state=dsp_state,
        input_gain=dsp_config.input_gain,
        filters=dsp_config.filters,
        output_gain=dsp_config.output_gain,
        output_limiter=output_limiter,
        output_format=player.extra_data.get("output_format", None),
    )


def get_stream_dsp_details(
    mass: MusicAssistant,
    queue_id: str,
) -> dict[str, DSPDetails]:
    """Return DSP details of all players playing this queue, keyed by player_id."""
    player = mass.players.get(queue_id)
    dsp: dict[str, DSPDetails] = {}
    assert player is not None  # for type checking
    group_preventing_dsp = is_grouping_preventing_dsp(player)
    output_format = None
    is_external_group = False

    if player.type == PlayerType.GROUP and isinstance(player, SyncGroupPlayer):
        if group_preventing_dsp:
            if sync_leader := player.sync_leader:
                output_format = sync_leader.extra_data.get("output_format", None)
    else:
        # We only add real players (so skip the PlayerGroups as they only sync containing players)
        details = get_player_dsp_details(mass, player)
        dsp[player.player_id] = details
        if group_preventing_dsp:
            # The leader is responsible for sending the (combined) audio stream, so get
            # the output format from the leader.
            output_format = player.extra_data.get("output_format", None)
        is_external_group = player.type in (PlayerType.GROUP, PlayerType.STEREO_PAIR)

    # We don't enumerate all group members in case this group is externally created
    # (e.g. a Chromecast group from the Google Home app)
    if player and player.group_members and not is_external_group:
        # grouped playback, get DSP details for each player in the group
        for child_id in player.group_members:
            # skip if we already have the details (so if it's the group leader)
            if child_id in dsp:
                continue
            if child_player := mass.players.get(child_id):
                dsp[child_id] = get_player_dsp_details(
                    mass, child_player, group_preventing_dsp=group_preventing_dsp
                )
                if group_preventing_dsp:
                    # Use the correct format from the group leader, since
                    # this player is part of a group that does not support
                    # multi device DSP processing.
                    dsp[child_id].output_format = output_format
    return dsp


async def get_stream_details(
    mass: MusicAssistant,
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
    streamdetails: StreamDetails | None = None
    time_start = time.time()
    LOGGER.debug("Getting streamdetails for %s", queue_item.uri)
    if seek_position and (queue_item.media_type == MediaType.RADIO or not queue_item.duration):
        LOGGER.warning("seeking is not possible on duration-less streams!")
        seek_position = 0

    if not queue_item.media_item and not queue_item.streamdetails:
        # in case of a non-media item queue item, the streamdetails should already be provided
        # this should not happen, but guard it just in case
        raise MediaNotFoundError(
            f"Unable to retrieve streamdetails for {queue_item.name} ({queue_item.uri})"
        )
    buffer: AudioBuffer | None = None
    if queue_item.streamdetails and (
        (queue_item.streamdetails.created_at + queue_item.streamdetails.expiration) > time.time()
        or ((buffer := queue_item.streamdetails.buffer) and buffer.is_valid(seek_position))
    ):
        # already got a fresh/unused (or unexpired) streamdetails
        streamdetails = queue_item.streamdetails
    else:
        # need to (re)create streamdetails
        # retrieve streamdetails from provider

        media_item = queue_item.media_item
        assert media_item is not None  # for type checking
        preferred_providers: list[str] = []
        if (
            (queue := mass.player_queues.get(queue_item.queue_id))
            and queue.userid
            and (playback_user := await mass.webserver.auth.get_user(queue.userid))
            and playback_user.provider_filter
        ):
            # handle steering into user preferred providerinstance
            preferred_providers = playback_user.provider_filter
        else:
            preferred_providers = [x.provider_instance for x in media_item.provider_mappings]
        for allow_other_provider in (False, True):
            # sort by quality and check item's availability
            for prov_media in sorted(
                media_item.provider_mappings, key=lambda x: x.quality or 0, reverse=True
            ):
                if not prov_media.available:
                    LOGGER.debug(f"Skipping unavailable {prov_media}")
                    continue
                if (
                    not allow_other_provider
                    and prov_media.provider_instance not in preferred_providers
                ):
                    continue
                # guard that provider is available
                music_prov = mass.get_provider(prov_media.provider_instance)
                if TYPE_CHECKING:  # avoid circular import
                    assert isinstance(music_prov, MusicProvider)
                if not music_prov:
                    LOGGER.debug(f"Skipping {prov_media} - provider not available")
                    continue  # provider not available ?
                # get streamdetails from provider
                try:
                    BYPASS_THROTTLER.set(True)
                    streamdetails = await music_prov.get_stream_details(
                        prov_media.item_id, media_item.media_type
                    )
                except MusicAssistantError as err:
                    LOGGER.warning(str(err))
                else:
                    break
                finally:
                    BYPASS_THROTTLER.set(False)

        if not streamdetails:
            msg = f"Unable to retrieve streamdetails for {queue_item.name} ({queue_item.uri})"
            raise MediaNotFoundError(msg)

        # work out how to handle radio stream
        if (
            streamdetails.stream_type in (StreamType.ICY, StreamType.HLS, StreamType.HTTP)
            and streamdetails.media_type == MediaType.RADIO
            and isinstance(streamdetails.path, str)
        ):
            resolved_url, stream_type = await resolve_radio_stream(mass, streamdetails.path)
            streamdetails.path = resolved_url
            streamdetails.stream_type = stream_type
        # handle volume normalization details
        if result := await mass.music.get_loudness(
            streamdetails.item_id,
            streamdetails.provider,
            media_type=queue_item.media_type,
        ):
            streamdetails.loudness = result[0]
            streamdetails.loudness_album = result[1]

    # set queue_id on the streamdetails so we know what is being streamed
    streamdetails.queue_id = queue_item.queue_id
    # handle skip/fade_in details
    streamdetails.seek_position = seek_position
    streamdetails.fade_in = fade_in
    if not streamdetails.duration:
        streamdetails.duration = queue_item.duration
    streamdetails.prefer_album_loudness = prefer_album_loudness
    player_settings = await mass.config.get_player_config(streamdetails.queue_id)
    core_config = await mass.config.get_core_config("streams")
    conf_volume_normalization_target = float(
        str(player_settings.get_value(CONF_VOLUME_NORMALIZATION_TARGET, -17))
    )
    if conf_volume_normalization_target < -30 or conf_volume_normalization_target >= 0:
        conf_volume_normalization_target = -17.0  # reset to default if out of bounds
        LOGGER.warning(
            "Invalid volume normalization target configured for player %s, "
            "resetting to default of -17.0 dB",
            streamdetails.queue_id,
        )
    streamdetails.target_loudness = conf_volume_normalization_target
    streamdetails.volume_normalization_mode = _get_normalization_mode(
        core_config, player_settings, streamdetails
    )

    # attach the DSP details of all group members
    streamdetails.dsp = get_stream_dsp_details(mass, streamdetails.queue_id)

    LOGGER.debug(
        "retrieved streamdetails for %s in %s milliseconds",
        queue_item.uri,
        int((time.time() - time_start) * 1000),
    )
    return streamdetails


async def get_buffered_media_stream(
    mass: MusicAssistant,
    streamdetails: StreamDetails,
    pcm_format: AudioFormat,
    seek_position: int = 0,
    filter_params: list[str] | None = None,
) -> AsyncGenerator[bytes, None]:
    """Get audio stream for given media details as raw PCM with buffering."""
    LOGGER.log(
        VERBOSE_LOG_LEVEL,
        "buffered_media_stream: Starting for %s (seek: %s)",
        streamdetails.uri,
        seek_position,
    )

    # checksum based on filter_params
    checksum = f"{filter_params}"

    async def fill_buffer_task() -> None:
        """Background task to fill the audio buffer."""
        chunk_count = 0
        status = "running"
        try:
            async for chunk in get_media_stream(
                mass, streamdetails, pcm_format, seek_position=0, filter_params=filter_params
            ):
                chunk_count += 1
                await audio_buffer.put(chunk)
                # Yield to event loop to prevent blocking warnings
                await asyncio.sleep(0)
            # Only set EOF if we completed successfully
            await audio_buffer.set_eof()
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        except Exception:
            status = "aborted with error"
            raise
        finally:
            LOGGER.log(
                VERBOSE_LOG_LEVEL,
                "fill_buffer_task: %s (%s chunks) for %s",
                status,
                chunk_count,
                streamdetails.uri,
            )

    # check for existing buffer and reuse if possible
    existing_buffer: AudioBuffer | None = streamdetails.buffer
    if existing_buffer is not None:
        if not existing_buffer.is_valid(checksum, seek_position):
            LOGGER.log(
                VERBOSE_LOG_LEVEL,
                "buffered_media_stream: Existing buffer invalid for %s (seek: %s, discarded: %s)",
                streamdetails.uri,
                seek_position,
                existing_buffer._discarded_chunks,
            )
            await existing_buffer.clear()
            streamdetails.buffer = None
            existing_buffer = None
        else:
            LOGGER.debug(
                "buffered_media_stream: Reusing existing buffer for %s - "
                "available: %ss, seek: %s, discarded: %s",
                streamdetails.uri,
                existing_buffer.seconds_available,
                seek_position,
                existing_buffer._discarded_chunks,
            )
            audio_buffer = existing_buffer

    if not existing_buffer and seek_position > 60:
        # If seeking into the track and no valid buffer exists,
        # just start a normal stream without buffering,
        # otherwise we would need to fill the buffer up to the seek position first
        # which is not efficient.
        LOGGER.debug(
            "buffered_media_stream: No existing buffer and seek >60s for %s, "
            "starting normal (unbuffered) stream",
            streamdetails.uri,
        )
        async for chunk in get_media_stream(
            mass,
            streamdetails,
            pcm_format,
            seek_position=seek_position,
            filter_params=filter_params,
        ):
            yield chunk
        return

    if not existing_buffer:
        # create new audio buffer and start fill task
        LOGGER.debug(
            "buffered_media_stream: Creating new buffer for %s",
            streamdetails.uri,
        )
        audio_buffer = AudioBuffer(pcm_format, checksum)
        streamdetails.buffer = audio_buffer
        task = mass.loop.create_task(fill_buffer_task())
        audio_buffer.attach_producer_task(task)

    # special case: pcm format mismatch, resample on the fly
    # this may happen in some special situations such as crossfading
    # and its a bit of a waste to throw away the existing buffer
    if audio_buffer.pcm_format != pcm_format:
        LOGGER.info(
            "buffered_media_stream: pcm format mismatch, resampling on the fly for %s - "
            "buffer format: %s - requested format: %s",
            streamdetails.uri,
            audio_buffer.pcm_format,
            pcm_format,
        )
        async for chunk in get_ffmpeg_stream(
            audio_input=audio_buffer.iter(seek_position=seek_position),
            input_format=audio_buffer.pcm_format,
            output_format=pcm_format,
        ):
            yield chunk
        return

    # yield data from the buffer
    chunk_count = 0
    try:
        async for chunk in audio_buffer.iter(seek_position=seek_position):
            chunk_count += 1
            yield chunk
    finally:
        LOGGER.log(
            VERBOSE_LOG_LEVEL,
            "buffered_media_stream: Completed, yielded %s chunks",
            chunk_count,
        )


async def get_media_stream(
    mass: MusicAssistant,
    streamdetails: StreamDetails,
    pcm_format: AudioFormat,
    seek_position: int = 0,
    filter_params: list[str] | None = None,
) -> AsyncGenerator[bytes, None]:
    """Get audio stream for given media details as raw PCM."""
    logger = LOGGER.getChild("media_stream")
    logger.log(VERBOSE_LOG_LEVEL, "Starting media stream for %s", streamdetails.uri)
    extra_input_args = streamdetails.extra_input_args or []

    # work out audio source for these streamdetails
    audio_source: str | AsyncGenerator[bytes, None]
    stream_type = streamdetails.stream_type
    if stream_type == StreamType.CUSTOM:
        music_prov = mass.get_provider(streamdetails.provider)
        if TYPE_CHECKING:  # avoid circular import
            assert isinstance(music_prov, MusicProvider)
        audio_source = music_prov.get_audio_stream(
            streamdetails,
            seek_position=seek_position if streamdetails.can_seek else 0,
        )
        seek_position = 0 if streamdetails.can_seek else seek_position
    elif stream_type == StreamType.ICY:
        assert isinstance(streamdetails.path, str)  # for type checking
        audio_source = get_icy_radio_stream(mass, streamdetails.path, streamdetails)
        seek_position = 0  # seeking not possible on radio streams
    elif stream_type == StreamType.HLS:
        assert isinstance(streamdetails.path, str)  # for type checking
        substream = await get_hls_substream(mass, streamdetails.path)
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
            audio_source = get_multi_file_stream(mass, streamdetails, seek_position)
            seek_position = 0  # handled by get_multi_file_stream
        else:
            # regular single file/url stream
            assert isinstance(streamdetails.path, str)  # for type checking
            audio_source = streamdetails.path

    # handle seek support
    if seek_position and streamdetails.duration and streamdetails.allow_seek:
        extra_input_args += ["-ss", str(int(seek_position))]

    bytes_sent = 0
    finished = False
    cancelled = False
    first_chunk_received = False
    ffmpeg_proc = FFMpeg(
        audio_input=audio_source,
        input_format=streamdetails.audio_format,
        output_format=pcm_format,
        filter_params=filter_params,
        extra_input_args=extra_input_args,
        collect_log_history=True,
        loglevel="debug" if LOGGER.isEnabledFor(VERBOSE_LOG_LEVEL) else "info",
    )

    try:
        await ffmpeg_proc.start()
        assert ffmpeg_proc.proc is not None  # for type checking
        logger.debug(
            "Started media stream for %s - using streamtype: %s - pcm format: %s - ffmpeg PID: %s",
            streamdetails.uri,
            streamdetails.stream_type,
            pcm_format.content_type.value,
            ffmpeg_proc.proc.pid,
        )
        stream_start = mass.loop.time()

        chunk_size = get_chunksize(pcm_format, 1)
        async for chunk in ffmpeg_proc.iter_chunked(chunk_size):
            if not first_chunk_received:
                # At this point ffmpeg has started and should now know the codec used
                # for encoding the audio.
                first_chunk_received = True
                streamdetails.audio_format.codec_type = ffmpeg_proc.input_format.codec_type
                logger.debug(
                    "First chunk received after %.2f seconds (codec detected: %s)",
                    mass.loop.time() - stream_start,
                    ffmpeg_proc.input_format.codec_type,
                )
            yield chunk
            bytes_sent += len(chunk)

        # end of audio/track reached
        logger.log(VERBOSE_LOG_LEVEL, "End of stream reached.")
        # wait until stderr also completed reading
        await ffmpeg_proc.wait_with_timeout(5)
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
        # store accurate duration
        if finished and not seek_position and seconds_received:
            streamdetails.duration = int(seconds_received)

        logger.log(
            VERBOSE_LOG_LEVEL,
            "stream %s (with code %s) for %s",
            "cancelled" if cancelled else "finished" if finished else "aborted",
            ffmpeg_proc.returncode,
            streamdetails.uri,
        )

        # parse loudnorm data if we have that collected (and enabled)
        if (
            (streamdetails.loudness is None or finished)
            and streamdetails.volume_normalization_mode == VolumeNormalizationMode.DYNAMIC
            and (finished or (seconds_received >= 300))
        ):
            # if dynamic volume normalization is enabled
            # the loudnorm filter will output the measurement in the log,
            # so we can use that directly instead of analyzing the audio
            logger.log(VERBOSE_LOG_LEVEL, "Collecting loudness measurement...")
            if loudness_details := parse_loudnorm(" ".join(ffmpeg_proc.log_history)):
                logger.debug(
                    "Loudness measurement for %s: %s dB",
                    streamdetails.uri,
                    loudness_details,
                )
                mass.create_task(
                    mass.music.set_loudness(
                        streamdetails.item_id,
                        streamdetails.provider,
                        loudness_details,
                        media_type=streamdetails.media_type,
                    )
                )


def create_wave_header(
    samplerate: int = 44100, channels: int = 2, bitspersample: int = 16, duration: int | None = None
) -> bytes:
    """Generate a wave header from given params."""
    file = BytesIO()

    # Generate format chunk
    format_chunk_spec = b"<4sLHHLLHH"
    format_chunk = struct.pack(
        format_chunk_spec,
        b"fmt ",  # Chunk id
        16,  # Size of this chunk (excluding chunk id and this field)
        1,  # Audio format, 1 for PCM
        channels,  # Number of channels
        int(samplerate),  # Samplerate, 44100, 48000, etc.
        int(samplerate * channels * (bitspersample / 8)),  # Byterate
        int(channels * (bitspersample / 8)),  # Blockalign
        bitspersample,  # 16 bits for two byte samples, etc.
    )
    # Generate data chunk
    # duration = 3600*6.7
    data_chunk_spec = b"<4sL"
    if duration is None:
        # use max value possible
        datasize = 4254768000  # = 6,7 hours at 44100/16
    else:
        # calculate from duration
        numsamples = samplerate * duration
        datasize = int(numsamples * channels * (bitspersample / 8))
    data_chunk = struct.pack(
        data_chunk_spec,
        b"data",  # Chunk id
        int(datasize),  # Chunk size (excluding chunk id and this field)
    )
    sum_items = [
        # "WAVE" string following size field
        4,
        # "fmt " + chunk size field + chunk size
        struct.calcsize(format_chunk_spec),
        # Size of data chunk spec + data size
        struct.calcsize(data_chunk_spec) + datasize,
    ]
    # Generate main header
    all_chunks_size = int(sum(sum_items))
    main_header_spec = b"<4sL4s"
    main_header = struct.pack(main_header_spec, b"RIFF", all_chunks_size, b"WAVE")
    # Write all the contents in
    file.write(main_header)
    file.write(format_chunk)
    file.write(data_chunk)

    # return file.getvalue(), all_chunks_size + 8
    return file.getvalue()


async def resolve_radio_stream(mass: MusicAssistant, url: str) -> tuple[str, StreamType]:
    """
    Resolve a streaming radio URL.

    Unwraps any playlists if needed.
    Determines if the stream supports ICY metadata.

    Returns tuple;
    - unfolded URL as string
    - StreamType to determine ICY (radio) or HLS stream.
    """
    if cache := await mass.cache.get(
        key=url, provider=CACHE_PROVIDER, category=CACHE_CATEGORY_RESOLVED_RADIO_URL
    ):
        if TYPE_CHECKING:  # for type checking
            cache = cast("tuple[str, str]", cache)
        return (cache[0], StreamType(cache[1]))
    stream_type = StreamType.HTTP
    resolved_url = url
    timeout = ClientTimeout(total=None, connect=10, sock_read=5)
    try:
        async with mass.http_session_no_ssl.get(
            url, headers=HTTP_HEADERS_ICY, allow_redirects=True, timeout=timeout
        ) as resp:
            headers = resp.headers
            resp.raise_for_status()
            if not resp.headers:
                raise InvalidDataError("no headers found")
        if headers.get("icy-metaint") is not None:
            stream_type = StreamType.ICY
        if (
            url.endswith((".m3u", ".m3u8", ".pls"))
            or ".m3u?" in url
            or ".m3u8?" in url
            or ".pls?" in url
            or "audio/x-mpegurl" in headers.get("content-type", "")
            or "audio/x-scpls" in headers.get("content-type", "")
        ):
            # url is playlist, we need to unfold it
            try:
                substreams = await fetch_playlist(mass, url)
                if not any(x for x in substreams if x.length):
                    for line in substreams:
                        if not line.is_url:
                            continue
                        # unfold first url of playlist
                        return await resolve_radio_stream(mass, line.path)
                    raise InvalidDataError("No content found in playlist")
            except IsHLSPlaylist:
                stream_type = StreamType.HLS

    except Exception as err:
        LOGGER.warning("Error while parsing radio URL %s: %s", url, str(err))
        return (url, stream_type)

    result = (resolved_url, stream_type)
    cache_expiration = 3600 * 3
    await mass.cache.set(
        url,
        result,
        expiration=cache_expiration,
        provider=CACHE_PROVIDER,
        category=CACHE_CATEGORY_RESOLVED_RADIO_URL,
    )
    return result


async def get_icy_radio_stream(
    mass: MusicAssistant, url: str, streamdetails: StreamDetails
) -> AsyncGenerator[bytes, None]:
    """Get (radio) audio stream from HTTP, including ICY metadata retrieval."""
    timeout = ClientTimeout(total=None, connect=30, sock_read=5 * 60)
    LOGGER.debug("Start streaming radio with ICY metadata from url %s", url)
    async with mass.http_session_no_ssl.get(
        url, allow_redirects=True, headers=HTTP_HEADERS_ICY, timeout=timeout
    ) as resp:
        headers = resp.headers
        meta_int = int(headers["icy-metaint"])
        while True:
            try:
                yield await resp.content.readexactly(meta_int)
                meta_byte = await resp.content.readexactly(1)
                if meta_byte == b"\x00":
                    continue
                meta_length = ord(meta_byte) * 16
                meta_data = await resp.content.readexactly(meta_length)
            except asyncio.exceptions.IncompleteReadError:
                break
            if not meta_data:
                continue
            meta_data = meta_data.rstrip(b"\0")
            stream_title_re = re.search(rb"StreamTitle='([^']*)';", meta_data)
            if not stream_title_re:
                continue
            try:
                # in 99% of the cases the stream title is utf-8 encoded
                stream_title = stream_title_re.group(1).decode("utf-8")
            except UnicodeDecodeError:
                # fallback to iso-8859-1
                stream_title = stream_title_re.group(1).decode("iso-8859-1", errors="replace")
            cleaned_stream_title = clean_stream_title(stream_title)
            if cleaned_stream_title != streamdetails.stream_title:
                LOGGER.log(
                    VERBOSE_LOG_LEVEL,
                    "ICY Radio streamtitle original: %s",
                    stream_title,
                )
                LOGGER.log(
                    VERBOSE_LOG_LEVEL,
                    "ICY Radio streamtitle cleaned: %s",
                    cleaned_stream_title,
                )
                streamdetails.stream_title = cleaned_stream_title


async def get_hls_substream(
    mass: MusicAssistant,
    url: str,
) -> PlaylistItem:
    """Select the (highest quality) HLS substream for given HLS playlist/URL."""
    timeout = ClientTimeout(total=None, connect=30, sock_read=5 * 60)
    # fetch master playlist and select (best) child playlist
    # https://datatracker.ietf.org/doc/html/draft-pantos-http-live-streaming-19#section-10
    async with mass.http_session_no_ssl.get(
        url, allow_redirects=True, headers=HTTP_HEADERS, timeout=timeout
    ) as resp:
        resp.raise_for_status()
        raw_data = await resp.read()
        encoding = resp.charset or await detect_charset(raw_data)
        master_m3u_data = raw_data.decode(encoding)
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


async def get_http_stream(
    mass: MusicAssistant,
    url: str,
    streamdetails: StreamDetails,
    seek_position: int = 0,
    verify_ssl: bool = True,
) -> AsyncGenerator[bytes, None]:
    """Get audio stream from HTTP."""
    LOGGER.debug("Start HTTP stream for %s (seek_position %s)", streamdetails.uri, seek_position)
    if seek_position:
        assert streamdetails.duration, "Duration required for seek requests"
    http_session = mass.http_session if verify_ssl else mass.http_session_no_ssl
    # try to get filesize with a head request
    seek_supported = streamdetails.can_seek
    if seek_position or not streamdetails.size:
        async with http_session.head(url, allow_redirects=True, headers=HTTP_HEADERS) as resp:
            resp.raise_for_status()
            if size := resp.headers.get("Content-Length"):
                streamdetails.size = int(size)
            seek_supported = resp.headers.get("Accept-Ranges") == "bytes"
    # headers
    headers = {**HTTP_HEADERS}
    timeout = ClientTimeout(total=None, connect=30, sock_read=5 * 60)
    skip_bytes = 0
    if seek_position and streamdetails.size:
        assert streamdetails.duration is not None  # for type checking
        skip_bytes = int(streamdetails.size / streamdetails.duration * seek_position)
        headers["Range"] = f"bytes={skip_bytes}-{streamdetails.size}"

    # seeking an unknown or container format is not supported due to the (moov) headers
    if seek_position and (
        not seek_supported
        or streamdetails.audio_format.content_type
        in (
            ContentType.UNKNOWN,
            ContentType.M4A,
            ContentType.M4B,
        )
    ):
        LOGGER.warning(
            "Seeking in %s (%s) not possible.",
            streamdetails.uri,
            streamdetails.audio_format.output_format_str,
        )
        seek_position = 0
        streamdetails.seek_position = 0

    # start the streaming from http
    bytes_received = 0
    async with http_session.get(
        url, allow_redirects=True, headers=headers, timeout=timeout
    ) as resp:
        is_partial = resp.status == 206
        if seek_position and not is_partial:
            raise InvalidDataError("HTTP source does not support seeking!")
        resp.raise_for_status()
        async for chunk in resp.content.iter_any():
            bytes_received += len(chunk)
            yield chunk

    # store size on streamdetails for later use
    if not streamdetails.size:
        streamdetails.size = bytes_received
    LOGGER.debug(
        "Finished HTTP stream for %s (transferred %s/%s bytes)",
        streamdetails.uri,
        bytes_received,
        streamdetails.size,
    )


async def get_file_stream(
    mass: MusicAssistant,  # noqa: ARG001
    filename: str,
    streamdetails: StreamDetails,
    seek_position: int = 0,
) -> AsyncGenerator[bytes, None]:
    """Get audio stream from local accessible file."""
    if seek_position:
        assert streamdetails.duration, "Duration required for seek requests"
    if not streamdetails.size:
        stat = await asyncio.to_thread(os.stat, filename)
        streamdetails.size = stat.st_size

    # seeking an unknown or container format is not supported due to the (moov) headers
    if seek_position and (
        streamdetails.audio_format.content_type
        in (
            ContentType.UNKNOWN,
            ContentType.M4A,
            ContentType.M4B,
            ContentType.MP4,
        )
    ):
        LOGGER.warning(
            "Seeking in %s (%s) not possible.",
            streamdetails.uri,
            streamdetails.audio_format.output_format_str,
        )
        seek_position = 0
        streamdetails.seek_position = 0

    chunk_size = get_chunksize(streamdetails.audio_format)
    async with aiofiles.open(streamdetails.data, "rb") as _file:
        if seek_position:
            assert streamdetails.duration is not None  # for type checking
            seek_pos = int((streamdetails.size / streamdetails.duration) * seek_position)
            await _file.seek(seek_pos)
        # yield chunks of data from file
        while True:
            data = await _file.read(chunk_size)
            if not data:
                break
            yield data


def _get_parts_from_position(
    parts: list[MultiPartPath], seek_position: int
) -> tuple[list[MultiPartPath], int]:
    """Get the remaining parts list from a timestamp.

    Arguments:
    parts: The list of  parts
    seek_position: The seeking position in seconds of the tracklist

    Returns:
        In a tuple, A list of  parts, starting with the one at the requested
        seek position and the position in seconds to seek to in the first
        track.
    """
    skipped_duration = 0.0
    for i, part in enumerate(parts):
        if not isinstance(part, MultiPartPath):
            raise InvalidDataError("Multi-file streamdetails requires a list of MultiPartPath")
        if part.duration is None:
            return parts, seek_position
        if skipped_duration + part.duration < seek_position:
            skipped_duration += part.duration
            continue

        position = seek_position - skipped_duration

        # Seeking in some parts is inaccurate, making the seek to a chapter land on the end of
        # the previous track. If we're within 2 second of the end, skip the current track
        if position + 2 >= part.duration:
            LOGGER.debug(
                f"Skipping to the next part due to seek position being at the end: {position}"
            )
            if i + 1 < len(parts):
                return parts[i + 1 :], 0
            else:
                return parts[i:], int(position)  # last part, cannot skip

        return parts[i:], int(position)

    raise IndexError(f"Could not find any candidate part for position {seek_position}")


async def get_multi_file_stream(
    mass: MusicAssistant,  # noqa: ARG001
    streamdetails: StreamDetails,
    seek_position: int = 0,
) -> AsyncGenerator[bytes, None]:
    """Return audio stream for a concatenation of multiple files.

    Arguments:
    seek_position: The position to seek to in seconds
    """
    if not isinstance(streamdetails.path, list):
        raise InvalidDataError("Multi-file streamdetails requires a list of MultiPartPath")
    parts, seek_position = _get_parts_from_position(streamdetails.path, seek_position)
    files_list = [part.path for part in parts]

    # concat input files
    temp_file = f"/tmp/{shortuuid.random(20)}.txt"  # noqa: S108
    async with aiofiles.open(temp_file, "w") as f:
        for path in files_list:
            await f.write(f"file '{path}'\n")

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


async def get_preview_stream(
    mass: MusicAssistant,
    provider_instance_id_or_domain: str,
    item_id: str,
    media_type: MediaType = MediaType.TRACK,
) -> AsyncGenerator[bytes, None]:
    """Create a 30 seconds preview audioclip for the given streamdetails."""
    if not (music_prov := mass.get_provider(provider_instance_id_or_domain)):
        raise ProviderUnavailableError
    if TYPE_CHECKING:  # avoid circular import
        assert isinstance(music_prov, MusicProvider)
    streamdetails = await music_prov.get_stream_details(item_id, media_type)
    pcm_format = AudioFormat(
        content_type=ContentType.from_bit_depth(streamdetails.audio_format.bit_depth),
        sample_rate=streamdetails.audio_format.sample_rate,
        bit_depth=streamdetails.audio_format.bit_depth,
        channels=streamdetails.audio_format.channels,
    )
    async for chunk in get_ffmpeg_stream(
        audio_input=get_media_stream(
            mass=mass,
            streamdetails=streamdetails,
            pcm_format=pcm_format,
        ),
        input_format=pcm_format,
        output_format=AudioFormat(content_type=ContentType.AAC),
        extra_input_args=["-t", "30"],  # cut after 30 seconds
    ):
        yield chunk


async def get_silence(
    duration: int,
    output_format: AudioFormat,
) -> AsyncGenerator[bytes, None]:
    """Create stream of silence, encoded to format of choice."""
    if output_format.content_type.is_pcm():
        # pcm = just zeros
        for _ in range(duration):
            yield b"\0" * int(output_format.sample_rate * (output_format.bit_depth / 8) * 2)
        return
    if output_format.content_type == ContentType.WAV:
        # wav silence = wave header + zero's
        yield create_wave_header(
            samplerate=output_format.sample_rate,
            channels=2,
            bitspersample=output_format.bit_depth,
            duration=duration,
        )
        for _ in range(duration):
            yield b"\0" * int(output_format.sample_rate * (output_format.bit_depth / 8) * 2)
        return
    # use ffmpeg for all other encodings
    args = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "quiet",
        "-f",
        "lavfi",
        "-i",
        f"anullsrc=r={output_format.sample_rate}:cl={'stereo'}",
        "-t",
        str(duration),
        "-f",
        output_format.output_format_str,
        "-",
    ]
    async with AsyncProcess(args, stdout=True) as ffmpeg_proc:
        async for chunk in ffmpeg_proc.iter_chunked():
            yield chunk


async def resample_pcm_audio(
    input_audio: bytes,
    input_format: AudioFormat,
    output_format: AudioFormat,
) -> bytes:
    """
    Resample (a chunk of) PCM audio from input_format to output_format using ffmpeg.

    :param input_audio: Raw PCM audio data to resample.
    :param input_format: AudioFormat of the input audio.
    :param output_format: Desired AudioFormat for the output audio.

    :return: Resampled audio data, frame-aligned. Returns empty bytes if resampling fails.
    """
    if input_format == output_format:
        return input_audio
    LOGGER.log(VERBOSE_LOG_LEVEL, f"Resampling audio from {input_format} to {output_format}")
    try:
        ffmpeg_args = get_ffmpeg_args(
            input_format=input_format, output_format=output_format, filter_params=[]
        )
        _, stdout, stderr = await communicate(ffmpeg_args, input_audio)
        if not stdout:
            LOGGER.error(
                "Resampling failed: no output from ffmpeg. Input: %s, Output: %s, stderr: %s",
                input_format,
                output_format,
                stderr.decode() if stderr else "(no stderr)",
            )
            return b""
        # Ensure frame alignment after resampling
        return align_audio_to_frame_boundary(stdout, output_format)
    except Exception as err:
        LOGGER.exception(
            "Failed to resample audio from %s to %s: %s",
            input_format,
            output_format,
            err,
        )
        return b""


def get_chunksize(
    fmt: AudioFormat,
    seconds: float = 1,
) -> int:
    """Get a default chunk/file size for given contenttype in bytes."""
    pcm_size = int(fmt.sample_rate * (fmt.bit_depth / 8) * fmt.channels * seconds)
    if fmt.content_type.is_pcm() or fmt.content_type == ContentType.WAV:
        return pcm_size
    if fmt.content_type in (ContentType.WAV, ContentType.AIFF, ContentType.DSF):
        return pcm_size
    if fmt.bit_rate and fmt.bit_rate < 10000:
        return int(((fmt.bit_rate * 1000) / 8) * seconds)
    if fmt.content_type in (ContentType.FLAC, ContentType.WAVPACK, ContentType.ALAC):
        # assume 74.7% compression ratio (level 0)
        # source: https://z-issue.com/wp/flac-compression-level-comparison/
        return int(pcm_size * 0.747)
    if fmt.content_type in (ContentType.MP3, ContentType.OGG):
        return int((320000 / 8) * seconds)
    if fmt.content_type in (ContentType.AAC, ContentType.M4A):
        return int((256000 / 8) * seconds)
    return int((320000 / 8) * seconds)


def is_grouping_preventing_dsp(player: Player) -> bool:
    """Check if grouping is preventing DSP from being applied to this leader/PlayerGroup.

    If this returns True, no DSP should be applied to the player.
    This function will not check if the Player is in a group, the caller should do that first.
    """
    # We require the caller to handle non-leader cases themselves since player.synced_to
    # can be unreliable in some edge cases
    multi_device_dsp_supported = PlayerFeature.MULTI_DEVICE_DSP in player.supported_features
    child_count = len(player.group_members) if player.group_members else 0

    is_multiple_devices: bool
    if player.provider.domain == "player_group":
        # PlayerGroups have no leader, so having a child count of 1 means
        # the group actually contains only a single player.
        is_multiple_devices = child_count > 1
    elif player.type == PlayerType.GROUP:
        # This is an group player external to Music Assistant.
        is_multiple_devices = True
    else:
        is_multiple_devices = child_count > 0
    return is_multiple_devices and not multi_device_dsp_supported


def is_output_limiter_enabled(mass: MusicAssistant, player: Player) -> bool:
    """Check if the player has the output limiter enabled.

    Unlike DSP, the limiter is still configurable when synchronized without MULTI_DEVICE_DSP.
    So in grouped scenarios without MULTI_DEVICE_DSP, the permanent sync group or the leader gets
    decides if the limiter should be turned on or not.
    """
    deciding_player_id = player.player_id
    if player.active_group:
        # Syncgroup, get from the group player
        deciding_player_id = player.active_group
    elif player.synced_to:
        # Not in sync group, but synced, get from the leader
        deciding_player_id = player.synced_to
    output_limiter_enabled = mass.config.get_raw_player_config_value(
        deciding_player_id,
        CONF_ENTRY_OUTPUT_LIMITER.key,
        CONF_ENTRY_OUTPUT_LIMITER.default_value,
    )
    return bool(output_limiter_enabled)


def get_player_filter_params(
    mass: MusicAssistant,
    player_id: str,
    input_format: AudioFormat,
    output_format: AudioFormat,
) -> list[str]:
    """Get player specific filter parameters for ffmpeg (if any)."""
    filter_params = []

    dsp = mass.config.get_player_dsp_config(player_id)
    limiter_enabled = True

    if player := mass.players.get(player_id):
        if is_grouping_preventing_dsp(player):
            # We can not correctly apply DSP to a grouped player without multi-device DSP support,
            # so we disable it.
            dsp.enabled = False
        elif player.provider.domain == "player_group" and (
            PlayerFeature.MULTI_DEVICE_DSP not in player.supported_features
        ):
            # This is a special case! We have a player group where:
            # - The group leader does not support MULTI_DEVICE_DSP
            # - But only contains a single player (since nothing is preventing DSP)
            # We can still apply the DSP of that single player.
            if player.group_members:
                child_player = mass.players.get(player.group_members[0])
                assert child_player is not None  # for type checking
                dsp = mass.config.get_player_dsp_config(child_player.player_id)
            else:
                # This should normally never happen, but if it does, we disable DSP.
                dsp.enabled = False

        # We here implicitly know what output format is used for the player
        # in the audio processing steps. We save this information to
        # later be able to show this to the user in the UI.
        player.extra_data["output_format"] = output_format

        limiter_enabled = is_output_limiter_enabled(mass, player)

    if dsp.enabled:
        # Apply input gain
        if dsp.input_gain != 0:
            filter_params.append(f"volume={dsp.input_gain}dB")

        # Process each DSP filter sequentially
        for f in dsp.filters:
            if not f.enabled:
                continue

            # Apply filter
            filter_params.extend(filter_to_ffmpeg_params(f, input_format))

        # Apply output gain
        if dsp.output_gain != 0:
            filter_params.append(f"volume={dsp.output_gain}dB")

    conf_channels = mass.config.get_raw_player_config_value(
        player_id, CONF_OUTPUT_CHANNELS, "stereo"
    )

    # handle output mixing only left or right
    if conf_channels == "left":
        filter_params.append("pan=mono|c0=FL")
    elif conf_channels == "right":
        filter_params.append("pan=mono|c0=FR")

    # Add safety limiter at the end
    if limiter_enabled:
        filter_params.append("alimiter=limit=-2dB:level=false:asc=true")

    LOGGER.debug("Generated ffmpeg params for player %s: %s", player_id, filter_params)
    return filter_params


def parse_loudnorm(raw_stderr: bytes | str) -> float | None:
    """Parse Loudness measurement from ffmpeg stderr output."""
    stderr_data = raw_stderr.decode() if isinstance(raw_stderr, bytes) else raw_stderr
    if "[Parsed_loudnorm_0 @" not in stderr_data:
        return None
    for jsun_chunk in stderr_data.split(" { "):
        try:
            stderr_data = "{" + jsun_chunk.rsplit("}")[0].strip() + "}"
            loudness_data = json_loads(stderr_data)
            return float(loudness_data["input_i"])
        except (*JSON_DECODE_EXCEPTIONS, KeyError, ValueError, IndexError):
            continue
    return None


async def analyze_loudness(
    mass: MusicAssistant,
    streamdetails: StreamDetails,
) -> None:
    """Analyze media item's audio, to calculate EBU R128 loudness."""
    if await mass.music.get_loudness(
        streamdetails.item_id,
        streamdetails.provider,
        media_type=streamdetails.media_type,
    ):
        # only when needed we do the analyze job
        return

    logger = LOGGER.getChild("analyze_loudness")
    logger.debug("Start analyzing audio for %s", streamdetails.uri)

    extra_input_args = [
        # limit to 10 minutes to reading too much in memory
        "-t",
        "600",
    ]
    # work out audio source for these streamdetails
    stream_type = streamdetails.stream_type
    audio_source: str | AsyncGenerator[bytes, None]
    if stream_type == StreamType.CUSTOM:
        music_prov = mass.get_provider(streamdetails.provider)
        if TYPE_CHECKING:  # avoid circular import
            assert isinstance(music_prov, MusicProvider)
        audio_source = music_prov.get_audio_stream(streamdetails)
    elif stream_type == StreamType.ICY:
        assert isinstance(streamdetails.path, str)  # for type checking
        audio_source = get_icy_radio_stream(mass, streamdetails.path, streamdetails)
    elif stream_type == StreamType.HLS:
        assert isinstance(streamdetails.path, str)  # for type checking
        substream = await get_hls_substream(mass, streamdetails.path)
        audio_source = substream.path
    else:
        # all other stream types (HTTP, FILE, etc)
        if stream_type == StreamType.ENCRYPTED_HTTP:
            assert streamdetails.decryption_key is not None  # for type checking
            extra_input_args += ["-decryption_key", streamdetails.decryption_key]
        if isinstance(streamdetails.path, list):
            # multi part stream - just use a single file for the measurement
            audio_source = streamdetails.path[1].path
        else:
            # regular single file/url stream
            assert isinstance(streamdetails.path, str)  # for type checking
            audio_source = streamdetails.path

    # calculate BS.1770 R128 integrated loudness with ffmpeg
    async with FFMpeg(
        audio_input=audio_source,
        input_format=streamdetails.audio_format,
        output_format=streamdetails.audio_format,
        audio_output="NULL",
        filter_params=["ebur128=framelog=verbose"],
        extra_input_args=extra_input_args,
        collect_log_history=True,
        loglevel="info",
    ) as ffmpeg_proc:
        await ffmpeg_proc.wait()
        log_lines = ffmpeg_proc.log_history
        log_lines_str = "\n".join(log_lines)
        try:
            loudness_str = (
                log_lines_str.split("Integrated loudness")[1].split("I:")[1].split("LUFS")[0]
            )
            loudness = float(loudness_str.strip())
        except (IndexError, ValueError, AttributeError):
            LOGGER.warning(
                "Could not determine integrated loudness of %s - %s",
                streamdetails.uri,
                log_lines_str or "received empty value",
            )
        else:
            await mass.music.set_loudness(
                streamdetails.item_id,
                streamdetails.provider,
                loudness,
                media_type=streamdetails.media_type,
            )
            logger.debug(
                "Integrated loudness of %s is: %s",
                streamdetails.uri,
                loudness,
            )


def _get_normalization_mode(
    core_config: CoreConfig, player_config: PlayerConfig, streamdetails: StreamDetails
) -> VolumeNormalizationMode:
    if not player_config.get_value(CONF_VOLUME_NORMALIZATION):
        # disabled for this player
        return VolumeNormalizationMode.DISABLED
    if streamdetails.target_loudness is None:
        # no target loudness set, disable normalization
        return VolumeNormalizationMode.DISABLED
    # work out preference for track or radio
    preference = VolumeNormalizationMode(
        str(
            core_config.get_value(
                CONF_VOLUME_NORMALIZATION_RADIO
                if streamdetails.media_type == MediaType.RADIO
                else CONF_VOLUME_NORMALIZATION_TRACKS,
            )
        )
    )

    # handle no measurement available but fallback to dynamic mode is allowed
    if streamdetails.loudness is None and preference == VolumeNormalizationMode.FALLBACK_DYNAMIC:
        return VolumeNormalizationMode.DYNAMIC

    # handle no measurement available and no fallback allowed
    if streamdetails.loudness is None and preference == VolumeNormalizationMode.MEASUREMENT_ONLY:
        return VolumeNormalizationMode.DISABLED

    # handle no measurement available and fallback to fixed gain is allowed
    if streamdetails.loudness is None and preference == VolumeNormalizationMode.FALLBACK_FIXED_GAIN:
        return VolumeNormalizationMode.FIXED_GAIN

    # handle measurement available - chosen mode is measurement
    if streamdetails.loudness is not None and preference not in (
        VolumeNormalizationMode.DISABLED,
        VolumeNormalizationMode.FIXED_GAIN,
        VolumeNormalizationMode.DYNAMIC,
    ):
        return VolumeNormalizationMode.MEASUREMENT_ONLY

    # simply return the preference
    return preference
