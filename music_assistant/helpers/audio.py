"""Various helpers for audio manipulation."""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import struct
from base64 import b64encode
from io import BytesIO
from tempfile import gettempdir
from time import time
from typing import TYPE_CHECKING, AsyncGenerator, List, Optional, Tuple

import aiofiles

from music_assistant.helpers.process import AsyncProcess, check_output
from music_assistant.helpers.util import create_tempfile
from music_assistant.models.enums import EventType, ProviderType
from music_assistant.models.errors import AudioError, MediaNotFoundError
from music_assistant.models.event import MassEvent
from music_assistant.models.media_items import (
    ContentType,
    MediaType,
    StreamDetails,
    StreamType,
)

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant
    from music_assistant.models.player_queue import QueueItem

LOGGER = logging.getLogger(__name__)
CACHE_DIR = os.path.join(gettempdir(), "mass")
if not os.path.isdir(CACHE_DIR):
    os.mkdir(CACHE_DIR)

# pylint:disable=consider-using-f-string


async def crossfade_pcm_parts(
    fade_in_part: bytes,
    fade_out_part: bytes,
    fade_length: int,
    fmt: ContentType,
    sample_rate: int,
) -> bytes:
    """Crossfade two chunks of pcm/raw audio using sox."""
    _, ffmpeg_present = await check_audio_support()

    # prefer ffmpeg implementation (due to simplicity)
    if ffmpeg_present:
        fadeoutfile = create_tempfile()
        async with aiofiles.open(fadeoutfile.name, "wb") as outfile:
            await outfile.write(fade_out_part)
        # input args
        args = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
        args += [
            "-f",
            fmt.value,
            "-ac",
            "2",
            "-ar",
            str(sample_rate),
            "-i",
            fadeoutfile.name,
        ]
        args += ["-f", fmt.value, "-ac", "2", "-ar", str(sample_rate), "-i", "-"]
        # filter args
        args += ["-filter_complex", f"[0][1]acrossfade=d={fade_length}"]
        # output args
        args += ["-f", fmt.value, "-"]
        async with AsyncProcess(args, True) as proc:
            crossfade_data, _ = await proc.communicate(fade_in_part)
            return crossfade_data

    # sox based implementation
    sox_args = [fmt.sox_format(), "-c", "2", "-r", str(sample_rate)]
    # create fade-in part
    fadeinfile = create_tempfile()
    args = ["sox", "--ignore-length", "-t"] + sox_args
    args += ["-", "-t"] + sox_args + [fadeinfile.name, "fade", "t", str(fade_length)]
    async with AsyncProcess(args, enable_write=True) as sox_proc:
        await sox_proc.communicate(fade_in_part)
    # create fade-out part
    fadeoutfile = create_tempfile()
    args = ["sox", "--ignore-length", "-t"] + sox_args + ["-", "-t"] + sox_args
    args += [fadeoutfile.name, "reverse", "fade", "t", str(fade_length), "reverse"]
    async with AsyncProcess(args, enable_write=True) as sox_proc:
        await sox_proc.communicate(fade_out_part)
    # create crossfade using sox and some temp files
    # TODO: figure out how to make this less complex and without the tempfiles
    args = ["sox", "-m", "-v", "1.0", "-t"] + sox_args + [fadeoutfile.name, "-v", "1.0"]
    args += ["-t"] + sox_args + [fadeinfile.name, "-t"] + sox_args + ["-"]
    async with AsyncProcess(args, enable_write=False) as sox_proc:
        crossfade_part, _ = await sox_proc.communicate()
    fadeinfile.close()
    fadeoutfile.close()
    del fadeinfile
    del fadeoutfile
    return crossfade_part


async def fadein_pcm_part(
    pcm_audio: bytes,
    fade_length: int,
    fmt: ContentType,
    sample_rate: int,
) -> bytes:
    """Fadein chunk of pcm/raw audio using ffmpeg."""
    # input args
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
    args += [
        "-f",
        fmt.value,
        "-ac",
        "2",
        "-ar",
        str(sample_rate),
        "-i",
        "-",
    ]
    # filter args
    args += ["-af", f"afade=type=in:start_time=0:duration={fade_length}"]
    # output args
    args += ["-f", fmt.value, "-"]
    async with AsyncProcess(args, True) as proc:
        result_audio, _ = await proc.communicate(pcm_audio)
        return result_audio


