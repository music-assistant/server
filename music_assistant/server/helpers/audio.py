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
from typing import TYPE_CHECKING

import aiofiles
from aiohttp import ClientTimeout

from music_assistant.common.helpers.global_cache import set_global_cache_values
from music_assistant.common.helpers.json import JSON_DECODE_EXCEPTIONS, json_loads
from music_assistant.common.helpers.util import clean_stream_title
from music_assistant.common.models.config_entries import CoreConfig, PlayerConfig
from music_assistant.common.models.enums import MediaType, StreamType, VolumeNormalizationMode
from music_assistant.common.models.errors import (
    InvalidDataError,
    MediaNotFoundError,
    MusicAssistantError,
    ProviderUnavailableError,
)
from music_assistant.common.models.media_items import AudioFormat, ContentType
from music_assistant.common.models.streamdetails import StreamDetails
from music_assistant.constants import (
    CONF_EQ_BASS,
    CONF_EQ_MID,
    CONF_EQ_TREBLE,
    CONF_OUTPUT_CHANNELS,
    CONF_VOLUME_NORMALIZATION,
    CONF_VOLUME_NORMALIZATION_RADIO,
    CONF_VOLUME_NORMALIZATION_TARGET,
    CONF_VOLUME_NORMALIZATION_TRACKS,
    MASS_LOGGER_NAME,
    VERBOSE_LOG_LEVEL,
)

from .ffmpeg import FFMpeg, get_ffmpeg_stream
from .playlists import IsHLSPlaylist, PlaylistItem, fetch_playlist, parse_m3u
from .process import AsyncProcess, check_output, communicate
from .throttle_retry import BYPASS_THROTTLER
from .util import TimedAsyncGenerator, create_tempfile, detect_charset

if TYPE_CHECKING:
    from music_assistant.common.models.player_queue import QueueItem
    from music_assistant.server import MusicAssistant

LOGGER = logging.getLogger(f"{MASS_LOGGER_NAME}.audio")

# ruff: noqa: PLR0915

HTTP_HEADERS = {"User-Agent": "Lavf/60.16.100.MusicAssistant"}
HTTP_HEADERS_ICY = {**HTTP_HEADERS, "Icy-MetaData": "1"}


async def crossfade_pcm_parts(
    fade_in_part: bytes,
    fade_out_part: bytes,
    pcm_format: AudioFormat,
) -> bytes:
    """Crossfade two chunks of pcm/raw audio using ffmpeg."""
    sample_size = pcm_format.pcm_sample_size
    # calculate the fade_length from the smallest chunk
    fade_length = min(len(fade_in_part), len(fade_out_part)) / sample_size
    fadeoutfile = create_tempfile()
    async with aiofiles.open(fadeoutfile.name, "wb") as outfile:
        await outfile.write(fade_out_part)
    args = [
        # generic args
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "quiet",
        # fadeout part (as file)
        "-acodec",
        pcm_format.content_type.name.lower(),
        "-f",
        pcm_format.content_type.value,
        "-ac",
        str(pcm_format.channels),
        "-ar",
        str(pcm_format.sample_rate),
        "-i",
        fadeoutfile.name,
        # fade_in part (stdin)
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
        # filter args
        "-filter_complex",
        f"[0][1]acrossfade=d={fade_length}",
        # output args
        "-f",
        pcm_format.content_type.value,
        "-",
    ]
    _returncode, crossfaded_audio, _stderr = await communicate(args, fade_in_part)
    if crossfaded_audio:
        LOGGER.log(
            VERBOSE_LOG_LEVEL,
            "crossfaded 2 pcm chunks. fade_in_part: %s - "
            "fade_out_part: %s - fade_length: %s seconds",
            len(fade_in_part),
            len(fade_out_part),
            fade_length,
        )
        return crossfaded_audio
    # no crossfade_data, return original data instead
    LOGGER.debug(
        "crossfade of pcm chunks failed: not enough data? " "fade_in_part: %s - fade_out_part: %s",
        len(fade_in_part),
        len(fade_out_part),
    )
    return fade_out_part + fade_in_part


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


