"""Various helpers for audio streaming and manipulation."""

from __future__ import annotations

import asyncio
import logging
import re
import struct
import urllib.parse
from collections.abc import AsyncGenerator, Iterable, Iterator
from contextlib import aclosing
from io import BytesIO
from typing import TYPE_CHECKING, Final

from music_assistant_models.enums import (
    ContentType,
    MediaType,
    PlayerFeature,
    PlayerType,
    VolumeNormalizationMode,
)
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.streamdetails import MultiPartPath

from music_assistant.constants import (
    MASS_LOGGER_NAME,
    VERBOSE_LOG_LEVEL,
)
from music_assistant.helpers.json import JSON_DECODE_EXCEPTIONS, json_loads

from .ffmpeg import get_ffmpeg_stream
from .process import AsyncProcess, communicate

if TYPE_CHECKING:
    from music_assistant_models.media_items import AudioFormat
    from music_assistant_models.streamdetails import StreamDetails

    from music_assistant.mass import MusicAssistant
    from music_assistant.models.player import Player

LOGGER = logging.getLogger(f"{MASS_LOGGER_NAME}.helpers.audio")

HTTP_HEADERS = {"User-Agent": "Lavf/60.16.100.MusicAssistant"}
HTTP_HEADERS_ICY = {**HTTP_HEADERS, "Icy-MetaData": "1"}

SLOW_PROVIDERS = ("tidal", "ytmusic", "apple_music")

# Mapping of audio format identifiers to their correct IANA MIME types
# where the format name differs from the MIME subtype.
# Strict DLNA/UPnP devices reject non-standard MIME types (e.g. audio/mp3).
_MIME_TYPE_OVERRIDES: Final[dict[str, str]] = {
    "mp3": "audio/mpeg",
}


def resolve_output_player_ids(mass: MusicAssistant, player_ids: Iterable[str]) -> set[str]:
    """
    Return user-facing player identifiers for audio output destinations.

    :param mass: Music Assistant instance.
    :param player_ids: Player identifiers to resolve.
    """
    output_player_ids: set[str] = set()
    for player_id in player_ids:
        player = mass.players.get_player(player_id)
        protocol_parent_id = player.protocol_parent_id if player else None
        output_player_ids.add(protocol_parent_id or player_id)
    return output_player_ids


def get_mime_type(format_str: str) -> str:
    """
    Get the proper IANA MIME type for a given audio format string.

    :param format_str: The audio format string (e.g. "mp3", "flac",
        "pcm;codec=pcm;rate=44100;bitrate=16;channels=2").
    """
    base_format = format_str.split(";", maxsplit=1)[0]
    if override := _MIME_TYPE_OVERRIDES.get(base_format):
        return override
    return f"audio/{format_str}"


def parse_pcm_info(content_type: str) -> tuple[int, int, int]:
    """
    Parse PCM info from a codec/content_type string.

    :param content_type: Content type string like "pcm;codec=pcm;rate=44100;bitrate=16;channels=2".
    """
    params = (
        dict(urllib.parse.parse_qsl(content_type.replace(";", "&"))) if ";" in content_type else {}
    )
    sample_rate = int(params.get("rate", 44100))
    sample_size = int(params.get("bitrate", 16))
    channels = int(params.get("channels", 2))
    return (sample_rate, sample_size, channels)


CACHE_CATEGORY_RESOLVED_RADIO_URL: Final[int] = 100
CACHE_PROVIDER: Final[str] = "audio"