async def strip_silence(
    audio_data: bytes, fmt: ContentType, sample_rate: int, reverse=False
) -> bytes:
    """Strip silence from (a chunk of) pcm audio."""
    _, ffmpeg_present = await check_audio_support()
    # prefer ffmpeg implementation
    if ffmpeg_present:
        # input args
        args = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
        args += ["-f", fmt.value, "-ac", "2", "-ar", str(sample_rate), "-i", "-"]
        # filter args
        if reverse:
            args += ["-af", "areverse,silenceremove=1:0:-50dB:detection=peak,areverse"]
        else:
            args += ["-af", "silenceremove=1:0:-50dB:detection=peak"]
        # output args
        args += ["-f", fmt.value, "-"]
        async with AsyncProcess(args, True) as proc:
            stripped_data, _ = await proc.communicate(audio_data)
            return stripped_data

    # sox implementation
    sox_args = [fmt.sox_format(), "-c", "2", "-r", str(sample_rate)]
    args = ["sox", "--ignore-length", "-t"] + sox_args + ["-", "-t"] + sox_args + ["-"]
    if reverse:
        args.append("reverse")
    args += ["silence", "1", "0.1", "1%"]
    if reverse:
        args.append("reverse")
    async with AsyncProcess(args, enable_write=True) as sox_proc:
        stripped_data, _ = await sox_proc.communicate(audio_data)
    return stripped_data


async def analyze_audio(mass: MusicAssistant, streamdetails: StreamDetails) -> None:
    """Analyze track audio, for now we only calculate EBU R128 loudness."""

    if streamdetails.loudness is not None:
        # only when needed we do the analyze job
        return

    if streamdetails.type not in (StreamType.URL, StreamType.CACHE, StreamType.FILE):
        return

    LOGGER.debug("Start analyzing track %s", streamdetails.uri)
    # calculate BS.1770 R128 integrated loudness with ffmpeg
    audio_data = b""
    if streamdetails.media_type == MediaType.RADIO:
        proc_args = "ffmpeg -i pipe: -af ebur128=framelog=verbose -f null - 2>&1 | awk '/I:/{print $2}'"
        # for radio we collect ~10 minutes of audio data to process
        async with mass.http_session.get(streamdetails.path) as response:
            async for chunk, _ in response.content.iter_chunks():
                audio_data += chunk
                if len(audio_data) >= 20000:
                    break
    else:
        proc_args = (
            "ffmpeg -i '%s' -af ebur128=framelog=verbose -f null - 2>&1 | awk '/I:/{print $2}'"
            % streamdetails.path
        )

    proc = await asyncio.create_subprocess_shell(
        proc_args,
        stdout=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.PIPE if audio_data else None,
    )
    stdout, _ = await proc.communicate(audio_data or None)
    try:
        loudness = float(stdout.decode().strip())
    except (ValueError, AttributeError):
        LOGGER.warning(
            "Could not determine integrated loudness of %s - %s",
            streamdetails.uri,
            stdout.decode() or "received empty value",
        )
    else:
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
    if (
        queue_item.streamdetails is not None
        and (time() - queue_item.streamdetails.timestamp) < 600
    ):
        # we already have fresh streamdetails, use these
        queue_item.streamdetails.seconds_skipped = 0
        queue_item.streamdetails.seconds_streamed = 0
        return queue_item.streamdetails
    if not queue_item.media_item:
        # special case: a plain url was added to the queue
        streamdetails = StreamDetails(
            type=StreamType.URL,
            provider=ProviderType.URL,
            item_id=queue_item.item_id,
            path=queue_item.uri,
            content_type=ContentType.try_parse(queue_item.uri),
        )
    else:
        # always request the full db track as there might be other qualities available
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

            streamdetails: StreamDetails = await music_prov.get_stream_details(
                prov_media.item_id
            )
            if streamdetails:
                try:
                    streamdetails.content_type = ContentType(streamdetails.content_type)
                except KeyError:
                    LOGGER.warning("Invalid content type!")
                else:
                    break

    if not streamdetails:
        raise MediaNotFoundError(f"Unable to retrieve streamdetails for {queue_item}")

    # set player_id on the streamdetails so we know what players stream
    streamdetails.queue_id = queue_id
    # get gain correct / replaygain
    loudness, gain_correct = await get_gain_correct(
        mass, queue_id, streamdetails.item_id, streamdetails.provider
    )
    streamdetails.gain_correct = gain_correct
    streamdetails.loudness = loudness
    # check if cache file exists
    tmpfile = get_temp_filename(streamdetails.provider, streamdetails.item_id)
    if os.path.isfile(tmpfile):
        streamdetails.path = tmpfile
        streamdetails.type = StreamType.CACHE
    # set streamdetails as attribute on the media_item
    # this way the app knows what content is playing
    queue_item.streamdetails = streamdetails
    return streamdetails