async def get_stream_details(
    mass: MusicAssistant,
    queue_item: QueueItem,
    seek_position: int = 0,
    fade_in: bool = False,
    prefer_album_loudness: bool = False,
) -> StreamDetails:
    """Get streamdetails for the given QueueItem.

    This is called just-in-time when a PlayerQueue wants a MediaItem to be played.
    Do not try to request streamdetails in advance as this is expiring data.
        param media_item: The QueueItem for which to request the streamdetails for.
    """
    time_start = time.time()
    LOGGER.debug("Getting streamdetails for %s", queue_item.uri)
    if seek_position and (queue_item.media_type == MediaType.RADIO or not queue_item.duration):
        LOGGER.warning("seeking is not possible on duration-less streams!")
        seek_position = 0
    # we use a contextvar to bypass the throttler for this asyncio task/context
    # this makes sure that playback has priority over other requests that may be
    # happening in the background
    BYPASS_THROTTLER.set(True)
    # always request the full item as there might be other qualities available
    full_item = await mass.music.get_item_by_uri(queue_item.uri)
    # sort by quality and check track availability
    for prov_media in sorted(
        full_item.provider_mappings, key=lambda x: x.quality or 0, reverse=True
    ):
        if not prov_media.available:
            LOGGER.debug(f"Skipping unavailable {prov_media}")
            continue
        # guard that provider is available
        music_prov = mass.get_provider(prov_media.provider_instance)
        if not music_prov:
            LOGGER.debug(f"Skipping {prov_media} - provider not available")
            continue  # provider not available ?
        # get streamdetails from provider
        try:
            streamdetails: StreamDetails = await music_prov.get_stream_details(prov_media.item_id)
        except MusicAssistantError as err:
            LOGGER.warning(str(err))
        else:
            break
    else:
        raise MediaNotFoundError(
            f"Unable to retrieve streamdetails for {queue_item.name} ({queue_item.uri})"
        )

    # work out how to handle radio stream
    if (
        streamdetails.stream_type in (StreamType.ICY, StreamType.HLS, StreamType.HTTP)
        and streamdetails.media_type == MediaType.RADIO
    ):
        resolved_url, stream_type = await resolve_radio_stream(mass, streamdetails.path)
        streamdetails.path = resolved_url
        streamdetails.stream_type = stream_type
    # set queue_id on the streamdetails so we know what is being streamed
    streamdetails.queue_id = queue_item.queue_id
    # handle skip/fade_in details
    streamdetails.seek_position = seek_position
    streamdetails.fade_in = fade_in
    if not streamdetails.duration:
        streamdetails.duration = queue_item.duration

    # handle volume normalization details
    if result := await mass.music.get_loudness(
        streamdetails.item_id,
        streamdetails.provider,
        media_type=queue_item.media_type,
    ):
        streamdetails.loudness, streamdetails.loudness_album = result
    streamdetails.prefer_album_loudness = prefer_album_loudness
    player_settings = await mass.config.get_player_config(streamdetails.queue_id)
    core_config = await mass.config.get_core_config("streams")
    streamdetails.volume_normalization_mode = _get_normalization_mode(
        core_config, player_settings, streamdetails
    )
    streamdetails.target_loudness = player_settings.get_value(CONF_VOLUME_NORMALIZATION_TARGET)

    process_time = int((time.time() - time_start) * 1000)
    LOGGER.debug("retrieved streamdetails for %s in %s milliseconds", queue_item.uri, process_time)
    return streamdetails