def iter_pcm_slices(
    audio: bytes,
    pcm_format: AudioFormat,
    target_duration_ms: int = 100,
) -> Iterator[bytes]:
    """
    Yield frame-aligned PCM slices of approximately ``target_duration_ms``.

    Large PCM buffers (e.g. crossfade segments or full-track reads) are split
    into fixed-size sub-chunks so that downstream consumers get predictable
    chunk sizes for buffering, write-timeout management, and ring-buffer
    bookkeeping.

    :param audio: Raw PCM bytes to slice.
    :param pcm_format: Format description (sample rate, bit depth, channels).
    :param target_duration_ms: Desired slice length in milliseconds (default 100).
    """
    if not audio:
        return
    bytes_per_sample = max(1, pcm_format.bit_depth // 8)
    frame_size = bytes_per_sample * pcm_format.channels
    if frame_size <= 0:
        yield audio
        return
    samples_per_slice = max(1, round((target_duration_ms / 1000) * pcm_format.sample_rate))
    slice_size = max(frame_size, samples_per_slice * frame_size)
    offset = 0
    audio_len = len(audio)
    while offset < audio_len:
        end = min(audio_len, offset + slice_size)
        # Align to frame boundary unless this is the tail of the buffer.
        if end < audio_len:
            aligned_end = end - (end % frame_size)
            if aligned_end <= offset:
                aligned_end = min(audio_len, offset + frame_size)
            end = aligned_end
        yield audio[offset:end]
        offset = end


def align_audio_to_frame_boundary(audio_data: bytes, pcm_format: AudioFormat) -> bytes:
    """
    Align audio data to frame boundaries by truncating incomplete frames.

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
    audio_data: bytes,
    pcm_format: AudioFormat,
    reverse: bool = False,
) -> bytes:
    """
    Strip silence from begin or end of pcm audio using ffmpeg.

    :param audio_data: Raw PCM audio data.
    :param pcm_format: AudioFormat of the audio data.
    :param reverse: If True, strip from end instead of beginning.
    """
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
    if reverse:
        args += [
            "-af",
            "areverse,atrim=start=0.2,silenceremove=start_periods=1"
            ":start_silence=0.1:start_threshold=0.02,areverse",
        ]
    else:
        args += [
            "-af",
            "atrim=start=0.2,silenceremove=start_periods=1:start_silence=0.1:start_threshold=0.02",
        ]
    args += ["-f", pcm_format.content_type.value, "-"]
    _returncode, stripped_data, _stderr = await communicate(args, audio_data)

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


def create_wave_header(
    samplerate: int = 44100,
    channels: int = 2,
    bitspersample: int = 16,
    duration: int | None = None,
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


def parse_extinf_metadata(extinf_line: str) -> dict[str, str]:
    """
    Parse metadata from HLS EXTINF line.

    Extracts structured metadata like title="...", artist="..." from EXTINF lines.
    Common in iHeartRadio and other commercial radio HLS streams.

    :param extinf_line: The EXTINF line containing metadata
    """
    metadata = {}

    # Pattern to match key="value" pairs in the EXTINF line
    # Handles nested quotes by matching everything until the closing quote
    pattern = r'(\w+)="([^"]*)"'

    matches = re.findall(pattern, extinf_line)
    for key, value in matches:
        metadata[key.lower()] = value

    # Fallback: RFC 8216 plain title format `#EXTINF:<duration>,<title>`
    if not metadata and "," in extinf_line:
        title = extinf_line.split(",", 1)[1].strip()
        if title:
            metadata["title"] = title

    return metadata


def get_parts_from_position(
    parts: list[MultiPartPath],
    seek_position: int,
) -> tuple[list[MultiPartPath], int]:
    """
    Get the remaining parts list from a timestamp.

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
                f"Skipping to the next part due to seek position being at the end: {position}",
            )
            if i + 1 < len(parts):
                return parts[i + 1 :], 0
            return parts[i:], int(position)  # last part, cannot skip

        return parts[i:], int(position)

    raise IndexError(f"Could not find any candidate part for position {seek_position}")


def build_concat_filelist(paths: list[str]) -> str:
    """
    Build the file list content for ffmpeg's concat demuxer.

    :param paths: The file paths to include, in playback order.
    """
    lines = []
    for path in paths:
        # The concat demuxer uses single quotes as delimiters, so a literal quote in the
        # path must be written as '\'' to prevent the path being truncated at the quote.
        escaped_path = path.replace("'", "'\\''")
        lines.append(f"file '{escaped_path}'\n")
    return "".join(lines)


