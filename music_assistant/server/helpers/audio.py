"""Various helpers for audio manipulation."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import struct
from contextlib import suppress
from io import BytesIO
from time import time
from typing import TYPE_CHECKING

import aiofiles
from aiohttp import ClientResponseError, ClientTimeout

from music_assistant.common.models.errors import (
    AudioError,
    InvalidDataError,
    MediaNotFoundError,
    MusicAssistantError,
)
from music_assistant.common.models.media_items import (
    AudioFormat,
    ContentType,
    MediaType,
    StreamDetails,
)
from music_assistant.constants import (
    CONF_VOLUME_NORMALIZATION,
    CONF_VOLUME_NORMALIZATION_TARGET,
    ROOT_LOGGER_NAME,
)
from music_assistant.server.helpers.playlists import fetch_playlist

from .process import AsyncProcess, check_output
from .util import create_tempfile

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from music_assistant.common.models.player_queue import QueueItem
    from music_assistant.server import MusicAssistant

LOGGER = logging.getLogger(f"{ROOT_LOGGER_NAME}.audio")

# pylint:disable=consider-using-f-string,too-many-locals,too-many-statements


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
            LOGGER.debug(
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
    if LOGGER.isEnabledFor(logging.DEBUG):
        pcm_sample_size = int(sample_rate * (bit_depth / 8) * 2)
        seconds_stripped = round(bytes_stripped / pcm_sample_size, 2)
        location = "end" if reverse else "begin"
        LOGGER.debug(
            "stripped %s seconds of silence from %s of pcm audio. bytes stripped: %s",
            seconds_stripped,
            location,
            bytes_stripped,
        )
    return stripped_data


async def analyze_audio(mass: MusicAssistant, streamdetails: StreamDetails) -> None:
    """Analyze track audio, for now we only calculate EBU R128 loudness."""
    if streamdetails.loudness is not None:
        # only when needed we do the analyze job
        return

    LOGGER.debug("Start analyzing audio for %s", streamdetails.uri)
    # calculate BS.1770 R128 integrated loudness with ffmpeg
    input_file = streamdetails.direct or "-"
    proc_args = [
        "ffmpeg",
        "-protocol_whitelist",
        "file,http,https,tcp,tls,crypto,pipe,fd",
        "-t",
        "300",  # limit to 5 minutes to prevent OOM
        "-i",
        input_file,
        "-f",
        streamdetails.audio_format.content_type,
        "-af",
        "ebur128=framelog=verbose",
        "-f",
        "null",
        "-",
    ]
    async with AsyncProcess(
        proc_args,
        enable_stdin=streamdetails.direct is None,
        enable_stdout=False,
        enable_stderr=True,
    ) as ffmpeg_proc:

        async def writer() -> None:
            """Task that grabs the source audio and feeds it to ffmpeg."""
            music_prov = mass.get_provider(streamdetails.provider)
            chunk_count = 0
            async for audio_chunk in music_prov.get_audio_stream(streamdetails):
                chunk_count += 1
                await ffmpeg_proc.write(audio_chunk)
                if chunk_count == 300:
                    # safety guard: max (more or less) 5 minutes seconds of audio may be analyzed
                    break
            ffmpeg_proc.write_eof()

        if streamdetails.direct is None:
            writer_task = ffmpeg_proc.attach_task(writer())
            # wait for the writer task to finish
            await writer_task

        _, stderr = await ffmpeg_proc.communicate()
        try:
            loudness_str = (
                stderr.decode().split("Integrated loudness")[1].split("I:")[1].split("LUFS")[0]
            )
            loudness = float(loudness_str.strip())
        except (IndexError, ValueError, AttributeError):
            LOGGER.warning(
                "Could not determine integrated loudness of %s - %s",
                streamdetails.uri,
                stderr.decode() or "received empty value",
            )
        else:
            streamdetails.loudness = loudness
            await mass.music.set_track_loudness(
                streamdetails.item_id, streamdetails.provider, loudness
            )
            LOGGER.debug(
                "Integrated loudness of %s is: %s",
                streamdetails.uri,
                loudness,
            )


async def set_stream_details(mass: MusicAssistant, queue_item: QueueItem) -> None:
    """Set streamdetails for the given QueueItem.

    This is called just-in-time when a PlayerQueue wants a MediaItem to be played.
    Do not try to request streamdetails in advance as this is expiring data.
        param media_item: The QueueItem for which to request the streamdetails for.
    """
    streamdetails = None
    if queue_item.streamdetails and (time() < (queue_item.streamdetails.expires - 360)):
        LOGGER.debug(f"Using cached streamdetails for {queue_item.uri}")
        # we already have fresh streamdetails, use these
        queue_item.streamdetails.seconds_skipped = None
        queue_item.streamdetails.seconds_streamed = None
        streamdetails = queue_item.streamdetails
    else:
        # fetch streamdetails from provider
        # always request the full item as there might be other qualities available
        full_item = await mass.music.get_item_by_uri(queue_item.uri)
        # sort by quality and check track availability
        for prov_media in sorted(
            full_item.provider_mappings, key=lambda x: x.quality or 0, reverse=True
        ):
            if not prov_media.available:
                LOGGER.debug(f"Skipping unavailable {prov_media}")
                continue
            # get streamdetails from provider
            music_prov = mass.get_provider(prov_media.provider_instance)
            if not music_prov:
                LOGGER.debug(f"Skipping {prov_media} - provider not available")
                continue  # provider not available ?
            try:
                streamdetails: StreamDetails = await music_prov.get_stream_details(
                    prov_media.item_id
                )
            except MusicAssistantError as err:
                LOGGER.warning(str(err))
            else:
                break

    if not streamdetails:
        msg = f"Unable to retrieve streamdetails for {queue_item}"
        raise MediaNotFoundError(msg)

    # set queue_id on the streamdetails so we know what is being streamed
    streamdetails.queue_id = queue_item.queue_id
    # get gain correct / replaygain
    if streamdetails.gain_correct is None:
        loudness, gain_correct = await get_gain_correct(mass, streamdetails)
        streamdetails.gain_correct = gain_correct
        streamdetails.loudness = loudness
    if not streamdetails.duration:
        streamdetails.duration = queue_item.duration
    # make sure that ffmpeg handles mpeg dash streams directly
    if (
        streamdetails.audio_format.content_type == ContentType.MPEG_DASH
        and streamdetails.data
        and streamdetails.data.startswith("http")
    ):
        streamdetails.direct = streamdetails.data
    # set streamdetails as attribute on the queue_item
    queue_item.streamdetails = streamdetails


async def get_gain_correct(
    mass: MusicAssistant, streamdetails: StreamDetails
) -> tuple[float | None, float | None]:
    """Get gain correction for given queue / track combination."""
    player_settings = await mass.config.get_player_config(streamdetails.queue_id)
    if not player_settings or not player_settings.get_value(CONF_VOLUME_NORMALIZATION):
        return (None, None)
    if streamdetails.gain_correct is not None:
        return (streamdetails.loudness, streamdetails.gain_correct)
    target_gain = player_settings.get_value(CONF_VOLUME_NORMALIZATION_TARGET)
    track_loudness = await mass.music.get_track_loudness(
        streamdetails.item_id, streamdetails.provider
    )
    if track_loudness is None:
        # fallback to provider average
        fallback_track_loudness = await mass.music.get_provider_loudness(streamdetails.provider)
        if fallback_track_loudness is None:
            # fallback to some (hopefully sane) average value for now
            fallback_track_loudness = -8.5
        gain_correct = target_gain - fallback_track_loudness
    else:
        gain_correct = target_gain - track_loudness
    gain_correct = round(gain_correct, 2)
    return (track_loudness, gain_correct)


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


async def get_media_stream(  # noqa: PLR0915
    mass: MusicAssistant,
    streamdetails: StreamDetails,
    pcm_format: AudioFormat,
    seek_position: int = 0,
    fade_in: bool = False,
    strip_silence_begin: bool = False,
    strip_silence_end: bool = False,
) -> AsyncGenerator[tuple[bool, bytes], None]:
    """
    Get the (raw PCM) audio stream for the given streamdetails.

    Other than stripping silence at end and beginning and optional
    volume normalization this is the pure, unaltered audio data as PCM chunks.
    """
    bytes_sent = 0
    streamdetails.seconds_skipped = seek_position
    is_radio = streamdetails.media_type == MediaType.RADIO or not streamdetails.duration
    if is_radio or seek_position:
        strip_silence_begin = False
    # chunk size = 2 seconds of pcm audio
    pcm_sample_size = int(pcm_format.sample_rate * (pcm_format.bit_depth / 8) * 2)
    chunk_size = pcm_sample_size * (1 if is_radio else 2)
    expected_chunks = int((streamdetails.duration or 0) / 2)
    if expected_chunks < 60:
        strip_silence_end = False

    # collect all arguments for ffmpeg
    seek_pos = seek_position if (streamdetails.direct or not streamdetails.can_seek) else 0
    args = await _get_ffmpeg_args(
        streamdetails=streamdetails,
        pcm_output_format=pcm_format,
        # only use ffmpeg seeking if the provider stream does not support seeking
        seek_position=seek_pos,
        fade_in=fade_in,
    )

    async with AsyncProcess(args, enable_stdin=streamdetails.direct is None) as ffmpeg_proc:
        LOGGER.debug("start media stream for: %s", streamdetails.uri)

        async def writer() -> None:
            """Task that grabs the source audio and feeds it to ffmpeg."""
            LOGGER.debug("writer started for %s", streamdetails.uri)
            music_prov = mass.get_provider(streamdetails.provider)
            seek_pos = seek_position if streamdetails.can_seek else 0
            async for audio_chunk in music_prov.get_audio_stream(streamdetails, seek_pos):
                await ffmpeg_proc.write(audio_chunk)
            # write eof when last packet is received
            ffmpeg_proc.write_eof()
            LOGGER.debug("writer finished for %s", streamdetails.uri)

        if streamdetails.direct is None:
            ffmpeg_proc.attach_task(writer())

        # get pcm chunks from stdout
        # we always stay one chunk behind to properly detect end of chunks
        # so we can strip silence at the beginning and end of a track
        prev_chunk = b""
        chunk_num = 0
        try:
            async for chunk in ffmpeg_proc.iter_chunked(chunk_size):
                chunk_num += 1
                if strip_silence_begin and chunk_num == 2:
                    # first 2 chunks received, strip silence of beginning
                    stripped_audio = await strip_silence(
                        mass,
                        prev_chunk + chunk,
                        sample_rate=pcm_format.sample_rate,
                        bit_depth=pcm_format.bit_depth,
                    )
                    yield (False, stripped_audio)
                    bytes_sent += len(stripped_audio)
                    prev_chunk = b""
                    del stripped_audio
                    continue
                if strip_silence_end and chunk_num >= (expected_chunks - 6):
                    # last part of the track, collect multiple chunks to strip silence later
                    prev_chunk += chunk
                    continue

                # middle part of the track, send previous chunk and collect current chunk
                if prev_chunk:
                    yield (False, prev_chunk)
                    bytes_sent += len(prev_chunk)

                prev_chunk = chunk

            # all chunks received, strip silence of last part
            if strip_silence_end:
                stripped_audio = await strip_silence(
                    mass,
                    prev_chunk,
                    sample_rate=pcm_format.sample_rate,
                    bit_depth=pcm_format.bit_depth,
                    reverse=True,
                )
                yield (True, stripped_audio)
                bytes_sent += len(stripped_audio)
                del stripped_audio
            else:
                yield (True, prev_chunk)
                bytes_sent += len(prev_chunk)

            del prev_chunk

            # update duration details based on the actual pcm data we sent
            streamdetails.seconds_streamed = bytes_sent / pcm_sample_size
            streamdetails.duration = seek_position + streamdetails.seconds_streamed

        except (asyncio.CancelledError, GeneratorExit):
            LOGGER.debug("media stream aborted for: %s", streamdetails.uri)
            raise
        else:
            LOGGER.debug("finished media stream for: %s", streamdetails.uri)
        finally:
            # report playback
            await mass.music.mark_item_played(
                streamdetails.media_type, streamdetails.item_id, streamdetails.provider
            )
            if streamdetails.callback:
                mass.create_task(streamdetails.callback, streamdetails)
            # send analyze job to background worker
            if streamdetails.loudness is None:
                mass.create_task(analyze_audio(mass, streamdetails))


async def resolve_radio_stream(mass: MusicAssistant, url: str) -> tuple[str, bool]:
    """
    Resolve a streaming radio URL.

    Unwraps any playlists if needed.
    Determines if the stream supports ICY metadata.

    Returns unfolded URL and a bool if the URL supports ICY metadata.
    """
    cache_key = f"resolved_radio_url_{url}"
    if cache := await mass.cache.get(cache_key):
        return cache
    # handle playlisted radio urls
    is_mpeg_dash = False
    supports_icy = False
    if ".m3u" in url or ".pls" in url:
        # url is playlist, try to figure out how to handle it
        with suppress(InvalidDataError, IndexError):
            playlist = await fetch_playlist(mass, url)
            if len(playlist) > 1 or ".m3u" in playlist[0] or ".pls" in playlist[0]:
                # if it is an mpeg-dash stream, let ffmpeg handle that
                is_mpeg_dash = True
            url = playlist[0]
    if not is_mpeg_dash:
        # determine ICY metadata support by looking at the http headers
        headers = {"Icy-MetaData": "1", "User-Agent": "VLC/3.0.2.LibVLC/3.0.2"}
        timeout = ClientTimeout(total=0, connect=10, sock_read=5)
        try:
            async with mass.http_session.head(
                url, headers=headers, allow_redirects=True, timeout=timeout
            ) as resp:
                headers = resp.headers
                supports_icy = int(headers.get("icy-metaint", "0")) > 0
        except ClientResponseError as err:
            LOGGER.debug("Error while parsing radio URL %s: %s", url, err)

    result = (url, supports_icy)
    await mass.cache.set(cache_key, result, expiration=86400)
    return result


async def get_radio_stream(
    mass: MusicAssistant, url: str, streamdetails: StreamDetails
) -> AsyncGenerator[bytes, None]:
    """Get radio audio stream from HTTP, including metadata retrieval."""
    headers = {"Icy-MetaData": "1", "User-Agent": "VLC/3.0.2.LibVLC/3.0.2"}
    timeout = ClientTimeout(total=0, connect=30, sock_read=60)
    async with mass.http_session.get(url, headers=headers, timeout=timeout) as resp:
        headers = resp.headers
        meta_int = int(headers.get("icy-metaint", "0"))
        # stream with ICY Metadata
        if meta_int:
            LOGGER.debug("Start streaming radio with ICY metadata from url %s", url)
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
        # Regular HTTP stream
        else:
            LOGGER.debug("Start streaming radio without ICY metadata from url %s", url)
            async for chunk in resp.content.iter_any():
                yield chunk
        LOGGER.debug("Finished streaming radio from url %s", url)


async def get_http_stream(
    mass: MusicAssistant,
    url: str,
    streamdetails: StreamDetails,
    seek_position: int = 0,
) -> AsyncGenerator[bytes, None]:
    """Get audio stream from HTTP."""
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
    async with mass.http_session.get(url, headers=headers, timeout=timeout) as resp:
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


async def check_audio_support() -> tuple[bool, bool, str]:
    """Check if ffmpeg is present (with/without libsoxr support)."""
    cache_key = "audio_support_cache"
    if cache := globals().get(cache_key):
        return cache

    # check for FFmpeg presence
    returncode, output = await check_output("ffmpeg -version")
    ffmpeg_present = returncode == 0 and "FFmpeg" in output.decode()

    # use globals as in-memory cache
    version = output.decode().split("ffmpeg version ")[1].split(" ")[0].split("-")[0]
    libsoxr_support = "enable-libsoxr" in output.decode()
    result = (ffmpeg_present, libsoxr_support, version)
    globals()[cache_key] = result
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
    async with AsyncProcess(args, True) as ffmpeg_proc:

        async def writer() -> None:
            """Task that grabs the source audio and feeds it to ffmpeg."""
            music_prov = mass.get_provider(streamdetails.provider)
            async for audio_chunk in music_prov.get_audio_stream(streamdetails, 30):
                await ffmpeg_proc.write(audio_chunk)
            # write eof when last packet is received
            ffmpeg_proc.write_eof()

        if not streamdetails.direct:
            ffmpeg_proc.attach_task(writer())

        # yield chunks from stdout
        async for chunk in ffmpeg_proc.iter_any():
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
        output_format.output_fmt.value,
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


async def _get_ffmpeg_args(
    streamdetails: StreamDetails,
    pcm_output_format: AudioFormat,
    seek_position: int = 0,
    fade_in: bool = False,
) -> list[str]:
    """Collect all args to send to the ffmpeg process."""
    ffmpeg_present, libsoxr_support, version = await check_audio_support()

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
        "warning" if LOGGER.isEnabledFor(logging.DEBUG) else "quiet",
        "-ignore_unknown",
        "-protocol_whitelist",
        "file,http,https,tcp,tls,crypto,pipe,data,fd",
    ]
    # collect input args
    input_args = [
        "-ac",
        str(streamdetails.audio_format.channels),
        "-channel_layout",
        "mono" if streamdetails.audio_format.channels == 1 else "stereo",
    ]
    if seek_position:
        input_args += ["-ss", str(seek_position)]
    if streamdetails.direct:
        # ffmpeg can access the inputfile (or url) directly
        if streamdetails.direct.startswith("http"):
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

        input_args += ["-i", streamdetails.direct]
    else:
        # the input is received from pipe/stdin
        if streamdetails.audio_format.content_type != ContentType.UNKNOWN:
            input_args += ["-f", streamdetails.audio_format.content_type.value]
        input_args += [
            "-i",
            "-",
        ]

    # collect output args
    output_args = [
        "-acodec",
        pcm_output_format.content_type.name.lower(),
        "-f",
        pcm_output_format.content_type.value,
        "-ac",
        str(pcm_output_format.channels),
        "-ar",
        str(pcm_output_format.sample_rate),
        "-",
    ]
    # collect extra and filter args
    extra_args = []
    filter_params = []
    if streamdetails.gain_correct is not None:
        filter_params.append(f"volume={streamdetails.gain_correct}dB")
    if (
        streamdetails.audio_format.sample_rate != pcm_output_format.sample_rate
        and libsoxr_support
        and streamdetails.media_type == MediaType.TRACK
    ):
        # prefer libsoxr high quality resampler (if present) for sample rate conversions
        filter_params.append("aresample=resampler=soxr")
    if fade_in:
        filter_params.append("afade=type=in:start_time=0:duration=3")
    if filter_params:
        extra_args += ["-af", ",".join(filter_params)]

    return generic_args + input_args + extra_args + output_args