async def get_media_stream(
    mass: MusicAssistant,
    streamdetails: StreamDetails,
    pcm_format: AudioFormat,
    audio_source: AsyncGenerator[bytes, None] | str,
    filter_params: list[str] | None = None,
    extra_input_args: list[str] | None = None,
) -> AsyncGenerator[bytes, None]:
    """Get PCM audio stream for given media details."""
    logger = LOGGER.getChild("media_stream")
    logger.debug("start media stream for: %s", streamdetails.uri)
    strip_silence_begin = streamdetails.strip_silence_begin
    strip_silence_end = streamdetails.strip_silence_end
    if streamdetails.fade_in:
        filter_params.append("afade=type=in:start_time=0:duration=3")
        strip_silence_begin = False
    bytes_sent = 0
    chunk_number = 0
    buffer: bytes = b""
    finished = False

    ffmpeg_proc = FFMpeg(
        audio_input=audio_source,
        input_format=streamdetails.audio_format,
        output_format=pcm_format,
        filter_params=filter_params,
        extra_input_args=extra_input_args,
        collect_log_history=True,
    )
    try:
        await ffmpeg_proc.start()
        async for chunk in TimedAsyncGenerator(
            ffmpeg_proc.iter_chunked(pcm_format.pcm_sample_size), 300
        ):
            # for radio streams we just yield all chunks directly
            if streamdetails.media_type == MediaType.RADIO:
                yield chunk
                bytes_sent += len(chunk)
                continue

            chunk_number += 1
            # determine buffer size dynamically
            if chunk_number < 5 and strip_silence_begin:
                req_buffer_size = int(pcm_format.pcm_sample_size * 4)
            elif chunk_number > 30 and strip_silence_end:
                req_buffer_size = int(pcm_format.pcm_sample_size * 8)
            else:
                req_buffer_size = int(pcm_format.pcm_sample_size * 2)

            # always append to buffer
            buffer += chunk
            del chunk

            if len(buffer) < req_buffer_size:
                # buffer is not full enough, move on
                continue

            if chunk_number == 5 and strip_silence_begin:
                # strip silence from begin of audio
                chunk = await strip_silence(  # noqa: PLW2901
                    mass, buffer, pcm_format=pcm_format
                )
                bytes_sent += len(chunk)
                yield chunk
                buffer = b""
                continue

            #### OTHER: enough data in buffer, feed to output
            while len(buffer) > req_buffer_size:
                yield buffer[: pcm_format.pcm_sample_size]
                bytes_sent += pcm_format.pcm_sample_size
                buffer = buffer[pcm_format.pcm_sample_size :]

        # end of audio/track reached
        if strip_silence_end and buffer:
            # strip silence from end of audio
            buffer = await strip_silence(
                mass,
                buffer,
                pcm_format=pcm_format,
                reverse=True,
            )
        # send remaining bytes in buffer
        bytes_sent += len(buffer)
        yield buffer
        del buffer
        finished = True

    finally:
        await ffmpeg_proc.close()

        if bytes_sent == 0:
            # edge case: no audio data was sent
            streamdetails.stream_error = True
            seconds_streamed = 0
            logger.warning("Stream error on %s", streamdetails.uri)
        else:
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
        if (
            streamdetails.loudness is None
            and streamdetails.volume_normalization_mode != VolumeNormalizationMode.DISABLED
            and (finished or (seconds_streamed >= 300))
        ):
            # if dynamic volume normalization is enabled and the entire track is streamed
            # the loudnorm filter will output the measuremeet in the log,
            # so we can use those directly instead of analyzing the audio
            if loudness_details := parse_loudnorm(" ".join(ffmpeg_proc.log_history)):
                logger.debug(
                    "Loudness measurement for %s: %s dB",
                    streamdetails.uri,
                    loudness_details,
                )
                streamdetails.loudness = loudness_details
                mass.create_task(
                    mass.music.set_loudness(
                        streamdetails.item_id,
                        streamdetails.provider,
                        loudness_details,
                        media_type=streamdetails.media_type,
                    )
                )
            else:
                # no data from loudnorm filter found, we need to analyze the audio
                # add background task to start analyzing the audio
                task_id = f"analyze_loudness_{streamdetails.uri}"
                mass.create_task(analyze_loudness, mass, streamdetails, task_id=task_id)

        # mark item as played in db if finished or streamed for 30 seconds
        # NOTE that this is not the actual played time but the buffered time
        # the queue controller will update the actual played time when the item is played
        if finished or seconds_streamed > 30:
            mass.create_task(
                mass.music.mark_item_played(
                    streamdetails.media_type,
                    streamdetails.item_id,
                    streamdetails.provider,
                )
            )