async def realtime_pcm_pacer(
    inner: AsyncGenerator[bytes],
    pcm_format: AudioFormat,
) -> AsyncGenerator[bytes]:
    """
    Pace a PCM byte stream at the format's native rate.

    Useful for live AudioSource streams whose producer is not realtime-paced
    (e.g. librespot's pipe backend) — without rate-limiting the consumer would
    buffer many seconds of audio ahead of playback, making skip/next laggy.

    :param inner: Source generator yielding raw PCM bytes.
    :param pcm_format: PCM format the inner generator emits.
    """
    bytes_per_second = pcm_format.sample_rate * pcm_format.channels * (pcm_format.bit_depth // 8)
    if bytes_per_second <= 0 or not pcm_format.content_type.is_pcm():
        # non-PCM or malformed format: pass through unchanged
        async for chunk in inner:
            yield chunk
        return
    loop = asyncio.get_running_loop()
    start_time = loop.time()
    total_bytes = 0
    async for chunk in inner:
        yield chunk
        total_bytes += len(chunk)
        expected_elapsed = total_bytes / bytes_per_second
        actual_elapsed = loop.time() - start_time
        if actual_elapsed < expected_elapsed:
            await asyncio.sleep(expected_elapsed - actual_elapsed)


async def audio_source_silence_keepalive(
    inner: AsyncGenerator[bytes],
    pcm_format: AudioFormat,
    silence_chunk_ms: int = 100,
    idle_threshold_s: float | None = None,
) -> AsyncGenerator[bytes]:
    """
    Wrap a live AudioSource PCM stream and emit silence during idle gaps.

    Plugin providers exposing an AudioSource may stop yielding bytes while the
    upstream device is paused (e.g. user paused in the Spotify app). Without
    bytes flowing the downstream consumer (ffmpeg / the player) may disconnect.
    This wrapper inserts ``silence_chunk_ms`` worth of zero bytes whenever the
    inner generator hasn't produced for ``idle_threshold_s`` seconds, while
    relaying real bytes immediately when they arrive.

    Only meaningful for PCM streams — injecting raw zero bytes into a compressed
    stream (MP3/AAC/etc.) would corrupt the bitstream. For non-PCM ``pcm_format``
    inputs the wrapper degrades to a transparent pass-through.

    :param inner: The underlying async generator yielding raw PCM bytes.
    :param pcm_format: PCM format the inner generator emits (used to size the
        silence chunk so it lines up to a frame boundary).
    :param silence_chunk_ms: Duration of each silence chunk in milliseconds.
    :param idle_threshold_s: Seconds without input before silence is inserted.
        Defaults to the chunk duration so silence flows at realtime — critical
        for keeping HTTP consumers (Sonos, Chromecast) connected.
    """
    if idle_threshold_s is None:
        idle_threshold_s = silence_chunk_ms / 1000
    frame_size = pcm_format.channels * (pcm_format.bit_depth // 8)
    bytes_per_second = (
        pcm_format.sample_rate * frame_size if pcm_format.content_type.is_pcm() else 0
    )
    if bytes_per_second <= 0 or frame_size <= 0:
        # non-PCM or malformed format: pass through unchanged, no silence injection
        async for chunk in inner:
            yield chunk
        return

    # Round the silence chunk size DOWN to a whole-frame multiple so emitted
    # chunks line up to PCM frame boundaries for arbitrary silence_chunk_ms /
    # sample-rate combinations.
    raw_silence_bytes = bytes_per_second * silence_chunk_ms // 1000
    silence_bytes = max(frame_size, (raw_silence_bytes // frame_size) * frame_size)
    silence_chunk = b"\x00" * silence_bytes
    # empty bytes is the end-of-stream sentinel; real PCM frames are never empty
    queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=8)

    async def _producer() -> None:
        # aclosing ensures inner.aclose() runs on cancellation so the underlying
        # generator's own finally (e.g. plugin lock release, fd cleanup) fires
        # instead of leaking until GC.
        try:
            async with aclosing(inner) as managed_inner:
                async for chunk in managed_inner:
                    await queue.put(chunk)
        finally:
            await queue.put(b"")

    producer_task = asyncio.create_task(_producer())
    try:
        while True:
            try:
                chunk = await asyncio.wait_for(queue.get(), timeout=idle_threshold_s)
            except TimeoutError:
                yield silence_chunk
                continue
            if not chunk:
                break
            yield chunk
    finally:
        producer_task.cancel()
        try:
            await producer_task
        except asyncio.CancelledError:
            pass
        except Exception:
            # log but don't re-raise: we're already in a finally and the
            # downstream consumer has its own error handling for the outer stream.
            LOGGER.exception("AudioSource producer task raised")


async def get_silence(
    duration: int,
    output_format: AudioFormat,
) -> AsyncGenerator[bytes]:
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
    input_audio: bytes | AsyncGenerator[bytes],
    input_format: AudioFormat,
    output_format: AudioFormat,
    chunk_size: int | None = None,
) -> AsyncGenerator[bytes]:
    """
    Resample PCM audio from input_format to output_format using ffmpeg.

    Yields chunks of resampled audio as they become available.

    :param input_audio: Raw PCM audio data or async generator of PCM chunks.
    :param input_format: AudioFormat of the input audio.
    :param output_format: Desired AudioFormat for the output audio.
    :param chunk_size: Output chunk size in bytes. Defaults to 1 second of output PCM.
    """
    if chunk_size is None:
        chunk_size = output_format.pcm_sample_size

    async def _as_generator() -> AsyncGenerator[bytes]:
        if isinstance(input_audio, bytes):
            yield input_audio
        else:
            async for chunk in input_audio:
                yield chunk

    if input_format == output_format:
        buffer = b""
        async for chunk in _as_generator():
            buffer += chunk
            while len(buffer) >= chunk_size:
                yield buffer[:chunk_size]
                buffer = buffer[chunk_size:]
        if buffer:
            yield buffer
        return

    async for chunk in get_ffmpeg_stream(
        audio_input=_as_generator(),
        input_format=input_format,
        output_format=output_format,
        chunk_size=chunk_size,
    ):
        yield chunk


def calculate_content_length(
    fmt: AudioFormat,
    seconds: float = 1,
) -> int:
    """
    Calculate the estimated encoded size in bytes for a given format and duration.

    For CBR lossy formats (MP3/AAC), the estimate is near-exact.
    For lossless formats (FLAC), the estimate uses an empirical average
    compression ratio and may differ from actual size by up to ~15%.
    For uncompressed formats (PCM/WAV), the result is exact.

    :param fmt: The audio format to estimate size for.
    :param seconds: Duration in seconds.
    """
    pcm_size = int(fmt.sample_rate * (fmt.bit_depth / 8) * fmt.channels * seconds)
    if fmt.content_type.is_pcm():
        return pcm_size
    if fmt.content_type in (ContentType.WAV, ContentType.AIFF, ContentType.DSF):
        return pcm_size
    if fmt.bit_rate and fmt.bit_rate < 10000:
        return int(((fmt.bit_rate * 1000) / 8) * seconds)
    if fmt.content_type in (ContentType.FLAC, ContentType.WAVPACK, ContentType.ALAC):
        # FLAC compression_level 0: empirical ratio ~74.7% of PCM
        # Source: https://z-issue.com/wp/flac-compression-level-comparison/
        # Real-world variance: 65-85% depending on audio content.
        return int(pcm_size * 0.747)
    if fmt.content_type in (ContentType.MP3, ContentType.OGG):
        # CBR 320kbps as set in get_ffmpeg_args
        return int((320000 / 8) * seconds)
    if fmt.content_type in (ContentType.AAC, ContentType.M4A):
        # CBR 256kbps as set in get_ffmpeg_args
        return int((256000 / 8) * seconds)
    return int((320000 / 8) * seconds)


def get_output_format_key(fmt: AudioFormat) -> str:
    """
    Get a stable key representing the output encoding parameters.

    :param fmt: The output audio format.
    """
    return f"{fmt.content_type.value}_{fmt.sample_rate}_{fmt.bit_depth}_{fmt.channels}"


CONTENT_LENGTH_CACHE_CATEGORY = 50
CONTENT_LENGTH_CACHE_PROVIDER = "audio"
CONTENT_LENGTH_CACHE_EXPIRATION = 365 * 86400  # 1 year


async def get_content_length(
    mass: MusicAssistant,
    uri: str,
    output_format: AudioFormat,
    seconds: float,
) -> int:
    """
    Get the estimated encoded size, using cached actual measurement when available.

    After a track has been fully streamed, its actual content size and duration
    are cached. On subsequent plays this gives a near-exact content_length:
    - Exact when the requested duration matches the cached duration.
    - Very accurate when the duration differs (derived bytes-per-second).

    Falls back to the static estimate from calculate_content_length() if no cache entry exists.

    :param mass: The MusicAssistant instance (for cache access).
    :param uri: The media URI (e.g. "qobuz://track/12345").
    :param output_format: The output audio format.
    :param seconds: Duration in seconds to estimate.
    """
    cache_key = f"{uri}/{get_output_format_key(output_format)}"
    cached: dict[str, float] | None = await mass.cache.get(
        cache_key,
        provider=CONTENT_LENGTH_CACHE_PROVIDER,
        category=CONTENT_LENGTH_CACHE_CATEGORY,
    )
    if cached is not None:
        cached_size = cached["size"]
        cached_duration = cached["duration"]
        if abs(seconds - cached_duration) < 1:
            # same duration: return the exact cached size
            return int(cached_size)
        # different duration: derive bytes-per-second from the cached measurement
        return int((cached_size / cached_duration) * seconds)
    return calculate_content_length(output_format, seconds)


async def store_content_length_in_cache(
    mass: MusicAssistant,
    uri: str,
    output_format: AudioFormat,
    content_size: int,
    seconds_streamed: float,
) -> None:
    """
    Store the actual content size after a track has been fully streamed.

    :param mass: The MusicAssistant instance (for cache access).
    :param uri: The media URI (e.g. "qobuz://track/12345").
    :param output_format: The output audio format used for encoding.
    :param content_size: Total encoded bytes sent to the player.
    :param seconds_streamed: Duration of audio streamed in seconds.
    """
    if seconds_streamed < 10 or content_size < 1000:
        return
    cache_key = f"{uri}/{get_output_format_key(output_format)}"
    await mass.cache.set(
        cache_key,
        {"size": content_size, "duration": seconds_streamed},
        expiration=CONTENT_LENGTH_CACHE_EXPIRATION,
        provider=CONTENT_LENGTH_CACHE_PROVIDER,
        category=CONTENT_LENGTH_CACHE_CATEGORY,
        persistent=True,
    )


def get_bit_rate(fmt: AudioFormat) -> int:
    """Get the (estimated) bit rate for a given AudioFormat, if known."""
    if fmt.bit_rate:
        return int(fmt.bit_rate / 1000) if fmt.bit_rate >= 10000 else fmt.bit_rate
    return int((calculate_content_length(fmt, seconds=1) / 1000) * 8)


def is_grouping_preventing_dsp(player: Player) -> bool:
    """
    Check if grouping is preventing DSP from being applied to this leader/PlayerGroup.

    If this returns True, no DSP should be applied to the player.
    This function will not check if the Player is in a group, the caller should do that first.
    """
    # We require the caller to handle non-leader cases themselves since player.state.synced_to
    # can be unreliable in some edge cases
    multi_device_dsp_supported = PlayerFeature.MULTI_DEVICE_DSP in player.state.supported_features
    child_count = len(player.state.group_members) if player.state.group_members else 0

    is_multiple_devices: bool
    if player.provider.domain == "player_group":
        # PlayerGroups have no leader, so having a child count of 1 means
        # the group actually contains only a single player.
        is_multiple_devices = child_count > 1
    elif player.state.type == PlayerType.GROUP:
        # This is an group player external to Music Assistant.
        is_multiple_devices = True
    else:
        is_multiple_devices = child_count > 0
    return is_multiple_devices and not multi_device_dsp_supported


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


def get_normalization_mode(
    preference: VolumeNormalizationMode,
    volume_normalization_enabled: bool,
    streamdetails: StreamDetails,
) -> VolumeNormalizationMode:
    """
    Get the volume normalization mode for a given queue and stream.

    :param preference: The configured normalization preference for the stream's media type
        (tracks or radio), from the streams core config.
    :param volume_normalization_enabled: Whether normalization is enabled for the queue, already
        resolved from the per-queue setting and its global (queue controller) fallback.
    :param streamdetails: The stream to evaluate.
    """
    if not volume_normalization_enabled:
        # disabled for this queue
        return VolumeNormalizationMode.DISABLED
    if streamdetails.media_type == MediaType.AUDIO_SOURCE:
        # live/realtime: upstream producer owns loudness, no measurement to converge on
        return VolumeNormalizationMode.DISABLED
    if streamdetails.target_loudness is None:
        # no target loudness set, disable normalization
        return VolumeNormalizationMode.DISABLED

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