async def get_gain_correct(
    mass: MusicAssistant, queue_id: str, item_id: str, provider: ProviderType
) -> Tuple[float, float]:
    """Get gain correction for given queue / track combination."""
    queue = mass.players.get_player_queue(queue_id)
    if not queue or not queue.settings.volume_normalization_enabled:
        return (0, 0)
    target_gain = queue.settings.volume_normalization_target
    track_loudness = await mass.music.get_track_loudness(item_id, provider)
    if track_loudness is None:
        # fallback to provider average
        fallback_track_loudness = await mass.music.get_provider_loudness(provider)
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


async def get_sox_args(
    streamdetails: StreamDetails,
    output_format: Optional[ContentType] = None,
    resample: Optional[int] = None,
    seek_position: Optional[int] = None,
    use_file: bool = False,
) -> List[str]:
    """Collect all args to send to the sox (or ffmpeg) process."""
    input_format = streamdetails.content_type
    if output_format is None:
        output_format = input_format

    sox_present, ffmpeg_present = await check_audio_support()
    use_ffmpeg = not sox_present or not input_format.sox_supported() or seek_position

    input_filename = streamdetails.path if use_file else "-"

    # use ffmpeg if content not supported by SoX (e.g. AAC radio streams)
    if use_ffmpeg:
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
            "error",
            "-f",
            input_format.value,
            "-i",
            input_filename,
        ]
        if seek_position:
            input_args += ["-ss", str(seek_position)]
        # collect output args
        if output_format.is_pcm():
            output_args = [
                "-f",
                output_format.value,
                "-c:a",
                output_format.name.lower(),
                "-",
            ]
        else:
            output_args = ["-f", output_format.value, "-"]
        # collect filter args
        filter_args = []
        if streamdetails.gain_correct:
            filter_args += ["-filter:a", f"volume={streamdetails.gain_correct}dB"]
        if resample or input_format.is_pcm():
            filter_args += ["-ar", str(resample)]
        return input_args + filter_args + output_args

    # Prefer SoX for all other (=highest quality)
    if input_format.is_pcm():
        input_args = [
            "sox",
            "-t",
            input_format.sox_format(),
            "-r",
            str(streamdetails.sample_rate),
            "-c",
            str(streamdetails.channels),
            input_filename,
        ]
    else:
        input_args = ["sox", "-t", input_format.sox_format(), input_filename]

    # collect output args
    if output_format.is_pcm():
        output_args = ["-t", output_format.sox_format(), "-c", "2", "-"]
    elif output_format == ContentType.FLAC:
        output_args = ["-t", "flac", "-C", "0", "-"]
    else:
        output_args = ["-t", output_format.sox_format(), "-"]
    # collect filter args
    filter_args = []
    if streamdetails.gain_correct:
        filter_args += ["vol", str(streamdetails.gain_correct), "dB"]
    if resample and streamdetails.media_type != MediaType.RADIO:
        # use extra high quality resampler only if it makes sense
        filter_args += ["rate", "-v", str(resample)]
    elif resample:
        filter_args += ["rate", str(resample)]
    return input_args + output_args + filter_args


