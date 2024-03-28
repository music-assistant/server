"""Various helpers for audio manipulation."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import struct
from collections import deque
from io import BytesIO
from time import time
from typing import TYPE_CHECKING

import aiofiles
from aiohttp import ClientResponseError, ClientTimeout

from music_assistant.common.helpers.global_cache import (
    get_global_cache_value,
    set_global_cache_values,
)
from music_assistant.common.helpers.json import JSON_DECODE_EXCEPTIONS, json_loads
from music_assistant.common.models.errors import (
    AudioError,
    InvalidDataError,
    MediaNotFoundError,
    MusicAssistantError,
)
from music_assistant.common.models.media_items import AudioFormat, ContentType
from music_assistant.common.models.streamdetails import LoudnessMeasurement, StreamDetails
from music_assistant.constants import (
    CONF_EQ_BASS,
    CONF_EQ_MID,
    CONF_EQ_TREBLE,
    CONF_OUTPUT_CHANNELS,
    CONF_VOLUME_NORMALIZATION,
    CONF_VOLUME_NORMALIZATION_TARGET,
    MASS_LOGGER_NAME,
    VERBOSE_LOG_LEVEL,
)
from music_assistant.server.helpers.playlists import (
    HLS_CONTENT_TYPES,
    IsHLSPlaylist,
    fetch_playlist,
    parse_m3u,
)
from music_assistant.server.helpers.tags import parse_tags

from .process import AsyncProcess, check_output
from .util import create_tempfile

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from music_assistant.common.models.player_queue import QueueItem
    from music_assistant.server import MusicAssistant

LOGGER = logging.getLogger(f"{MASS_LOGGER_NAME}.audio")
# pylint:disable=consider-using-f-string,too-many-locals,too-many-statements
# ruff: noqa: PLR0915

VLC_HEADERS = {"User-Agent": "VLC/3.0.2.LibVLC/3.0.2"}
VLC_HEADERS_ICY = {**VLC_HEADERS, "Icy-MetaData": "1"}


async def crossfade_pcm_parts(
    fade_in_part: bytes,
    fade_out_part: bytes,
    bit_depth: int,
    sample_rate: int,
) -> bytes:
    """Crossfade two chunks of pcm/raw audio using ffmpeg."""
    sample_size = int(sample_rate * (bit_depth / 8) * 2)
    fmt = ContentType.from_bit_depth(bit_depth)
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
        fmt.name.lower(),
        "-f",
        fmt,
        "-ac",
        "2",
        "-ar",
        str(sample_rate),
        "-i",
        fadeoutfile.name,
        # fade_in part (stdin)
        "-acodec",
        fmt.name.lower(),
        "-f",
        fmt,
        "-ac",
        "2",
        "-ar",
        str(sample_rate),
        "-i",
        "-",
        # filter args
        "-filter_complex",
        f"[0][1]acrossfade=d={fade_length}",
        # output args
        "-f",
        fmt,
        "-",
    ]
    async with AsyncProcess(args, True) as proc:
        crossfade_data, _ = await proc.communicate(fade_in_part)
        if crossfade_data:
            LOGGER.log(
                5,
                "crossfaded 2 pcm chunks. fade_in_part: %s - "
                "fade_out_part: %s - fade_length: %s seconds",
                len(fade_in_part),
                len(fade_out_part),
                fade_length,
            )
            return crossfade_data
        # no crossfade_data, return original data instead
        LOGGER.debug(
            "crossfade of pcm chunks failed: not enough data? "
            "fade_in_part: %s - fade_out_part: %s",
            len(fade_in_part),
            len(fade_out_part),
        )
        return fade_out_part + fade_in_part


async def strip_silence(
    mass: MusicAssistant,  # noqa: ARG001
    audio_data: bytes,
    sample_rate: int,
    bit_depth: int,
    reverse: bool = False,
) -> bytes:
    """Strip silence from begin or end of pcm audio using ffmpeg."""
    fmt = ContentType.from_bit_depth(bit_depth)
    args = ["ffmpeg", "-hide_banner", "-loglevel", "quiet"]
    args += [
        "-acodec",
        fmt.name.lower(),
        "-f",
        fmt,
        "-ac",
        "2",
        "-ar",
        str(sample_rate),
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
    args += ["-f", fmt, "-"]
    async with AsyncProcess(args, True) as proc:
        stripped_data, _ = await proc.communicate(audio_data)

    # return stripped audio
    bytes_stripped = len(audio_data) - len(stripped_data)
    if LOGGER.isEnabledFor(5):
        pcm_sample_size = int(sample_rate * (bit_depth / 8) * 2)
        seconds_stripped = round(bytes_stripped / pcm_sample_size, 2)
        location = "end" if reverse else "begin"
        LOGGER.log(
            5,
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
) -> StreamDetails:
    """Get streamdetails for the given QueueItem.

    This is called just-in-time when a PlayerQueue wants a MediaItem to be played.
    Do not try to request streamdetails in advance as this is expiring data.
        param media_item: The QueueItem for which to request the streamdetails for.
    """
    if queue_item.streamdetails and (time() + 60) < queue_item.streamdetails.expires:
        LOGGER.debug(f"Using (pre)cached streamdetails from queue_item for {queue_item.uri}")
        # we already have (fresh) streamdetails stored on the queueitem, use these.
        # this happens for example while seeking in a track.
        # we create a copy (using to/from dict) to ensure the one-time values are cleared
        streamdetails = StreamDetails.from_dict(queue_item.streamdetails.to_dict())
    else:
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
            # prefer cache
            item_key = f"{music_prov.lookup_key}/{prov_media.item_id}"
            cache_key = f"cached_streamdetails_{item_key}"
            if cache := await mass.cache.get(cache_key):
                if time() + 60 < cache["expires"]:
                    LOGGER.debug(f"Using cached streamdetails for {item_key}")
                    streamdetails = StreamDetails.from_dict(cache)
            # get streamdetails from provider
            try:
                streamdetails: StreamDetails = await music_prov.get_stream_details(
                    prov_media.item_id
                )
            except MusicAssistantError as err:
                LOGGER.warning(str(err))
            else:
                # store streamdetails in cache
                expiration = streamdetails.expires - time()
                await mass.cache.set(cache_key, streamdetails.to_dict(), expiration=expiration - 60)
                break
        else:
            raise MediaNotFoundError(f"Unable to retrieve streamdetails for {queue_item}")

    # set queue_id on the streamdetails so we know what is being streamed
    streamdetails.queue_id = queue_item.queue_id
    # handle skip/fade_in details
    streamdetails.seek_position = seek_position
    streamdetails.fade_in = fade_in
    # handle volume normalization details
    if not streamdetails.loudness:
        streamdetails.loudness = await mass.music.get_track_loudness(
            streamdetails.item_id, streamdetails.provider
        )
    if streamdetails.target_loudness is not None:
        streamdetails.target_loudness = streamdetails.target_loudness
    elif (
        player_settings := await mass.config.get_player_config(streamdetails.queue_id)
    ) and player_settings.get_value(CONF_VOLUME_NORMALIZATION):
        streamdetails.target_loudness = player_settings.get_value(CONF_VOLUME_NORMALIZATION_TARGET)
    else:
        streamdetails.target_loudness = None

    if not streamdetails.duration:
        streamdetails.duration = queue_item.duration
    return streamdetails


def create_wave_header(samplerate=44100, channels=2, bitspersample=16, duration=None):
    """Generate a wave header from given params."""
    # pylint: disable=no-member
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


async def resolve_radio_stream(
    mass: MusicAssistant, url: str, use_get: bool = False
) -> tuple[str, bool, bool]:
    """
    Resolve a streaming radio URL.

    Unwraps any playlists if needed.
    Determines if the stream supports ICY metadata.

    Returns tuple;
    - unfolded URL as string
    - bool if the URL supports ICY metadata.
    - bool uf the URL represents a HLS stream/playlist.
    """
    base_url = url.split("?")[0]
    cache_key = f"RADIO_RESOLVED_{url}"
    if cache := await mass.cache.get(cache_key):
        return cache
    is_hls = False
    supports_icy = False
    resolved_url = url
    timeout = ClientTimeout(total=0, connect=10, sock_read=5)
    try:
        method = "GET" if use_get else "HEAD"
        async with mass.http_session.request(
            method, url, headers=VLC_HEADERS_ICY, allow_redirects=True, timeout=timeout
        ) as resp:
            resolved_url = str(resp.real_url)
            headers = resp.headers
            resp.raise_for_status()
            if not resp.headers:
                raise InvalidDataError("no headers found")
        supports_icy = headers.get("icy-name") is not None or "Icecast" in headers.get("server", "")
        is_hls = headers.get("content-type") in HLS_CONTENT_TYPES
        if (
            base_url.endswith((".m3u", ".m3u8", ".pls"))
            or headers.get("content-type") == "audio/x-mpegurl"
        ):
            # url is playlist, we need to unfold it
            try:
                for line in await fetch_playlist(mass, resolved_url):
                    if not line.is_url:
                        continue
                    # unfold first url of playlist
                    return await resolve_radio_stream(mass, line.path)
                raise InvalidDataError("No content found in playlist")
            except IsHLSPlaylist:
                is_hls = True

    except (ClientResponseError, InvalidDataError) as err:
        if not use_get:
            return await resolve_radio_stream(mass, resolved_url, True)
        LOGGER.warning("Error while parsing radio URL %s: %s", url, err)
        return (resolved_url, supports_icy, is_hls)

    result = (resolved_url, supports_icy, is_hls)
    cache_expiration = 24 * 3600 if url == resolved_url else 600
    await mass.cache.set(cache_key, result, expiration=cache_expiration)
    return result


async def get_radio_stream(
    mass: MusicAssistant, url: str, streamdetails: StreamDetails
) -> AsyncGenerator[bytes, None]:
    """Get radio audio stream from HTTP, including metadata retrieval."""
    resolved_url, supports_icy, is_hls = await resolve_radio_stream(mass, url)
    # handle special HLS stream
    if is_hls:
        async for chunk in get_hls_stream(mass, resolved_url, streamdetails):
            yield chunk
        return
    # handle http stream supports icy metadata
    if supports_icy:
        async for chunk in get_icy_stream(mass, resolved_url, streamdetails):
            yield chunk
        return
    # generic http stream (without icy metadata)
    async for chunk in get_http_stream(mass, resolved_url, streamdetails):
        yield chunk


async def get_icy_stream(
    mass: MusicAssistant, url: str, streamdetails: StreamDetails
) -> AsyncGenerator[bytes, None]:
    """Get (radio) audio stream from HTTP, including ICY metadata retrieval."""
    timeout = ClientTimeout(total=0, connect=30, sock_read=5 * 60)
    LOGGER.debug("Start streaming radio with ICY metadata from url %s", url)
    async with mass.http_session.get(url, headers=VLC_HEADERS_ICY, timeout=timeout) as resp:
        headers = resp.headers
        meta_int = int(headers["icy-metaint"])
        while True:
            try:
                audio_chunk = await resp.content.readexactly(meta_int)
                yield audio_chunk
                meta_byte = await resp.content.readexactly(1)
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
            stream_title = stream_title.group(1).decode()
            if stream_title != streamdetails.stream_title:
                streamdetails.stream_title = stream_title


async def get_hls_stream(
    mass: MusicAssistant, url: str, streamdetails: StreamDetails
) -> AsyncGenerator[bytes, None]:
    """Get audio stream from HTTP HLS stream."""
    logger = LOGGER.getChild("hls_stream")
    timeout = ClientTimeout(total=0, connect=30, sock_read=5 * 60)
    # fetch master playlist and select (best) child playlist
    # https://datatracker.ietf.org/doc/html/draft-pantos-http-live-streaming-19#section-10
    async with mass.http_session.get(url, headers=VLC_HEADERS, timeout=timeout) as resp:
        charset = resp.charset or "utf-8"
        master_m3u_data = await resp.text(charset)
    substreams = parse_m3u(master_m3u_data)
    if any(x for x in substreams if x.path.endswith(".ts")) or not all(
        x for x in substreams if x.stream_info is not None
    ):
        # the url we got is already a substream
        substream_url = url
    else:
        # sort substreams on best quality (highest bandwidth)
        substreams.sort(key=lambda x: int(x.stream_info.get("BANDWIDTH", "0")), reverse=True)
        substream = substreams[0]
        substream_url = substream.path
        if not substream_url.startswith("http"):
            # path is relative, stitch it together
            base_path = url.rsplit("/", 1)[0]
            substream_url = base_path + "/" + substream.path

    logger.debug(
        "Start streaming HLS stream for url %s (selected substream %s)", url, substream_url
    )

    if streamdetails.audio_format.content_type == ContentType.UNKNOWN:
        streamdetails.audio_format = AudioFormat(content_type=ContentType.AAC)

    prev_chunks: deque[str] = deque(maxlen=30)
    has_playlist_metadata: bool | None = None
    has_id3_metadata: bool | None = None
    while True:
        async with mass.http_session.get(
            substream_url, headers=VLC_HEADERS, timeout=timeout
        ) as resp:
            charset = resp.charset or "utf-8"
            substream_m3u_data = await resp.text(charset)
        # get chunk-parts from the substream
        hls_chunks = parse_m3u(substream_m3u_data)
        for chunk_item in hls_chunks:
            if chunk_item.path in prev_chunks:
                continue
            chunk_item_url = chunk_item.path
            if not chunk_item_url.startswith("http"):
                # path is relative, stitch it together
                base_path = substream_url.rsplit("/", 1)[0]
                chunk_item_url = base_path + "/" + chunk_item.path
            # handle (optional) in-playlist (timed) metadata
            if has_playlist_metadata is None:
                has_playlist_metadata = chunk_item.title is not None
                logger.debug("Station support for in-playlist metadata: %s", has_playlist_metadata)
            if has_playlist_metadata and chunk_item.title != "no desc":
                # bbc (and maybe others?) set the title to 'no desc'
                streamdetails.stream_title = chunk_item.title
            logger.log(VERBOSE_LOG_LEVEL, "playing chunk %s", chunk_item)
            # prevent that we play this chunk again if we loop through
            prev_chunks.append(chunk_item.path)
            async with mass.http_session.get(
                chunk_item_url, headers=VLC_HEADERS, timeout=timeout
            ) as resp:
                async for chunk in resp.content.iter_any():
                    yield chunk
            # handle (optional) in-band (m3u) metadata
            if has_id3_metadata is not None and has_playlist_metadata:
                continue
            if has_id3_metadata in (None, True):
                tags = await parse_tags(chunk_item_url)
                has_id3_metadata = tags.title and tags.title not in chunk_item.path
                logger.debug("Station support for in-band (ID3) metadata: %s", has_id3_metadata)


async def get_hls_stream_org(
    mass: MusicAssistant, url: str, streamdetails: StreamDetails
) -> AsyncGenerator[bytes, None]:
    """Get audio stream from HTTP HLS stream."""
    timeout = ClientTimeout(total=0, connect=30, sock_read=5 * 60)
    # fetch master playlist and select (best) child playlist
    # https://datatracker.ietf.org/doc/html/draft-pantos-http-live-streaming-19#section-10
    async with mass.http_session.get(url, headers=VLC_HEADERS, timeout=timeout) as resp:
        charset = resp.charset or "utf-8"
        master_m3u_data = await resp.text(charset)
    substreams = parse_m3u(master_m3u_data)
    if any(x for x in substreams if x.path.endswith(".ts")) or not all(
        x for x in substreams if x.stream_info is not None
    ):
        # the url we got is already a substream
        substream_url = url
    else:
        # sort substreams on best quality (highest bandwidth)
        substreams.sort(key=lambda x: int(x.stream_info.get("BANDWIDTH", "0")), reverse=True)
        substream = substreams[0]
        substream_url = substream.path
        if not substream_url.startswith("http"):
            # path is relative, stitch it together
            base_path = url.rsplit("/", 1)[0]
            substream_url = base_path + "/" + substream.path

    async def watch_metadata():
        # ffmpeg is not (yet?) able to handle metadata updates that is provided
        # in the substream playlist and/or the ID3 metadata
        # so we do that here in a separate task.
        # this also gets the basic
        prev_chunk = ""
        while True:
            async with mass.http_session.get(
                substream_url, headers=VLC_HEADERS, timeout=timeout
            ) as resp:
                charset = resp.charset or "utf-8"
                substream_m3u_data = await resp.text(charset)
            # get chunk-parts from the substream
            hls_chunks = parse_m3u(substream_m3u_data)
            metadata_found = False
            for chunk_item in hls_chunks:
                if chunk_item.path == prev_chunk:
                    continue
                chunk_item_url = chunk_item.path
                if not chunk_item_url.startswith("http"):
                    # path is relative, stitch it together
                    base_path = substream_url.rsplit("/", 1)[0]
                    chunk_item_url = base_path + "/" + chunk_item.path
                if chunk_item.title and chunk_item.title != "no desc":
                    streamdetails.stream_title = chunk_item.title
                    metadata_found = True
                # prevent that we play this chunk again if we loop through
                prev_chunk = chunk_item.path
                if chunk_item.length and chunk_item.length.isnumeric():
                    await asyncio.sleep(int(chunk_item.length))
                else:
                    await asyncio.sleep(5)
            if not metadata_found:
                # this station does not provide metadata embedded in the HLS playlist
                return

    LOGGER.debug(
        "Start streaming HLS stream for url %s (selected substream %s)", url, substream_url
    )

    if streamdetails.audio_format.content_type == ContentType.UNKNOWN:
        streamdetails.audio_format = AudioFormat(content_type=ContentType.AAC)

    try:
        metadata_task = asyncio.create_task(watch_metadata())
        async for chunk in get_ffmpeg_stream(
            audio_input=substream_url,
            input_format=streamdetails.audio_format,
            # we need a self-explaining codec but not loose data from re-encoding
            output_format=AudioFormat(content_type=ContentType.FLAC),
        ):
            yield chunk
    finally:
        if metadata_task and not metadata_task.done():
            metadata_task.cancel()


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
    if seek_position and not streamdetails.size:
        async with mass.http_session.head(url) as resp:
            if size := resp.headers.get("Content-Length"):
                streamdetails.size = int(size)
    # headers
    headers = {}
    skip_bytes = 0
    if seek_position and streamdetails.size:
        skip_bytes = int(streamdetails.size / streamdetails.duration * seek_position)
        headers["Range"] = f"bytes={skip_bytes}-"

    # start the streaming from http
    buffer = b""
    buffer_all = False
    bytes_received = 0
    timeout = ClientTimeout(total=0, connect=30, sock_read=5 * 60)
    async with mass.http_session.get(url, headers=VLC_HEADERS, timeout=timeout) as resp:
        is_partial = resp.status == 206
        buffer_all = seek_position and not is_partial
        async for chunk in resp.content.iter_any():
            bytes_received += len(chunk)
            if buffer_all and not skip_bytes:
                buffer += chunk
                continue
            if not is_partial and skip_bytes and bytes_received < skip_bytes:
                continue
            yield chunk

    # store size on streamdetails for later use
    if not streamdetails.size:
        streamdetails.size = bytes_received
    if buffer_all:
        skip_bytes = streamdetails.size / streamdetails.duration * seek_position
        yield buffer[:skip_bytes]
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
    chunk_size = get_chunksize(streamdetails.audio_format.content_type)
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


async def get_ffmpeg_stream(
    audio_input: AsyncGenerator[bytes, None] | str,
    input_format: AudioFormat,
    output_format: AudioFormat,
    filter_params: list[str] | None = None,
    extra_args: list[str] | None = None,
    chunk_size: int | None = None,
    loglevel: str | None = None,
) -> AsyncGenerator[bytes, None]:
    """
    Get the ffmpeg audio stream as async generator.

    Takes care of resampling and/or recoding if needed,
    according to player preferences.
    """
    if loglevel is None:
        loglevel = "info" if LOGGER.isEnabledFor(VERBOSE_LOG_LEVEL) else "fatal"
    use_stdin = not isinstance(audio_input, str)
    ffmpeg_args = get_ffmpeg_args(
        input_format=input_format,
        output_format=output_format,
        filter_params=filter_params or [],
        extra_args=extra_args or [],
        input_path="-" if use_stdin else audio_input,
        output_path="-",
        loglevel=loglevel,
    )
    async with AsyncProcess(
        ffmpeg_args,
        enable_stdin=use_stdin,
        enable_stdout=True,
        enable_stderr=False,
        custom_stdin=audio_input if use_stdin else None,
        name="ffmpeg_stream",
    ) as ffmpeg_proc:
        # read final chunks from stdout
        chunk_size = chunk_size or get_chunksize(output_format, 1)
        async for chunk in ffmpeg_proc.iter_chunked(chunk_size):
            yield chunk


async def check_audio_support() -> tuple[bool, bool, str]:
    """Check if ffmpeg is present (with/without libsoxr support)."""
    # check for FFmpeg presence
    returncode, output = await check_output("ffmpeg -version")
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
    music_prov = mass.get_provider(provider_instance_id_or_domain)

    streamdetails = await music_prov.get_stream_details(track_id)

    input_args = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "quiet",
        "-ignore_unknown",
    ]
    if streamdetails.direct:
        input_args += ["-ss", "30", "-i", streamdetails.direct]
    else:
        # the input is received from pipe/stdin
        if streamdetails.audio_format.content_type != ContentType.UNKNOWN:
            input_args += ["-f", streamdetails.audio_format.content_type]
        input_args += ["-i", "-"]

    output_args = ["-to", "30", "-f", "mp3", "-"]
    args = input_args + output_args

    writer_task: asyncio.Task | None = None
    ffmpeg_proc = AsyncProcess(args, enable_stdin=True, enable_stdout=True, enable_stderr=False)
    await ffmpeg_proc.start()

    async def writer() -> None:
        """Task that grabs the source audio and feeds it to ffmpeg."""
        music_prov = mass.get_provider(streamdetails.provider)
        async for audio_chunk in music_prov.get_audio_stream(streamdetails, 30):
            await ffmpeg_proc.write(audio_chunk)
        # write eof when last packet is received
        await ffmpeg_proc.write_eof()

    if not streamdetails.direct:
        writer_task = asyncio.create_task(writer())

    # yield chunks from stdout
    try:
        async for chunk in ffmpeg_proc.iter_any():
            yield chunk
    finally:
        if writer_task and not writer_task.done():
            writer_task.cancel()
        await ffmpeg_proc.close()


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
    async with AsyncProcess(args) as ffmpeg_proc:
        async for chunk in ffmpeg_proc.iter_any():
            yield chunk


def get_chunksize(
    fmt: AudioFormat,
    seconds: int = 1,
) -> int:
    """Get a default chunksize for given contenttype."""
    pcm_size = int(fmt.sample_rate * (fmt.bit_depth / 8) * 2 * seconds)
    if fmt.content_type.is_pcm() or fmt.content_type == ContentType.WAV:
        return pcm_size
    if fmt.content_type in (ContentType.WAV, ContentType.AIFF, ContentType.DSF):
        return pcm_size
    if fmt.content_type in (ContentType.FLAC, ContentType.WAVPACK, ContentType.ALAC):
        return int(pcm_size * 0.5)
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

    return filter_params


def get_ffmpeg_args(
    input_format: AudioFormat,
    output_format: AudioFormat,
    filter_params: list[str],
    extra_args: list[str] | None = None,
    input_path: str = "-",
    output_path: str = "-",
    loglevel: str = "info",
    extra_input_args: list[str] | None = None,
) -> list[str]:
    """Collect all args to send to the ffmpeg process."""
    if extra_args is None:
        extra_args = []
    ffmpeg_present, libsoxr_support, version = get_global_cache_value("ffmpeg_support")
    if not ffmpeg_present:
        msg = (
            "FFmpeg binary is missing from system."
            "Please install ffmpeg on your OS to enable playback."
        )
        raise AudioError(
            msg,
        )

    major_version = int("".join(char for char in version.split(".")[0] if not char.isalpha()))

    # generic args
    generic_args = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        loglevel,
        "-ignore_unknown",
        "-protocol_whitelist",
        "file,http,https,tcp,tls,crypto,pipe,data,fd",
    ]
    # collect input args
    input_args = []
    if extra_input_args:
        input_args += extra_input_args
    if input_path.startswith("http"):
        # append reconnect options for direct stream from http
        input_args += [
            "-reconnect",
            "1",
            "-reconnect_streamed",
            "1",
            "-reconnect_delay_max",
            "10",
        ]
        if major_version > 4:
            # these options are only supported in ffmpeg > 5
            input_args += [
                "-reconnect_on_network_error",
                "1",
                "-reconnect_on_http_error",
                "5xx",
            ]
    if input_format.content_type.is_pcm():
        input_args += [
            "-ac",
            str(input_format.channels),
            "-channel_layout",
            "mono" if input_format.channels == 1 else "stereo",
            "-ar",
            str(input_format.sample_rate),
            "-acodec",
            input_format.content_type.name.lower(),
            "-f",
            input_format.content_type.value,
        ]
    input_args += ["-i", input_path]

    # collect output args
    if output_path.upper() == "NULL":
        output_args = ["-f", "null", "-"]
    else:
        output_args = [
            "-acodec",
            output_format.content_type.name.lower(),
            "-f",
            output_format.content_type.value,
            "-ac",
            str(output_format.channels),
            "-ar",
            str(output_format.sample_rate),
            output_path,
        ]

    # prefer libsoxr high quality resampler (if present) for sample rate conversions
    if input_format.sample_rate != output_format.sample_rate and libsoxr_support:
        filter_params.append("aresample=resampler=soxr")

    if filter_params and "-filter_complex" not in extra_args:
        extra_args += ["-af", ",".join(filter_params)]

    return generic_args + input_args + extra_args + output_args


def parse_loudnorm(raw_stderr: bytes | str) -> LoudnessMeasurement | None:
    """Parse Loudness measurement from ffmpeg stderr output."""
    stderr_data = raw_stderr.decode() if isinstance(raw_stderr, bytes) else raw_stderr
    if "[Parsed_loudnorm_" not in stderr_data:
        return None
    stderr_data = stderr_data.split("[Parsed_loudnorm_")[1]
    stderr_data = stderr_data.rsplit("]")[-1].strip()
    stderr_data = stderr_data.rsplit("}")[0].strip() + "}"
    try:
        loudness_data = json_loads(stderr_data)
    except JSON_DECODE_EXCEPTIONS:
        return None
    return LoudnessMeasurement(
        integrated=float(loudness_data["input_i"]),
        true_peak=float(loudness_data["input_tp"]),
        lra=float(loudness_data["input_lra"]),
        threshold=float(loudness_data["input_thresh"]),
    )
