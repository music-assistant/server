"""Various helpers for audio manipulation."""

import asyncio
import logging
import struct
from io import BytesIO
from typing import List, Optional, Tuple

from music_assistant.helpers.process import AsyncProcess
from music_assistant.helpers.typing import MusicAssistant, QueueItem
from music_assistant.helpers.util import create_tempfile
from music_assistant.models.media_types import MediaType
from music_assistant.models.streamdetails import ContentType, StreamDetails, StreamType

LOGGER = logging.getLogger("audio")


async def crossfade_pcm_parts(
    fade_in_part: bytes, fade_out_part: bytes, pcm_args: List[str], fade_length: int
) -> bytes:
    """Crossfade two chunks of pcm/raw audio using sox."""
    # create fade-in part
    fadeinfile = create_tempfile()
    args = ["sox", "--ignore-length", "-t"] + pcm_args
    args += ["-", "-t"] + pcm_args + [fadeinfile.name, "fade", "t", str(fade_length)]
    async with AsyncProcess(args, enable_write=True) as sox_proc:
        await sox_proc.communicate(fade_in_part)
    # create fade-out part
    fadeoutfile = create_tempfile()
    args = ["sox", "--ignore-length", "-t"] + pcm_args + ["-", "-t"] + pcm_args
    args += [fadeoutfile.name, "reverse", "fade", "t", str(fade_length), "reverse"]
    async with AsyncProcess(args, enable_write=True) as sox_proc:
        await sox_proc.communicate(fade_out_part)
    # create crossfade using sox and some temp files
    # TODO: figure out how to make this less complex and without the tempfiles
    args = ["sox", "-m", "-v", "1.0", "-t"] + pcm_args + [fadeoutfile.name, "-v", "1.0"]
    args += ["-t"] + pcm_args + [fadeinfile.name, "-t"] + pcm_args + ["-"]
    async with AsyncProcess(args, enable_write=False) as sox_proc:
        crossfade_part, _ = await sox_proc.communicate()
    fadeinfile.close()
    fadeoutfile.close()
    del fadeinfile
    del fadeoutfile
    return crossfade_part


async def strip_silence(audio_data: bytes, pcm_args: List[str], reverse=False) -> bytes:
    """Strip silence from (a chunk of) pcm audio."""
    args = ["sox", "--ignore-length", "-t"] + pcm_args + ["-", "-t"] + pcm_args + ["-"]
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
        # only when needed we do the analyze stuff
        return

    # only when needed we do the analyze stuff
    LOGGER.debug(
        "Start analyzing track %s/%s",
        streamdetails.provider,
        streamdetails.item_id,
    )
    # calculate BS.1770 R128 integrated loudness with ffmpeg
    if streamdetails.type == StreamType.EXECUTABLE:
        proc_args = (
            "%s | ffmpeg -i pipe: -af ebur128=framelog=verbose -f null - 2>&1 | awk '/I:/{print $2}'"
            % streamdetails.path
        )
    else:
        proc_args = (
            "ffmpeg -i '%s' -af ebur128=framelog=verbose -f null - 2>&1 | awk '/I:/{print $2}'"
            % streamdetails.path
        )
    audio_data = b""
    if streamdetails.media_type == MediaType.RADIO:
        proc_args = "ffmpeg -i pipe: -af ebur128=framelog=verbose -f null - 2>&1 | awk '/I:/{print $2}'"
        # for radio we collect ~10 minutes of audio data to process
        async with mass.http_session.get(streamdetails.path) as response:
            async for chunk, _ in response.content.iter_chunks():
                audio_data += chunk
                if len(audio_data) >= 20000:
                    break

    proc = await asyncio.create_subprocess_shell(
        proc_args,
        stdout=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.PIPE if audio_data else None,
    )
    value, _ = await proc.communicate(audio_data or None)
    loudness = float(value.decode().strip())
    await mass.database.set_track_loudness(
        streamdetails.item_id, streamdetails.provider, loudness
    )
    LOGGER.debug(
        "Integrated loudness of %s/%s is: %s",
        streamdetails.provider,
        streamdetails.item_id,
        loudness,
    )