async def get_media_stream(
    mass: MusicAssistant,
    streamdetails: StreamDetails,
    output_format: Optional[ContentType] = None,
    resample: Optional[int] = None,
    chunk_size: Optional[int] = None,
    seek_position: Optional[int] = None,
) -> AsyncGenerator[Tuple[bool, bytes], None]:
    """Get the audio stream for the given streamdetails."""

    mass.signal_event(
        MassEvent(
            EventType.STREAM_STARTED,
            object_id=streamdetails.provider.value,
            data=streamdetails,
        )
    )
    use_file = streamdetails.type in (StreamType.CACHE, StreamType.FILE)
    args = await get_sox_args(
        streamdetails, output_format, resample, seek_position, use_file
    )
    async with AsyncProcess(
        args, enable_write=not use_file, chunk_size=chunk_size
    ) as sox_proc:

        LOGGER.debug(
            "start media stream for: %s (%s)",
            streamdetails.uri,
            streamdetails.type,
        )

        async def writer():
            """Task that grabs the source audio and feeds it to sox/ffmpeg."""
            LOGGER.debug("writer started for %s", streamdetails.uri)
            async for audio_chunk in _get_source_stream(mass, streamdetails):
                if sox_proc.closed:
                    return
                await sox_proc.write(audio_chunk)
                del audio_chunk
            LOGGER.debug("writer finished for %s", streamdetails.uri)
            # write eof when last packet is received
            sox_proc.write_eof()

        if not use_file:
            sox_proc.attach_task(writer())

        # yield chunks from stdout
        # we keep 1 chunk behind to detect end of stream properly
        try:
            prev_chunk = b""
            async for chunk in sox_proc.iterate_chunks():
                if prev_chunk:
                    yield (False, prev_chunk)
                    del prev_chunk
                prev_chunk = chunk
            # send last chunk
            yield (True, prev_chunk)
            del prev_chunk
        except (asyncio.CancelledError, GeneratorExit) as err:
            LOGGER.debug("media stream aborted for: %s", streamdetails.uri)
            raise err
        else:
            LOGGER.debug("finished media stream for: %s", streamdetails.uri)
            await mass.music.mark_item_played(
                streamdetails.item_id, streamdetails.provider
            )
        finally:
            mass.signal_event(
                MassEvent(
                    EventType.STREAM_ENDED,
                    object_id=streamdetails.provider.value,
                    data=streamdetails,
                )
            )
            # send analyze job to background worker
            if (
                streamdetails.loudness is None
                and streamdetails.provider != ProviderType.URL
            ):
                mass.add_job(
                    analyze_audio(mass, streamdetails),
                    f"Analyze audio for {streamdetails.uri}",
                )


async def _get_source_stream(
    mass: MusicAssistant, streamdetails: StreamDetails
) -> AsyncGenerator[bytes, None]:
    """Get source media stream."""
    allow_cache = (
        streamdetails.media_type == MediaType.TRACK
        and streamdetails.type in (StreamType.URL, StreamType.EXECUTABLE)
    )
    async with aiofiles.tempfile.NamedTemporaryFile("wb") as _tmpfile:
        if streamdetails.type == StreamType.EXECUTABLE:
            async with AsyncProcess(streamdetails.path) as proc:
                async for chunk in proc.iterate_chunks():
                    yield chunk
                    if allow_cache:
                        await _tmpfile.write(chunk)
                    del chunk
        elif streamdetails.type == StreamType.URL:
            async with mass.http_session.get(streamdetails.path) as resp:
                async for chunk in resp.content.iter_any():
                    yield chunk
                    if allow_cache:
                        await _tmpfile.write(chunk)
                    del chunk
        elif streamdetails.type in (StreamType.FILE, StreamType.CACHE):
            async with aiofiles.open(streamdetails.path, "rb") as _file:
                async for chunk in _file:
                    yield chunk
                    del chunk
        # we streamed the full content, now save the cache file for later use
        # this cache file is used for 2 purposes:
        # 1: this same track is being seeked
        # 2: the audio needs to be analysed
        if allow_cache:
            # move to final location
            cachefile = get_temp_filename(streamdetails.provider, streamdetails.item_id)
            await mass.loop.run_in_executor(None, shutil.move, _tmpfile.name, cachefile)
            streamdetails.type = StreamType.CACHE
            streamdetails.path = cachefile


