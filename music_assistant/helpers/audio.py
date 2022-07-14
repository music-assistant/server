"""Various helpers for audio manipulation."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import struct
from io import BytesIO
from time import time
from typing import TYPE_CHECKING, AsyncGenerator, List, Optional, Tuple

import aiofiles
from aiohttp import ClientTimeout

from music_assistant.helpers.process import AsyncProcess, check_output
from music_assistant.helpers.util import create_tempfile
from music_assistant.models.errors import (
    AudioError,
    MediaNotFoundError,
    MusicAssistantError,
)
from music_assistant.models.media_items import ContentType, MediaType, StreamDetails

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant
    from music_assistant.models.player_queue import QueueItem

LOGGER = logging.getLogger(__name__)

# pylint:disable=consider-using-f-string


async def crossfade_pcm_parts(
    fade_in_part: bytes,
    fade_out_part: bytes,
    fade_length: int,
    fmt: ContentType,
    sample_rate: int,
    channels: int = 2,
) -> bytes:
    """Crossfade two chunks of pcm/raw audio using ffmpeg."""
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
        fmt.value,
        "-ac",
        str(channels),
        "-ar",
        str(sample_rate),
        "-i",
        fadeoutfile.name,
        # fade_in part (stdin)
        "-acodec",
        fmt.name.lower(),
        "-f",
        fmt.value,
        "-ac",
        str(channels),
        "-ar",
        str(sample_rate),
        "-i",
        "-",
        # filter args
        "-filter_complex",
        f"[0][1]acrossfade=d={fade_length}",
        # output args
        "-f",
        fmt.value,
        "-",
    ]
    async with AsyncProcess(args, True) as proc:
        crossfade_data, _ = await proc.communicate(fade_in_part)
        if crossfade_data:
            LOGGER.debug(
                "crossfaded 2 pcm chunks. fade_in_part: %s - fade_out_part: %s - result: %s",
                len(fade_in_part),
                len(fade_out_part),
                len(crossfade_data),
            )
            return crossfade_data
        # no crossfade_data, return original data instead
        LOGGER.debug(
            "crossfade of pcm chunks failed: not enough data. fade_in_part: %s - fade_out_part: %s",
            len(fade_in_part),
            len(fade_out_part),
        )
        return fade_out_part + fade_in_part


async def strip_silence(
    audio_data: bytes,
    fmt: ContentType,
    sample_rate: int,
    channels: int = 2,
    reverse=False,
) -> bytes:
    """Strip silence from (a chunk of) pcm audio."""
    # input args
    args = ["ffmpeg", "-hide_banner", "-loglevel", "quiet"]
    args += [
        "-acodec",
        fmt.name.lower(),
        "-f",
        fmt.value,
        "-ac",
        str(channels),
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
    args += ["-f", fmt.value, "-"]
    async with AsyncProcess(args, True) as proc:
        stripped_data, _ = await proc.communicate(audio_data)
        LOGGER.debug(
            "stripped silence of pcm chunk. size before: %s - after: %s",
            len(audio_data),
            len(stripped_data),
        )
        return stripped_data


async def analyze_audio(mass: MusicAssistant, streamdetails: StreamDetails) -> None:
    """Analyze track audio, for now we only calculate EBU R128 loudness."""

    if streamdetails.loudness is not None:
        # only when needed we do the analyze job
        return

    LOGGER.debug("Start analyzing track %s", streamdetails.uri)
    # calculate BS.1770 R128 integrated loudness with ffmpeg
    started = time()
    input_file = streamdetails.direct or "-"
    proc_args = [
        "ffmpeg",
        "-i",
        input_file,
        "-f",
        streamdetails.content_type.value,
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

        async def writer():
            """Task that grabs the source audio and feeds it to ffmpeg."""
            music_prov = mass.music.get_provider(streamdetails.provider)
            async for audio_chunk in music_prov.get_audio_stream(streamdetails):
                await ffmpeg_proc.write(audio_chunk)
                if (time() - started) > 300:
                    # just in case of endless radio stream etc
                    break
            ffmpeg_proc.write_eof()

        if streamdetails.direct is None:
            writer_task = ffmpeg_proc.attach_task(writer())
            # wait for the writer task to finish
            await writer_task

        _, stderr = await ffmpeg_proc.communicate()
        try:
            loudness_str = (
                stderr.decode()
                .split("Integrated loudness")[1]
                .split("I:")[1]
                .split("LUFS")[0]
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


async def get_stream_details(
    mass: MusicAssistant, queue_item: "QueueItem", queue_id: str = ""
) -> StreamDetails:
    """
    Get streamdetails for the given QueueItem.

    This is called just-in-time when a PlayerQueue wants a MediaItem to be played.
    Do not try to request streamdetails in advance as this is expiring data.
        param media_item: The MediaItem (track/radio) for which to request the streamdetails for.
        param queue_id: Optionally provide the queue_id which will play this stream.
    """
    streamdetails = None
    if queue_item.streamdetails and (time() < queue_item.streamdetails.expires):
        # we already have fresh streamdetails, use these
        queue_item.streamdetails.seconds_skipped = 0
        queue_item.streamdetails.seconds_streamed = 0
        streamdetails = queue_item.streamdetails
    else:
        # fetch streamdetails from provider
        # always request the full item as there might be other qualities available
        full_item = await mass.music.get_item_by_uri(queue_item.uri)
        # sort by quality and check track availability
        for prov_media in sorted(
            full_item.provider_ids, key=lambda x: x.quality or 0, reverse=True
        ):
            if not prov_media.available:
                continue
            # get streamdetails from provider
            music_prov = mass.music.get_provider(prov_media.prov_id)
            if not music_prov or not music_prov.available:
                continue  # provider temporary unavailable ?
            try:
                streamdetails: StreamDetails = await music_prov.get_stream_details(
                    prov_media.item_id
                )
                streamdetails.content_type = ContentType(streamdetails.content_type)
            except MusicAssistantError as err:
                LOGGER.warning(str(err))
            else:
                break

    if not streamdetails:
        raise MediaNotFoundError(f"Unable to retrieve streamdetails for {queue_item}")

    # set queue_id on the streamdetails so we know what is being streamed
    streamdetails.queue_id = queue_id
    # get gain correct / replaygain
    if streamdetails.gain_correct is None:
        loudness, gain_correct = await get_gain_correct(mass, streamdetails)
        streamdetails.gain_correct = gain_correct
        streamdetails.loudness = loudness
    if not streamdetails.duration:
        streamdetails.duration = queue_item.duration
    # set streamdetails as attribute on the media_item
    # this way the app knows what content is playing
    queue_item.streamdetails = streamdetails
    return streamdetails


async def get_gain_correct(
    mass: MusicAssistant, streamdetails: StreamDetails
) -> Tuple[Optional[float], Optional[float]]:
    """Get gain correction for given queue / track combination."""
    queue = mass.players.get_player_queue(streamdetails.queue_id)
    if not queue or not queue.settings.volume_normalization_enabled:
        return (None, None)
    if streamdetails.gain_correct is not None:
        return (streamdetails.loudness, streamdetails.gain_correct)
    target_gain = queue.settings.volume_normalization_target
    track_loudness = await mass.music.get_track_loudness(
        streamdetails.item_id, streamdetails.provider
    )
    if track_loudness is None:
        # fallback to provider average
        fallback_track_loudness = await mass.music.get_provider_loudness(
            streamdetails.provider
        )
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


async def get_media_stream(
    mass: MusicAssistant,
    streamdetails: StreamDetails,
    pcm_fmt: ContentType,
    sample_rate: int,
    channels: int = 2,
    seek_position: int = 0,
    chunk_size: int = 64000,
) -> AsyncGenerator[bytes, None]:
    """Get the PCM audio stream for the given streamdetails."""
    assert pcm_fmt.is_pcm(), "Output format must be a PCM type"
    args = await _get_ffmpeg_args(
        streamdetails,
        pcm_fmt,
        pcm_sample_rate=sample_rate,
        pcm_channels=channels,
        seek_position=seek_position,
    )
    async with AsyncProcess(
        args, enable_stdin=streamdetails.direct is None
    ) as ffmpeg_proc:

        LOGGER.debug("start media stream for: %s", streamdetails.uri)

        async def writer():
            """Task that grabs the source audio and feeds it to ffmpeg."""
            LOGGER.debug("writer started for %s", streamdetails.uri)
            music_prov = mass.music.get_provider(streamdetails.provider)
            async for audio_chunk in music_prov.get_audio_stream(
                streamdetails, seek_position
            ):
                await ffmpeg_proc.write(audio_chunk)
            # write eof when last packet is received
            ffmpeg_proc.write_eof()
            LOGGER.debug("writer finished for %s", streamdetails.uri)

        if streamdetails.direct is None:
            ffmpeg_proc.attach_task(writer())

        # yield chunks from stdout
        try:
            async for chunk in ffmpeg_proc.iter_chunked(chunk_size):
                yield chunk

        except (asyncio.CancelledError, GeneratorExit) as err:
            LOGGER.debug("media stream aborted for: %s", streamdetails.uri)
            raise err
        else:
            LOGGER.debug("finished media stream for: %s", streamdetails.uri)
            await mass.music.mark_item_played(
                streamdetails.item_id, streamdetails.provider
            )
        finally:
            # report playback
            if streamdetails.callback:
                mass.create_task(streamdetails.callback, streamdetails)
            # send analyze job to background worker
            if streamdetails.loudness is None:
                mass.add_job(
                    analyze_audio(mass, streamdetails),
                    f"Analyze audio for {streamdetails.uri}",
                )


async def get_radio_stream(
    mass: MusicAssistant, url: str, streamdetails: StreamDetails
) -> AsyncGenerator[bytes, None]:
    """Get radio audio stream from HTTP, including metadata retrieval."""
    headers = {"Icy-MetaData": "1"}
    timeout = ClientTimeout(total=0, connect=30, sock_read=120)
    async with mass.http_session.get(url, headers=headers, timeout=timeout) as resp:
        headers = resp.headers
        meta_int = int(headers.get("icy-metaint", "0"))
        # stream with ICY Metadata
        if meta_int:
            while True:
                audio_chunk = await resp.content.readexactly(meta_int)
                yield audio_chunk
                meta_byte = await resp.content.readexactly(1)
                meta_length = ord(meta_byte) * 16
                meta_data = await resp.content.readexactly(meta_length)
                if not meta_data:
                    continue
                meta_data = meta_data.rstrip(b"\0")
                stream_title = re.search(rb"StreamTitle='([^']*)';", meta_data)
                if not stream_title:
                    continue
                stream_title = stream_title.group(1).decode()
                if stream_title != streamdetails.stream_title:
                    streamdetails.stream_title = stream_title
                    if queue := mass.players.get_player_queue(streamdetails.queue_id):
                        queue.signal_update()
        # Regular HTTP stream
        else:
            async for chunk in resp.content.iter_any():
                yield chunk


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
    timeout = ClientTimeout(total=0, connect=30, sock_read=120)
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
    mass: MusicAssistant,
    filename: str,
    streamdetails: StreamDetails,
    seek_position: int = 0,
) -> AsyncGenerator[bytes, None]:
    """Get audio stream from local accessible file."""
    if seek_position:
        assert streamdetails.duration, "Duration required for seek requests"
    if not streamdetails.size:
        stat = await mass.loop.run_in_executor(None, os.stat, filename)
        streamdetails.size = stat.st_size
    chunk_size = get_chunksize(streamdetails.content_type)
    async with aiofiles.open(streamdetails.data, "rb") as _file:
        if seek_position:
            seek_pos = int(
                (streamdetails.size / streamdetails.duration) * seek_position
            )
            await _file.seek(seek_pos)
        # yield chunks of data from file
        while True:
            data = await _file.read(chunk_size)
            if not data:
                break
            yield data


async def check_audio_support(try_install: bool = False) -> Tuple[bool, bool]:
    """Check if ffmpeg is present (with/without libsoxr support)."""
    cache_key = "audio_support_cache"
    if cache := globals().get(cache_key):
        return cache

    # check for FFmpeg presence
    returncode, output = await check_output("ffmpeg -version")
    ffmpeg_present = returncode == 0 and "FFmpeg" in output.decode()
    if not ffmpeg_present and try_install:
        # try a few common ways to install ffmpeg
        # this all assumes we have enough rights and running on a linux based platform (or docker)
        await check_output("apt-get update && apt-get install ffmpeg")
        await check_output("apk add ffmpeg")
        # test again
        returncode, output = await check_output("ffmpeg -version")
        ffmpeg_present = returncode == 0 and "FFmpeg" in output.decode()

    # use globals as in-memory cache
    libsoxr_support = "enable-libsoxr" in output.decode()
    result = (ffmpeg_present, libsoxr_support)
    globals()[cache_key] = result
    return result


async def get_preview_stream(
    mass: MusicAssistant,
    provider_id: str,
    track_id: str,
) -> AsyncGenerator[bytes, None]:
    """Create a 30 seconds preview audioclip for the given streamdetails."""
    music_prov = mass.music.get_provider(provider_id)

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
        if streamdetails.content_type != ContentType.UNKNOWN:
            input_args += ["-f", streamdetails.content_type.value]
        input_args += ["-i", "-"]

    output_args = ["-to", "30", "-f", "mp3", "-"]
    args = input_args + output_args
    async with AsyncProcess(args, True) as ffmpeg_proc:

        async def writer():
            """Task that grabs the source audio and feeds it to ffmpeg."""
            music_prov = mass.music.get_provider(streamdetails.provider)
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
    output_fmt: ContentType = ContentType.WAV,
    sample_rate: int = 44100,
    bit_depth: int = 16,
    channels: int = 2,
) -> AsyncGenerator[bytes, None]:
    """Create stream of silence, encoded to format of choice."""

    # wav silence = just zero's
    if output_fmt == ContentType.WAV:
        yield create_wave_header(
            samplerate=sample_rate,
            channels=2,
            bitspersample=bit_depth,
            duration=duration,
        )
        for _ in range(0, duration):
            yield b"\0" * int(sample_rate * (bit_depth / 8) * channels)
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
        f"anullsrc=r={sample_rate}:cl={'stereo' if channels == 2 else 'mono'}",
        "-t",
        str(duration),
        "-f",
        output_fmt.value,
        "-",
    ]
    async with AsyncProcess(args) as ffmpeg_proc:
        async for chunk in ffmpeg_proc.iter_any():
            yield chunk


def get_chunksize(
    content_type: ContentType,
    sample_rate: int = 44100,
    bit_depth: int = 16,
    channels: int = 2,
    seconds: int = 1,
) -> int:
    """Get a default chunksize for given contenttype."""
    pcm_size = int(sample_rate * (bit_depth / 8) * channels * seconds)
    if content_type.is_pcm() or content_type == ContentType.WAV:
        return pcm_size
    if content_type in (ContentType.WAV, ContentType.AIFF, ContentType.DSF):
        return pcm_size
    if content_type in (ContentType.FLAC, ContentType.WAVPACK, ContentType.ALAC):
        return int(pcm_size * 0.6)
    if content_type in (ContentType.MP3, ContentType.OGG, ContentType.M4A):
        return int(640000 * seconds)
    return 32000 * seconds


async def _get_ffmpeg_args(
    streamdetails: StreamDetails,
    pcm_output_format: ContentType,
    pcm_sample_rate: int,
    pcm_channels: int = 2,
    seek_position: int = 0,
) -> List[str]:
    """Collect all args to send to the ffmpeg process."""
    input_format = streamdetails.content_type
    assert pcm_output_format.is_pcm(), "Output format needs to be PCM"

    ffmpeg_present, libsoxr_support = await check_audio_support()

    if not ffmpeg_present:
        raise AudioError(
            "FFmpeg binary is missing from system."
            "Please install ffmpeg on your OS to enable playback.",
        )
    # collect input args
    input_args = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "quiet",
        "-ignore_unknown",
    ]
    if streamdetails.direct:
        # ffmpeg can access the inputfile (or url) directly
        if seek_position:
            input_args += ["-ss", str(seek_position)]
        input_args += ["-i", streamdetails.direct]
    else:
        # the input is received from pipe/stdin
        if streamdetails.content_type != ContentType.UNKNOWN:
            input_args += ["-f", input_format.value]
        input_args += ["-i", "-"]

    # collect output args
    output_args = [
        "-acodec",
        pcm_output_format.name.lower(),
        "-f",
        pcm_output_format.value,
        "-ac",
        str(pcm_channels),
        "-ar",
        str(pcm_sample_rate),
        "-",
    ]
    # collect extra and filter args
    extra_args = []
    filter_params = []
    if streamdetails.gain_correct is not None:
        filter_params.append(f"volume={streamdetails.gain_correct}dB")
    if (
        streamdetails.sample_rate != pcm_sample_rate
        and libsoxr_support
        and streamdetails.media_type == MediaType.TRACK
    ):
        # prefer libsoxr high quality resampler (if present) for sample rate conversions
        filter_params.append("aresample=resampler=soxr")
    if filter_params:
        extra_args += ["-af", ",".join(filter_params)]

    return input_args + extra_args + output_args