async def get_stream_details(
    mass: MusicAssistant, queue_item: QueueItem, player_id: str = ""
) -> StreamDetails:
    """
    Get streamdetails for the given media_item.

    This is called just-in-time when a player/queue wants a MediaItem to be played.
    Do not try to request streamdetails in advance as this is expiring data.
        param media_item: The MediaItem (track/radio) for which to request the streamdetails for.
        param player_id: Optionally provide the player_id which will play this stream.
    """
    if queue_item.provider == "url":
        # special case: a plain url was added to the queue
        streamdetails = StreamDetails(
            type=StreamType.URL,
            provider="url",
            item_id=queue_item.item_id,
            path=queue_item.uri if queue_item.uri else queue_item.item_id,
            content_type=ContentType(queue_item.uri.split(".")[-1]),
        )
    else:
        # always request the full db track as there might be other qualities available
        # except for radio
        if queue_item.media_type == MediaType.RADIO:
            full_track = await mass.music.get_radio(
                queue_item.item_id, queue_item.provider
            )
        else:
            full_track = await mass.music.get_track(
                queue_item.item_id, queue_item.provider
            )
        if not full_track:
            return None
        # sort by quality and check track availability
        for prov_media in sorted(
            full_track.provider_ids, key=lambda x: x.quality, reverse=True
        ):
            if not prov_media.available:
                continue
            # get streamdetails from provider
            music_prov = mass.get_provider(prov_media.provider)
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

    if streamdetails:
        # set player_id on the streamdetails so we know what players stream
        streamdetails.player_id = player_id
        # get gain correct / replaygain
        if queue_item.name == "alert":
            loudness = 5
            gain_correct = 0
        else:
            loudness, gain_correct = await get_gain_correct(
                mass, player_id, streamdetails.item_id, streamdetails.provider
            )
        streamdetails.gain_correct = gain_correct
        streamdetails.loudness = loudness
        # set streamdetails as attribute on the media_item
        # this way the app knows what content is playing
        queue_item.streamdetails = streamdetails
        return streamdetails
    return None


async def get_gain_correct(
    mass: MusicAssistant, player_id: str, item_id: str, provider_id: str
) -> Tuple[float, float]:
    """Get gain correction for given player / track combination."""
    player_conf = mass.config.get_player_config(player_id)
    if not player_conf["volume_normalisation"]:
        return 0
    target_gain = int(player_conf["target_volume"])
    track_loudness = await mass.database.get_track_loudness(item_id, provider_id)
    if track_loudness is None:
        # fallback to provider average
        fallback_track_loudness = await mass.database.get_provider_loudness(provider_id)
        if fallback_track_loudness is None:
            # fallback to some (hopefully sane) average value for now
            fallback_track_loudness = -8.5
        gain_correct = target_gain - fallback_track_loudness
    else:
        gain_correct = target_gain - track_loudness
    gain_correct = round(gain_correct, 2)
    return (track_loudness, gain_correct)


def create_wave_header(samplerate=44100, channels=2, bitspersample=16, duration=3600):
    """Generate a wave header from given params."""
    # pylint: disable=no-member
    file = BytesIO()
    numsamples = samplerate * duration

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
    data_chunk_spec = b"<4sL"
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


def get_sox_args(
    streamdetails: StreamDetails,
    output_format: Optional[ContentType] = None,
    resample: Optional[int] = None,
):
    """Collect all args to send to the sox (or ffmpeg) process."""
    stream_path = streamdetails.path
    stream_type = StreamType(streamdetails.type)
    content_type = streamdetails.content_type
    if output_format is None:
        output_format = streamdetails.content_type

    # use ffmpeg if content not supported by SoX (e.g. AAC radio streams)
    if not streamdetails.content_type.sox_supported():
        # collect input args
        input_args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", stream_path]
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
            filter_args += ["-filter:a", "volume=%sdB" % streamdetails.gain_correct]
        if resample:
            filter_args += ["-ar", str(resample)]
        return input_args + filter_args + output_args

    # Prefer SoX for all other (=highest quality)
    if stream_type == StreamType.EXECUTABLE:
        # stream from executable
        input_args = [
            stream_path,
            "|",
            "sox",
            "-t",
            content_type.sox_format(),
            "-",
        ]
    else:
        input_args = ["sox", "-t", content_type.sox_format(), stream_path]
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
    if resample and streamdetails.content_type == ContentType.FLAC:
        # use extra high quality resampler only if it makes sense
        filter_args += ["rate", "-v", str(resample)]
    elif resample:
        filter_args += ["rate", str(resample)]
    # TODO: still not sure about the order of the filter arguments in the chain
    # assumption is they need to be at the end of the chain
    return input_args + output_args + filter_args