async def check_audio_support(try_install: bool = False) -> Tuple[bool, bool, bool]:
    """Check if sox and/or ffmpeg are present."""
    cache_key = "audio_support_cache"
    if cache := globals().get(cache_key):
        return cache
    # check for SoX presence
    returncode, output = await check_output("sox --version")
    sox_present = returncode == 0 and "SoX" in output.decode()
    if not sox_present and try_install:
        # try a few common ways to install SoX
        # this all assumes we have enough rights and running on a linux based platform (or docker)
        await check_output("apt-get update && apt-get install sox libsox-fmt-all")
        await check_output("apk add sox")
        # test again
        returncode, output = await check_output("sox --version")
        sox_present = returncode == 0 and "SoX" in output.decode()

    # check for FFmpeg presence
    returncode, output = await check_output("ffmpeg -version")
    ffmpeg_present = returncode == 0 and "FFmpeg" in output.decode()
    if not ffmpeg_present and try_install:
        # try a few common ways to install SoX
        # this all assumes we have enough rights and running on a linux based platform (or docker)
        await check_output("apt-get update && apt-get install ffmpeg")
        await check_output("apk add ffmpeg")
        # test again
        returncode, output = await check_output("ffmpeg -version")
        ffmpeg_present = returncode == 0 and "FFmpeg" in output.decode()

    # use globals as in-memory cache
    result = (sox_present, ffmpeg_present)
    globals()[cache_key] = result
    return result


async def get_sox_args_for_pcm_stream(
    sample_rate: int,
    bit_depth: int,
    channels: int,
    floating_point: bool = False,
    output_format: ContentType = ContentType.FLAC,
) -> List[str]:
    """Collect args for sox (or ffmpeg) when converting from raw pcm to another contenttype."""

    sox_present, ffmpeg_present = await check_audio_support()
    input_format = ContentType.from_bit_depth(bit_depth, floating_point)
    sox_present = True

    # use ffmpeg if sox is not present
    if not sox_present:
        if not ffmpeg_present:
            raise AudioError(
                "FFmpeg binary is missing from system. "
                "Please install ffmpeg on your OS to enable playback.",
            )
        # collect input args
        input_args = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
        input_args += [
            "-f",
            input_format.value,
            "-ac",
            str(channels),
            "-ar",
            str(sample_rate),
            "-i",
            "-",
        ]
        # collect output args
        output_args = ["-f", output_format.value, "-"]
        return input_args + output_args

    # Prefer SoX for all other (=highest quality)

    # collect input args
    input_args = [
        "sox",
        "-t",
        input_format.sox_format(),
        "-r",
        str(sample_rate),
        "-b",
        str(bit_depth),
        "-c",
        str(channels),
        "-",
    ]
    #  collect output args
    if output_format == ContentType.FLAC:
        output_args = ["-t", "flac", "-C", "0", "-"]
    else:
        output_args = ["-t", output_format.sox_format(), "-"]
    return input_args + output_args


async def get_preview_stream(
    mass: MusicAssistant,
    provider_id: str,
    track_id: str,
) -> AsyncGenerator[Tuple[bool, bytes], None]:
    """Get the audio stream for the given streamdetails."""
    music_prov = mass.music.get_provider(provider_id)

    streamdetails = await music_prov.get_stream_details(track_id)

    if streamdetails.type == StreamType.EXECUTABLE:
        # stream from executable
        input_args = [
            streamdetails.path,
            "|",
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            streamdetails.content_type.value,
            "-i",
            "-",
        ]
    else:
        input_args = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            streamdetails.path,
        ]
    output_args = ["-ss", "30", "-to", "60", "-f", "mp3", "-"]
    async with AsyncProcess(input_args + output_args) as proc:

        # yield chunks from stdout
        # we keep 1 chunk behind to detect end of stream properly
        try:
            prev_chunk = b""
            async for chunk in proc.iterate_chunks():
                if prev_chunk:
                    yield (False, prev_chunk)
                prev_chunk = chunk
            # send last chunk
            yield (True, prev_chunk)
        finally:
            mass.signal_event(
                MassEvent(
                    EventType.STREAM_ENDED,
                    object_id=streamdetails.provider.value,
                    data=streamdetails,
                )
            )


def get_chunksize(content_type: ContentType) -> int:
    """Get a default chunksize for given contenttype."""
    if content_type in (
        ContentType.AAC,
        ContentType.M4A,
        ContentType.MP3,
        ContentType.OGG,
    ):
        return 32000
    return 256000


def get_temp_filename(provider: ProviderType, item_id: str) -> str:
    """Create temp filename for media item."""
    tmpname = b64encode(f"{provider.name}{item_id}".encode()).decode()
    return os.path.join(CACHE_DIR, tmpname)
