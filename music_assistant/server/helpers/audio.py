"""Various helpers for audio streaming and manipulation."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import struct
import time
from collections import deque
from collections.abc import AsyncGenerator
from contextlib import suppress
from io import BytesIO
from signal import SIGINT
from typing import TYPE_CHECKING

import aiofiles
from aiohttp import ClientTimeout

from music_assistant.common.helpers.global_cache import (
    get_global_cache_value,
    set_global_cache_values,
)
from music_assistant.common.helpers.json import JSON_DECODE_EXCEPTIONS, json_loads
from music_assistant.common.helpers.util import clean_stream_title
from music_assistant.common.models.enums import MediaType, StreamType
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
    PlaylistItem,
    fetch_playlist,
    parse_m3u,
)
from music_assistant.server.helpers.tags import parse_tags

from .process import AsyncProcess, check_output, communicate
from .util import create_tempfile

if TYPE_CHECKING:
    from music_assistant.common.models.player_queue import QueueItem
    from music_assistant.server import MusicAssistant

LOGGER = logging.getLogger(f"{MASS_LOGGER_NAME}.audio")
# pylint:disable=consider-using-f-string,too-many-locals,too-many-statements
# ruff: noqa: PLR0915

HTTP_HEADERS = {"User-Agent": "Lavf/60.16.100.MusicAssistant"}
HTTP_HEADERS_ICY = {**HTTP_HEADERS, "Icy-MetaData": "1"}


class FFMpeg(AsyncProcess):
    """FFMpeg wrapped as AsyncProcess."""

    def __init__(
        self,
        audio_input: AsyncGenerator[bytes, None] | str | int,
        input_format: AudioFormat,
        output_format: AudioFormat,
        filter_params: list[str] | None = None,
        extra_args: list[str] | None = None,
        extra_input_args: list[str] | None = None,
        audio_output: str | int = "-",
        collect_log_history: bool = False,
        logger: logging.Logger | None = None,
    ) -> None:
        """Initialize AsyncProcess."""
        ffmpeg_args = get_ffmpeg_args(
            input_format=input_format,
            output_format=output_format,
            filter_params=filter_params or [],
            extra_args=extra_args or [],
            input_path=audio_input if isinstance(audio_input, str) else "-",
            output_path=audio_output if isinstance(audio_output, str) else "-",
            extra_input_args=extra_input_args or [],
            loglevel="info",
        )
        self.audio_input = audio_input
        self.input_format = input_format
        self.collect_log_history = collect_log_history
        self.log_history: deque[str] = deque(maxlen=100)
        self._stdin_task: asyncio.Task | None = None
        self._logger_task: asyncio.Task | None = None
        super().__init__(
            ffmpeg_args,
            stdin=True if isinstance(audio_input, str | AsyncGenerator) else audio_input,
            stdout=True if isinstance(audio_output, str) else audio_output,
            stderr=True,
        )
        self.logger = logger or LOGGER.getChild("ffmpeg")
        clean_args = []
        for arg in ffmpeg_args[1:]:
            if arg.startswith("http"):
                clean_args.append("<URL>")
            elif "/" in arg and "." in arg:
                clean_args.append("<FILE>")
            else:
                clean_args.append(arg)
        args_str = " ".join(clean_args)
        self.logger.debug("starting ffmpeg with args: %s", args_str)

    async def start(self) -> None:
        """Perform Async init of process."""
        await super().start()
        self._logger_task = asyncio.create_task(self._log_reader_task())
        if isinstance(self.audio_input, AsyncGenerator):
            self._stdin_task = asyncio.create_task(self._feed_stdin())

    async def close(self, send_signal: bool = True) -> None:
        """Close/terminate the process and wait for exit."""
        if self._stdin_task and not self._stdin_task.done():
            self._stdin_task.cancel()
        if not self.collect_log_history:
            await super().close(send_signal)
            return
        # override close logic to make sure we catch all logging
        self._close_called = True
        if send_signal and self.returncode is None:
            self.proc.send_signal(SIGINT)
        if self.proc.stdin and not self.proc.stdin.is_closing():
            self.proc.stdin.close()
            await asyncio.sleep(0)  # yield to loop
        # abort existing readers on stdout first before we send communicate
        waiter: asyncio.Future
        if self.proc.stdout and (waiter := self.proc.stdout._waiter):
            self.proc.stdout._waiter = None
            if waiter and not waiter.done():
                waiter.set_exception(asyncio.CancelledError())
            # read remaining bytes to unblock pipe
            await self.read(-1)
        # wait for log task to complete that reads the remaining data from stderr
        with suppress(TimeoutError):
            await asyncio.wait_for(self._logger_task, 5)
        await super().close(False)

    async def _log_reader_task(self) -> None:
        """Read ffmpeg log from stderr."""
        decode_errors = 0
        async for line in self.iter_stderr():
            if self.collect_log_history:
                self.log_history.append(line)
            if "error" in line or "warning" in line:
                self.logger.debug(line)
            elif "critical" in line:
                self.logger.warning(line)
            else:
                self.logger.log(VERBOSE_LOG_LEVEL, line)

            if "Invalid data found when processing input" in line:
                decode_errors += 1
            if decode_errors >= 50:
                self.logger.error(line)
                await super().close(True)

            # if streamdetails contenttype is unknown, try parse it from the ffmpeg log
            if line.startswith("Stream #") and ": Audio: " in line:
                if self.input_format.content_type == ContentType.UNKNOWN:
                    content_type_raw = line.split(": Audio: ")[1].split(" ")[0]
                    content_type = ContentType.try_parse(content_type_raw)
                    self.logger.info(
                        "Detected (input) content type: %s (%s)", content_type, content_type_raw
                    )
                    self.input_format.content_type = content_type
            del line

    async def _feed_stdin(self) -> None:
        """Feed stdin with audio chunks from an AsyncGenerator."""
        if TYPE_CHECKING:
            self.audio_input: AsyncGenerator[bytes, None]
        try:
            async for chunk in self.audio_input:
                await self.write(chunk)
            # write EOF once we've reached the end of the input stream
            await self.write_eof()
        except Exception as err:
            # make sure we dont swallow any exceptions and we bail out
            # once our audio source fails.
            if isinstance(err, asyncio.CancelledError):
                self.logger.exception(err)
                await self.close(True)


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
    _returncode, crossfaded_audio, _stderr = await communicate(args, fade_in_part)
    if crossfaded_audio:
        LOGGER.log(
            5,
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
    _returncode, stripped_data, _stderr = await communicate(args, audio_data)

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
    if seek_position and (queue_item.media_type == MediaType.RADIO or not queue_item.duration):
        LOGGER.warning("seeking is not possible on duration-less streams!")
        seek_position = 0
    if queue_item.streamdetails and seek_position:
        LOGGER.debug(f"Using (pre)cached streamdetails from queue_item for {queue_item.uri}")
        # we already have (fresh?) streamdetails stored on the queueitem, use these.
        # only do this when we're seeking.
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
            # get streamdetails from provider
            try:
                streamdetails: StreamDetails = await music_prov.get_stream_details(
                    prov_media.item_id
                )
            except MusicAssistantError as err:
                LOGGER.warning(str(err))
            else:
                break
        else:
            raise MediaNotFoundError(f"Unable to retrieve streamdetails for {queue_item}")

        # work out how to handle radio stream
        if (
            streamdetails.media_type == MediaType.RADIO
            and streamdetails.stream_type == StreamType.HTTP
        ):
            resolved_url, is_icy, is_hls = await resolve_radio_stream(mass, streamdetails.path)
            streamdetails.path = resolved_url
            if is_hls:
                streamdetails.stream_type = StreamType.HLS
            elif is_icy:
                streamdetails.stream_type = StreamType.ICY
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
    player_settings = await mass.config.get_player_config(streamdetails.queue_id)
    if player_settings.get_value(CONF_VOLUME_NORMALIZATION):
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


async def resolve_radio_stream(mass: MusicAssistant, url: str) -> tuple[str, bool, bool]:
    """
    Resolve a streaming radio URL.

    Unwraps any playlists if needed.
    Determines if the stream supports ICY metadata.

    Returns tuple;
    - unfolded URL as string
    - bool if the URL represents a ICY (radio) stream.
    - bool uf the URL represents a HLS stream/playlist.
    """
    base_url = url.split("?")[0]
    cache_key = f"RADIO_RESOLVED_{url}"
    if cache := await mass.cache.get(cache_key):
        return cache
    is_hls = False
    is_icy = False
    resolved_url = url
    timeout = ClientTimeout(total=0, connect=10, sock_read=5)
    try:
        async with mass.http_session.get(
            url, headers=HTTP_HEADERS_ICY, allow_redirects=True, timeout=timeout
        ) as resp:
            resolved_url = str(resp.real_url)
            headers = resp.headers
            resp.raise_for_status()
            if not resp.headers:
                raise InvalidDataError("no headers found")
        is_icy = headers.get("icy-metaint") is not None
        is_hls = headers.get("content-type") in HLS_CONTENT_TYPES
        if (
            base_url.endswith((".m3u", ".m3u8", ".pls"))
            or headers.get("content-type") == "audio/x-mpegurl"
        ):
            # url is playlist, we need to unfold it
            substreams = await fetch_playlist(mass, resolved_url)
            if not any(x for x in substreams if x.length):
                try:
                    for line in substreams:
                        if not line.is_url:
                            continue
                        # unfold first url of playlist
                        return await resolve_radio_stream(mass, line.path)
                    raise InvalidDataError("No content found in playlist")
                except IsHLSPlaylist:
                    is_hls = True

    except Exception as err:
        LOGGER.warning("Error while parsing radio URL %s: %s", url, err)
        return (resolved_url, is_icy, is_hls)

    result = (resolved_url, is_icy, is_hls)
    cache_expiration = 30 * 24 * 3600 if url == resolved_url else 600
    await mass.cache.set(cache_key, result, expiration=cache_expiration)
    return result


async def get_icy_stream(
    mass: MusicAssistant, url: str, streamdetails: StreamDetails
) -> AsyncGenerator[bytes, None]:
    """Get (radio) audio stream from HTTP, including ICY metadata retrieval."""
    timeout = ClientTimeout(total=0, connect=30, sock_read=5 * 60)
    LOGGER.debug("Start streaming radio with ICY metadata from url %s", url)
    async with mass.http_session.get(url, headers=HTTP_HEADERS_ICY, timeout=timeout) as resp:
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
            stream_title = stream_title.group(1).decode()
            cleaned_stream_title = clean_stream_title(stream_title)
            if cleaned_stream_title != streamdetails.stream_title:
                LOGGER.log(VERBOSE_LOG_LEVEL, "ICY Radio streamtitle original: %s", stream_title)
                LOGGER.log(
                    VERBOSE_LOG_LEVEL, "ICY Radio streamtitle cleaned: %s", cleaned_stream_title
                )
                streamdetails.stream_title = cleaned_stream_title


async def get_hls_stream(
    mass: MusicAssistant,
    url: str,
    streamdetails: StreamDetails,
    seek_position: int = 0,
) -> AsyncGenerator[bytes, None]:
    """Get audio stream from HTTP HLS stream."""
    logger = LOGGER.getChild("hls_stream")
    logger.debug("Start streaming HLS stream for url %s", url)
    timeout = ClientTimeout(total=0, connect=30, sock_read=5 * 60)
    prev_chunks: deque[str] = deque(maxlen=50)
    has_playlist_metadata: bool | None = None
    has_id3_metadata: bool | None = None
    is_live_stream = streamdetails.media_type == MediaType.RADIO or not streamdetails.duration
    # we simply select the best quality substream here
    # if we ever want to support adaptive stream selection based on bandwidth
    # we need to move the substream selection into the loop below and make it
    # bandwidth aware. For now we just assume domestic high bandwidth where
    # the user wants the best quality possible at all times.
    playlist_item = await get_hls_substream(mass, url)
    substream_url = playlist_item.path
    seconds_skipped = 0
    empty_loops = 0
    while True:
        logger.log(VERBOSE_LOG_LEVEL, "start streaming chunks from substream %s", substream_url)
        async with mass.http_session.get(
            substream_url, headers=HTTP_HEADERS, timeout=timeout
        ) as resp:
            resp.raise_for_status()
            charset = resp.charset or "utf-8"
            substream_m3u_data = await resp.text(charset)
        # get chunk-parts from the substream
        hls_chunks = parse_m3u(substream_m3u_data)
        chunk_seconds = 0
        time_start = time.time()
        for chunk_item in hls_chunks:
            if chunk_item.path in prev_chunks:
                continue
            chunk_length = int(chunk_item.length) if chunk_item.length else 6
            # try to support seeking here
            if seek_position and (seconds_skipped + chunk_length) < seek_position:
                seconds_skipped += chunk_length
                continue
            chunk_item_url = chunk_item.path
            if not chunk_item_url.startswith("http"):
                # path is relative, stitch it together
                base_path = substream_url.rsplit("/", 1)[0]
                chunk_item_url = base_path + "/" + chunk_item.path
            # handle (optional) in-playlist (timed) metadata
            if has_playlist_metadata is None:
                has_playlist_metadata = chunk_item.title not in (None, "")
                logger.debug("Station support for in-playlist metadata: %s", has_playlist_metadata)
            if has_playlist_metadata and chunk_item.title != "no desc":
                # bbc (and maybe others?) set the title to 'no desc'
                cleaned_stream_title = clean_stream_title(chunk_item.title)
                if cleaned_stream_title != streamdetails.stream_title:
                    logger.log(
                        VERBOSE_LOG_LEVEL, "HLS Radio streamtitle original: %s", chunk_item.title
                    )
                    logger.log(
                        VERBOSE_LOG_LEVEL, "HLS Radio streamtitle cleaned: %s", cleaned_stream_title
                    )
                    streamdetails.stream_title = cleaned_stream_title
            logger.log(VERBOSE_LOG_LEVEL, "playing chunk %s", chunk_item)
            # prevent that we play this chunk again if we loop through
            prev_chunks.append(chunk_item.path)
            async with mass.http_session.get(
                chunk_item_url, headers=HTTP_HEADERS, timeout=timeout
            ) as resp:
                yield await resp.content.read()
            chunk_seconds += chunk_length
            # handle (optional) in-band (m3u) metadata
            if has_id3_metadata is not None and has_playlist_metadata:
                continue
            if has_id3_metadata in (None, True):
                tags = await parse_tags(chunk_item_url)
                has_id3_metadata = tags.title and tags.title not in chunk_item.path
                logger.debug("Station support for in-band (ID3) metadata: %s", has_id3_metadata)
        # end of stream reached - for non livestreams, we are ready and should return
        # for livestreams we loop around to get the next playlist with chunks
        if not is_live_stream:
            return
        # safeguard for an endless loop
        # this may happen if we're simply going too fast for the live stream
        # we already throttle it a bit but we may end up in a situation where something is wrong
        # and we want to break out of this loop, hence this check
        if chunk_seconds == 0:
            empty_loops += 1
            await asyncio.sleep(1)
        else:
            empty_loops = 0
        if empty_loops == 50:
            logger.warning("breaking out of endless loop")
            break
        # ensure that we're not going to fast - otherwise we get the same substream playlist
        while (time.time() - time_start) < (chunk_seconds - 1):
            await asyncio.sleep(0.5)


async def get_hls_substream(
    mass: MusicAssistant,
    url: str,
) -> PlaylistItem:
    """Select the (highest quality) HLS substream for given HLS playlist/URL."""
    timeout = ClientTimeout(total=0, connect=30, sock_read=5 * 60)
    # fetch master playlist and select (best) child playlist
    # https://datatracker.ietf.org/doc/html/draft-pantos-http-live-streaming-19#section-10
    async with mass.http_session.get(url, headers=HTTP_HEADERS, timeout=timeout) as resp:
        resp.raise_for_status()
        charset = resp.charset or "utf-8"
        master_m3u_data = await resp.text(charset)
    substreams = parse_m3u(master_m3u_data)
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
        async with mass.http_session.head(url, headers=HTTP_HEADERS) as resp:
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
    async with mass.http_session.get(url, headers=headers, timeout=timeout) as resp:
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


async def get_ffmpeg_stream(
    audio_input: AsyncGenerator[bytes, None] | str,
    input_format: AudioFormat,
    output_format: AudioFormat,
    filter_params: list[str] | None = None,
    extra_args: list[str] | None = None,
    chunk_size: int | None = None,
    extra_input_args: list[str] | None = None,
    logger: logging.Logger | None = None,
) -> AsyncGenerator[bytes, None]:
    """
    Get the ffmpeg audio stream as async generator.

    Takes care of resampling and/or recoding if needed,
    according to player preferences.
    """
    async with FFMpeg(
        audio_input=audio_input,
        input_format=input_format,
        output_format=output_format,
        filter_params=filter_params,
        extra_args=extra_args,
        extra_input_args=extra_input_args,
        logger=logger,
    ) as ffmpeg_proc:
        # read final chunks from stdout
        iterator = (
            ffmpeg_proc.iter_chunked(chunk_size)
            if chunk_size
            else ffmpeg_proc.iter_any(get_chunksize(output_format))
        )
        async for chunk in iterator:
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
    extra_input_args: list[str] | None = None,
    loglevel: str = "error",
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
        "-nostats",
        "-ignore_unknown",
        "-protocol_whitelist",
        "file,hls,http,https,tcp,tls,crypto,pipe,data,fd,rtp,udp",
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
            "-i",
            input_path,
        ]
    else:
        # let ffmpeg auto detect the content type from the metadata/headers
        input_args += ["-i", input_path]

    # collect output args
    output_args = []
    if output_path.upper() == "NULL":
        # devnull stream
        output_args = ["-f", "null", "-"]
    elif output_format.content_type == ContentType.UNKNOWN:
        raise RuntimeError("Invalid output format specified")
    elif output_format.content_type == ContentType.AAC:
        output_args = ["-f", "adts", output_path]
    else:
        if output_format.content_type.is_pcm():
            output_args += ["-acodec", output_format.content_type.name.lower()]
        # use explicit format identifier for all other
        output_args += [
            "-f",
            output_format.content_type.value,
            "-ar",
            str(output_format.sample_rate),
            "-ac",
            str(output_format.channels),
            output_path,
        ]

    # edge case: source file is not stereo - downmix to stereo
    if input_format.channels > 2 and output_format.channels == 2:
        filter_params = [
            "pan=stereo|FL=1.0*FL+0.707*FC+0.707*SL+0.707*LFE|FR=1.0*FR+0.707*FC+0.707*SR+0.707*LFE",
            *filter_params,
        ]

    # determine if we need to do resampling
    if (
        input_format.sample_rate != output_format.sample_rate
        or input_format.bit_depth > output_format.bit_depth
    ):
        # prefer resampling with libsoxr due to its high quality
        if libsoxr_support:
            resample_filter = "aresample=resampler=soxr:precision=30"
        else:
            resample_filter = "aresample=resampler=swr"

        # sample rate conversion
        if input_format.sample_rate != output_format.sample_rate:
            resample_filter += f":osr={output_format.sample_rate}"

        # bit depth conversion: apply dithering when going down to 16 bits
        if output_format.bit_depth < input_format.bit_depth:
            resample_filter += ":osf=s16:dither_method=triangular_hp"

        filter_params.append(resample_filter)

    if filter_params and "-filter_complex" not in extra_args:
        extra_args += ["-af", ",".join(filter_params)]

    return generic_args + input_args + extra_args + output_args


def parse_loudnorm(raw_stderr: bytes | str) -> LoudnessMeasurement | None:
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
    return LoudnessMeasurement(
        integrated=float(loudness_data["input_i"]),
        true_peak=float(loudness_data["input_tp"]),
        lra=float(loudness_data["input_lra"]),
        threshold=float(loudness_data["input_thresh"]),
        target_offset=float(loudness_data["target_offset"]),
    )