def create_wave_header(samplerate=44100, channels=2, bitspersample=16, duration=None):
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
    cache_base_key = "resolved_radio_info"
    if cache := await mass.cache.get(url, base_key=cache_base_key):
        return cache
    stream_type = StreamType.HTTP
    resolved_url = url
    timeout = ClientTimeout(total=0, connect=10, sock_read=5)
    try:
        async with mass.http_session.get(
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
            or "audio/x-mpegurl" in headers.get("content-type")
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
        LOGGER.warning("Error while parsing radio URL %s: %s", url, err)
        return (url, stream_type)

    result = (resolved_url, stream_type)
    cache_expiration = 3600 * 3
    await mass.cache.set(url, result, expiration=cache_expiration, base_key=cache_base_key)
    return result


async def get_icy_radio_stream(
    mass: MusicAssistant, url: str, streamdetails: StreamDetails
) -> AsyncGenerator[bytes, None]:
    """Get (radio) audio stream from HTTP, including ICY metadata retrieval."""
    timeout = ClientTimeout(total=0, connect=30, sock_read=5 * 60)
    LOGGER.debug("Start streaming radio with ICY metadata from url %s", url)
    async with mass.http_session.get(
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
            stream_title = re.search(rb"StreamTitle='([^']*)';", meta_data)
            if not stream_title:
                continue
            try:
                # in 99% of the cases the stream title is utf-8 encoded
                stream_title = stream_title.group(1).decode("utf-8")
            except UnicodeDecodeError:
                # fallback to iso-8859-1
                stream_title = stream_title.group(1).decode("iso-8859-1", errors="replace")
            cleaned_stream_title = clean_stream_title(stream_title)
            if cleaned_stream_title != streamdetails.stream_title:
                LOGGER.log(VERBOSE_LOG_LEVEL, "ICY Radio streamtitle original: %s", stream_title)
                LOGGER.log(
                    VERBOSE_LOG_LEVEL, "ICY Radio streamtitle cleaned: %s", cleaned_stream_title
                )
                streamdetails.stream_title = cleaned_stream_title


async def get_hls_substream(
    mass: MusicAssistant,
    url: str,
) -> PlaylistItem:
    """Select the (highest quality) HLS substream for given HLS playlist/URL."""
    timeout = ClientTimeout(total=0, connect=30, sock_read=5 * 60)
    # fetch master playlist and select (best) child playlist
    # https://datatracker.ietf.org/doc/html/draft-pantos-http-live-streaming-19#section-10
    async with mass.http_session.get(
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
        substreams.sort(key=lambda x: int(x.stream_info.get("BANDWIDTH", "0")), reverse=True)
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
) -> AsyncGenerator[bytes, None]:
    """Get audio stream from HTTP."""
    LOGGER.debug("Start HTTP stream for %s (seek_position %s)", streamdetails.uri, seek_position)
    if seek_position:
        assert streamdetails.duration, "Duration required for seek requests"
    # try to get filesize with a head request
    seek_supported = streamdetails.can_seek
    if seek_position or not streamdetails.size:
        async with mass.http_session.head(url, allow_redirects=True, headers=HTTP_HEADERS) as resp:
            resp.raise_for_status()
            if size := resp.headers.get("Content-Length"):
                streamdetails.size = int(size)
            seek_supported = resp.headers.get("Accept-Ranges") == "bytes"
    # headers
    headers = {**HTTP_HEADERS}
    timeout = ClientTimeout(total=0, connect=30, sock_read=5 * 60)
    skip_bytes = 0
    if seek_position and streamdetails.size:
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
    async with mass.http_session.get(
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
            seek_pos = int((streamdetails.size / streamdetails.duration) * seek_position)
            await _file.seek(seek_pos)
        # yield chunks of data from file
        while True:
            data = await _file.read(chunk_size)
            if not data:
                break
            yield data


async def check_audio_support() -> tuple[bool, bool, str]:
    """Check if ffmpeg is present (with/without libsoxr support)."""
    # check for FFmpeg presence
    returncode, output = await check_output("ffmpeg", "-version")
    ffmpeg_present = returncode == 0 and "FFmpeg" in output.decode()

    # use globals as in-memory cache
    version = output.decode().split("ffmpeg version ")[1].split(" ")[0].split("-")[0]
    libsoxr_support = "enable-libsoxr" in output.decode()
    result = (ffmpeg_present, libsoxr_support, version)
    # store in global cache for easy access by 'get_ffmpeg_args'
    await set_global_cache_values({"ffmpeg_support": result})
    return result


async def get_preview_stream(
    mass: MusicAssistant,
    provider_instance_id_or_domain: str,
    track_id: str,
) -> AsyncGenerator[bytes, None]:
    """Create a 30 seconds preview audioclip for the given streamdetails."""
    if not (music_prov := mass.get_provider(provider_instance_id_or_domain)):
        raise ProviderUnavailableError
    streamdetails = await music_prov.get_stream_details(track_id)
    async for chunk in get_ffmpeg_stream(
        audio_input=music_prov.get_audio_stream(streamdetails, 30)
        if streamdetails.stream_type == StreamType.CUSTOM
        else streamdetails.path,
        input_format=streamdetails.audio_format,
        output_format=AudioFormat(content_type=ContentType.AAC),
        extra_input_args=["-to", "30"],
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


def get_chunksize(
    fmt: AudioFormat,
    seconds: int = 1,
) -> int:
    """Get a default chunk/file size for given contenttype in bytes."""
    pcm_size = int(fmt.sample_rate * (fmt.bit_depth / 8) * fmt.channels * seconds)
    if fmt.content_type.is_pcm() or fmt.content_type == ContentType.WAV:
        return pcm_size
    if fmt.content_type in (ContentType.WAV, ContentType.AIFF, ContentType.DSF):
        return pcm_size
    if fmt.bit_rate:
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


def get_player_filter_params(
    mass: MusicAssistant,
    player_id: str,
) -> list[str]:
    """Get player specific filter parameters for ffmpeg (if any)."""
    # collect all players-specific filter args
    # TODO: add convolution/DSP/roomcorrections here?!
    filter_params = []

    # the below is a very basic 3-band equalizer,
    # this could be a lot more sophisticated at some point
    if (eq_bass := mass.config.get_raw_player_config_value(player_id, CONF_EQ_BASS, 0)) != 0:
        filter_params.append(f"equalizer=frequency=100:width=200:width_type=h:gain={eq_bass}")
    if (eq_mid := mass.config.get_raw_player_config_value(player_id, CONF_EQ_MID, 0)) != 0:
        filter_params.append(f"equalizer=frequency=900:width=1800:width_type=h:gain={eq_mid}")
    if (eq_treble := mass.config.get_raw_player_config_value(player_id, CONF_EQ_TREBLE, 0)) != 0:
        filter_params.append(f"equalizer=frequency=9000:width=18000:width_type=h:gain={eq_treble}")
    # handle output mixing only left or right
    conf_channels = mass.config.get_raw_player_config_value(
        player_id, CONF_OUTPUT_CHANNELS, "stereo"
    )
    if conf_channels == "left":
        filter_params.append("pan=mono|c0=FL")
    elif conf_channels == "right":
        filter_params.append("pan=mono|c0=FR")

    # add a peak limiter at the end of the filter chain
    filter_params.append("alimiter=limit=-2dB:level=false:asc=true")

    return filter_params


def parse_loudnorm(raw_stderr: bytes | str) -> float | None:
    """Parse Loudness measurement from ffmpeg stderr output."""
    stderr_data = raw_stderr.decode() if isinstance(raw_stderr, bytes) else raw_stderr
    if "[Parsed_loudnorm_" not in stderr_data:
        return None
    stderr_data = stderr_data.split("[Parsed_loudnorm_")[1]
    stderr_data = "{" + stderr_data.rsplit("{")[-1].strip()
    stderr_data = stderr_data.rsplit("}")[0].strip() + "}"
    try:
        loudness_data = json_loads(stderr_data)
    except JSON_DECODE_EXCEPTIONS:
        return None
    return float(loudness_data["input_i"])


async def analyze_loudness(
    mass: MusicAssistant,
    streamdetails: StreamDetails,
) -> None:
    """Analyze media item's audio, to calculate EBU R128 loudness."""
    if result := await mass.music.get_loudness(
        streamdetails.item_id,
        streamdetails.provider,
        media_type=streamdetails.media_type,
    ):
        # only when needed we do the analyze job
        streamdetails.loudness = result
        return

    logger = LOGGER.getChild("analyze_loudness")
    logger.debug("Start analyzing audio for %s", streamdetails.uri)

    extra_input_args = [
        # limit to 10 minutes to reading too much in memory
        "-t",
        "600",
    ]
    if streamdetails.stream_type == StreamType.CUSTOM:
        audio_source = mass.get_provider(streamdetails.provider).get_audio_stream(
            streamdetails,
        )
    elif streamdetails.stream_type == StreamType.HLS:
        substream = await get_hls_substream(mass, streamdetails.path)
        audio_source = substream.path
    elif streamdetails.stream_type == StreamType.ENCRYPTED_HTTP:
        audio_source = streamdetails.path
        extra_input_args += ["-decryption_key", streamdetails.decryption_key]
    else:
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
            streamdetails.loudness = loudness
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
    # work out preference for track or radio
    preference = VolumeNormalizationMode(
        core_config.get_value(
            CONF_VOLUME_NORMALIZATION_RADIO
            if streamdetails.media_type == MediaType.RADIO
            else CONF_VOLUME_NORMALIZATION_TRACKS,
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

    # simply return the preference
    return preference
