"""FFMpeg related helpers."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque
from collections.abc import AsyncGenerator, Sequence
from contextlib import suppress
from copy import copy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from music_assistant_models.enums import ContentType
from music_assistant_models.errors import AudioError
from music_assistant_models.helpers import get_global_cache_value, set_global_cache_values

from music_assistant.constants import VERBOSE_LOG_LEVEL

from .dsp import ComplexFilter
from .process import AsyncProcess, check_output
from .util import close_async_generator

if TYPE_CHECKING:
    from music_assistant_models.media_items import AudioFormat

LOGGER = logging.getLogger("ffmpeg")
MINIMAL_FFMPEG_VERSION = 6
CACHE_ATTR_LIBSOXR_PRESENT: Final[str] = "libsoxr_present"
CACHE_ATTR_FFMPEG_VERSION: Final[str] = "ffmpeg_version"
DEFAULT_MP3_BIT_RATE: Final[int] = 320

# Regex patterns to extract audio format details from ffmpeg's stderr output.
# Examples of the lines we parse:
#   Stream #0:0: Audio: mp3, 44100 Hz, stereo, fltp, 320 kb/s
#   Stream #0:0(eng): Audio: aac (LC) (mp4a / 0x6134706D), 44100 Hz, stereo, fltp, 254 kb/s
#   Stream #0:0: Audio: flac, 96000 Hz, stereo, s32 (24 bit)
#   Duration: 00:03:25.78, start: 0.000000, bitrate: 320 kb/s
_FFMPEG_SAMPLE_RATE_RE: Final = re.compile(r"(\d+) Hz")
_FFMPEG_BIT_RATE_RE: Final = re.compile(r"(\d+) kb/s")
_FFMPEG_EXPLICIT_BIT_DEPTH_RE: Final = re.compile(r"\((\d+) bit\)")
_FFMPEG_SAMPLE_FMT_RE: Final = re.compile(r"\b(u8p?|s16p?|s24p?|s32p?|fltp?|dblp?)\b")
_FFMPEG_DURATION_RE: Final = re.compile(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)")

# Mapping from ffmpeg sample format token to bit depth.
# Note: planar variants (suffix 'p') describe memory layout only.
# Floating point formats (flt/fltp/dbl/dblp) are typically the decoder's internal
# representation for lossy codecs and do not reflect source bit depth, so the
# caller decides whether to apply them based on the codec.
_SAMPLE_FMT_BIT_DEPTH: Final[dict[str, int]] = {
    "u8": 8,
    "u8p": 8,
    "s16": 16,
    "s16p": 16,
    "s24": 24,
    "s24p": 24,
    "s32": 32,
    "s32p": 32,
    "flt": 32,
    "fltp": 32,
    "dbl": 64,
    "dblp": 64,
}


@dataclass
class FFMpegStreamInfo:
    """Audio format details parsed from an ffmpeg 'Stream #' log line."""

    codec: ContentType
    sample_rate: int | None = None
    bit_depth: int | None = None
    bit_rate: int | None = None


class FFMpeg(AsyncProcess):
    """FFMpeg wrapped as AsyncProcess."""

    def __init__(
        self,
        audio_input: AsyncGenerator[bytes] | str | int,
        input_format: AudioFormat,
        output_format: AudioFormat,
        filter_params: Sequence[str | ComplexFilter] | None = None,
        extra_args: list[str] | None = None,
        extra_input_args: list[str] | None = None,
        extra_output_args: list[str] | None = None,
        audio_output: str | int = "-",
        collect_log_history: bool = False,
        loglevel: str = "info",
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
            extra_output_args=extra_output_args or [],
            loglevel=loglevel,
        )
        self.audio_input = audio_input
        self.input_format = input_format
        self.collect_log_history = collect_log_history
        self.log_history: deque[str] = deque(maxlen=100)
        self.concat_error = False  # switch to True if concat demuxer fails on MultiPartFiles
        # Audio format details for the input and output stream as detected from ffmpeg's
        # own stderr probe output. input_stream_info is also mirrored onto self.input_format
        # so callers that share the AudioFormat (e.g. streamdetails) pick up the corrected
        # values; output_stream_info is informational (useful for logging / future UI use).
        self.input_stream_info: FFMpegStreamInfo | None = None
        self.output_stream_info: FFMpegStreamInfo | None = None
        # Source duration in (whole) seconds as detected from the ffmpeg input log line,
        # or None if not yet parsed / not reported (e.g. live radio streams).
        self.parsed_duration: int | None = None
        self._stdin_feeder_task: asyncio.Task[None] | None = None
        self._stderr_reader_task: asyncio.Task[None] | None = None
        # holds the detached abort-on-corrupt-stream task from _log_reader_task so it
        # isn't garbage collected mid-flight; not otherwise awaited
        self._abort_task: asyncio.Task[None] | None = None
        # ffmpeg emits 'Input #N, ...' and 'Output #N, ...' headers before each block of
        # 'Stream #' lines; we track which block the next stream line belongs to.
        # Defaults to "input" so a stray Stream # line before any header still routes there.
        self._current_log_section: str = "input"
        stdin: bool | int
        if audio_input == "-" or isinstance(audio_input, AsyncGenerator):
            stdin = True
        else:
            stdin = audio_input if isinstance(audio_input, int) else False
        stdout = audio_output if isinstance(audio_output, int) else bool(audio_output == "-")
        super().__init__(
            ffmpeg_args,
            stdin=stdin,
            stdout=stdout,
            stderr=True,
        )
        self.logger = LOGGER

    async def start(self) -> None:
        """Perform Async init of process."""
        await super().start()
        if self.proc:
            self.logger = LOGGER.getChild(str(self.proc.pid))
        clean_args = []
        for arg in self._args[1:]:
            if arg.startswith("http"):
                clean_args.append("<URL>")
            elif "/" in arg and "." in arg:
                clean_args.append("<FILE>")
            elif arg.startswith("data:application/"):
                clean_args.append("<DATA>")
            else:
                clean_args.append(arg)
        args_str = " ".join(clean_args)
        self.logger.log(VERBOSE_LOG_LEVEL, "started with args: %s", args_str)
        self._stderr_reader_task = asyncio.create_task(self._log_reader_task())
        if isinstance(self.audio_input, AsyncGenerator):
            self._stdin_feeder_task = asyncio.create_task(self._feed_stdin())

    async def communicate(
        self,
        input: bytes | None = None,  # noqa: A002
        timeout: float | None = None,
    ) -> tuple[bytes, bytes]:
        """Override communicate to avoid blocking."""
        if self._stdin_feeder_task:
            if not self._stdin_feeder_task.done():
                self._stdin_feeder_task.cancel()
            # Always await the task to consume any exception and prevent
            # "Task exception was never retrieved" errors.
            try:
                await self._stdin_feeder_task
            except asyncio.CancelledError:
                pass  # Expected when we cancel the task
            except Exception as err:
                # Log unexpected exceptions from the stdin feeder before suppressing
                # The audio source may have failed, and we need visibility into this
                self.logger.warning(
                    "FFMpeg stdin feeder task ended with error: %s",
                    err,
                )
        if self._stderr_reader_task:
            if not self._stderr_reader_task.done():
                self._stderr_reader_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await self._stderr_reader_task
        return await super().communicate(input, timeout)

    async def _log_reader_task(self) -> None:
        """Read ffmpeg log from stderr."""
        decode_errors = 0
        decode_errors_reported = False
        async for line in self.iter_stderr():
            if self.collect_log_history:
                self.log_history.append(line)
            # ffmpeg logging can be quite verbose, so we only log critical errors
            # unless verbose logging is enabled
            if "critical" in line:
                self.logger.error(line)
            elif self.logger.isEnabledFor(VERBOSE_LOG_LEVEL):
                self.logger.log(VERBOSE_LOG_LEVEL, line)

            if "Invalid data found when processing input" in line:
                decode_errors += 1
            if decode_errors >= 50 and not decode_errors_reported:
                # stream is too corrupted to bother decoding further: report once (instead
                # of promoting every remaining line to ERROR) and abort. close() awaits
                # this very stderr reader task, and a task awaiting itself raises
                # RuntimeError, so the abort must run as a detached task rather than be
                # awaited here.
                decode_errors_reported = True
                self.logger.error(
                    "Excessive decode errors (%d+) for this stream; aborting", decode_errors
                )
                self._abort_task = asyncio.create_task(self.close())

            # Log reconnection events for radio streams
            if "Opening" in line or "Reconnect" in line or "reconnect" in line:
                self.logger.debug("FFmpeg: %s", line)

            if "Error during demuxing" in line:
                # this can occur if using the concat demuxer for multipart files
                # and should raise an exception to prevent false progress logging
                self.concat_error = True

            # Track which ffmpeg block we're currently parsing so the next 'Stream #'
            # audio line is routed to the correct slot (input vs output).
            if line.startswith("Input #"):
                self._current_log_section = "input"
            elif line.startswith("Output #"):
                self._current_log_section = "output"

            # Capture the first audio stream line per section. Provider-supplied input
            # details are often incomplete (e.g. defaults to 44.1/16) or missing for
            # lossy codecs, so we mirror the parsed input values onto input_format too.
            if self._current_log_section == "input" and self.input_stream_info is None:
                if stream_info := parse_ffmpeg_stream_info(line):
                    self.input_stream_info = stream_info
                    self._log_stream_info("input", stream_info)
                    self._apply_input_stream_info(stream_info)
            elif self._current_log_section == "output" and self.output_stream_info is None:
                if stream_info := parse_ffmpeg_stream_info(line):
                    self.output_stream_info = stream_info
                    self._log_stream_info("output", stream_info)

            # Source duration is reported separately from the stream info. Useful when
            # the provider didn't supply one (some podcast feeds report total_time=0).
            if self.parsed_duration is None:
                duration = parse_ffmpeg_duration(line)
                if duration is not None:
                    self.parsed_duration = duration
                    self.logger.debug("Detected input duration: %s seconds", duration)
            del line

    async def _feed_stdin(self) -> None:
        """Feed stdin with audio chunks from an AsyncGenerator."""
        assert not isinstance(self.audio_input, str | int)
        generator_exhausted = False
        cancelled = False
        status = "running"
        chunk_count = 0
        self.logger.log(VERBOSE_LOG_LEVEL, "Start reading audio data from source...")
        try:
            start = time.time()
            async for chunk in self.audio_input:
                chunk_count += 1
                if self.closed:
                    return
                await self.write(chunk)
            generator_exhausted = True
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        except Exception:
            status = "aborted with error"
            raise
        finally:
            LOGGER.log(
                VERBOSE_LOG_LEVEL,
                "fill_buffer_task: %s (%s chunks received) in in %.2fs",
                status,
                chunk_count,
                time.time() - start,
            )
            if not cancelled:
                await self.write_eof()
            # we need to ensure that we close the async generator
            # if we get cancelled otherwise it keeps lingering forever
            if not generator_exhausted:
                await close_async_generator(self.audio_input)

    def _apply_input_stream_info(self, info: FFMpegStreamInfo) -> None:
        """Mirror values from a parsed ffmpeg input stream line onto self.input_format."""
        # content_type is the container format; only fill it in if the provider didn't
        # specify one. codec_type is the audio codec ffmpeg detected; only override
        # if we actually parsed a known codec (don't clobber a provider value with UNKNOWN).
        if info.codec != ContentType.UNKNOWN:
            if self.input_format.content_type == ContentType.UNKNOWN:
                self.input_format.content_type = info.codec
            self.input_format.codec_type = info.codec
        if info.sample_rate:
            self.input_format.sample_rate = info.sample_rate
        if info.bit_depth:
            self.input_format.bit_depth = info.bit_depth
        if info.bit_rate:
            self.input_format.bit_rate = info.bit_rate

    def _log_stream_info(self, label: str, info: FFMpegStreamInfo) -> None:
        """Log a parsed FFMpegStreamInfo object at debug level."""
        self.logger.debug(
            "Detected %s stream info: codec=%s sample_rate=%s bit_depth=%s bit_rate=%s kb/s",
            label,
            info.codec,
            info.sample_rate,
            info.bit_depth,
            info.bit_rate,
        )


def parse_ffmpeg_stream_info(line: str) -> FFMpegStreamInfo | None:
    """
    Extract audio format details from an ffmpeg 'Stream #X: Audio: ...' log line.

    :param line: A single ffmpeg stderr log line.
    :returns: FFMpegStreamInfo when the line describes an audio stream,
        otherwise None.
    """
    if not (line.startswith("Stream #") and ": Audio: " in line):
        return None

    # the codec name is the first token right after "Audio: ", stripping
    # any trailing profile annotation like "(LC)" or container suffix
    codec_part = line.split(": Audio: ", 1)[1].split(" ", 1)[0].split(",", maxsplit=1)[0]
    codec = ContentType.try_parse(codec_part)

    info = FFMpegStreamInfo(codec=codec)
    if match := _FFMPEG_SAMPLE_RATE_RE.search(line):
        info.sample_rate = int(match.group(1))
    if match := _FFMPEG_BIT_RATE_RE.search(line):
        info.bit_rate = int(match.group(1))
    # Bit depth: an explicit "(N bit)" annotation wins (this is how ffmpeg reports
    # 24-bit FLAC stored in an s32 sample format), otherwise infer from the sample
    # format token. Lossy codecs report the decoder's internal precision (typically
    # fltp), so we ignore the sample format token for those.
    if match := _FFMPEG_EXPLICIT_BIT_DEPTH_RE.search(line):
        info.bit_depth = int(match.group(1))
    elif codec.is_lossless() and (match := _FFMPEG_SAMPLE_FMT_RE.search(line)):
        info.bit_depth = _SAMPLE_FMT_BIT_DEPTH.get(match.group(1))

    return info


def parse_ffmpeg_duration(line: str) -> int | None:
    """
    Extract the source duration in seconds from an ffmpeg 'Duration: ...' log line.

    :param line: A single ffmpeg stderr log line.
    :returns: Duration in whole seconds, or None if the line does not contain
        a parseable duration (e.g. 'Duration: N/A' on live streams).
    """
    match = _FFMPEG_DURATION_RE.search(line)
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + int(float(seconds))


async def get_ffmpeg_stream(
    audio_input: AsyncGenerator[bytes] | str,
    input_format: AudioFormat,
    output_format: AudioFormat,
    filter_params: Sequence[str | ComplexFilter] | None = None,
    extra_args: list[str] | None = None,
    chunk_size: int | None = None,
    extra_input_args: list[str] | None = None,
    extra_output_args: list[str] | None = None,
) -> AsyncGenerator[bytes]:
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
        extra_output_args=extra_output_args,
        collect_log_history=True,
    ) as ffmpeg_proc:
        # read final chunks from stdout
        iterator = ffmpeg_proc.iter_chunked(chunk_size) if chunk_size else ffmpeg_proc.iter_any()
        async for chunk in iterator:
            yield chunk
        # reap the process before trusting returncode: a stream aborted mid-decode (e.g.
        # excessive decode errors) closes stdout early, which ends the loop above before
        # the OS process has actually exited, leaving returncode as None if checked directly
        with suppress(TimeoutError):
            await ffmpeg_proc.wait_with_timeout(5)
        if ffmpeg_proc.returncode not in (None, 0) or ffmpeg_proc.concat_error:
            # unclean exit of ffmpeg - raise error with log tail
            log_lines = -20 if ffmpeg_proc.concat_error else -5
            log_tail = "\n" + "\n".join(list(ffmpeg_proc.log_history)[log_lines:])
            raise AudioError(log_tail)


async def get_ffmpeg_overlay_stream(
    audio_input: AsyncGenerator[bytes],
    overlay_input: str,
    pcm_format: AudioFormat,
    overlay_volume: int = 100,
    chunk_size: int | None = None,
) -> AsyncGenerator[bytes]:
    """
    Mix a looping audio overlay into a PCM audio stream.

    The overlay is looped for the full duration of the main stream and the mixed
    output has the exact same PCM format and duration as the main input. If the
    overlay input fails mid-stream, the main audio continues unaffected.

    :param audio_input: The main audio stream (raw PCM in ``pcm_format``).
    :param overlay_input: File path or URL of the overlay audio.
    :param overlay_volume: Overlay loudness relative to the main audio in
        percent (100 = equally loud, max 200).
    :param pcm_format: PCM format of both the main input and the mixed output.
    :param chunk_size: Optional exact chunk size for the yielded audio.
    """
    # the overlay is passed as an extra (first) ffmpeg input, infinitely looped;
    # the main audio arrives on stdin. amix with duration=first follows the main
    # input's length, normalize=0 keeps the original levels (no averaging).
    overlay_input_args = []
    if overlay_input.startswith("http"):
        overlay_input_args += [
            "-reconnect",
            "1",
            "-reconnect_delay_max",
            "10",
            "-reconnect_streamed",
            "1",
        ]
    overlay_input_args += ["-stream_loop", "-1", "-i", overlay_input]
    channel_layout = "mono" if pcm_format.channels == 1 else "stereo"
    # silenceremove strips a near-silent intro from the overlay source (e.g. a soft
    # fade-in) so it becomes audible right away; it is a no-op for sources that
    # already start at full level. It runs before volume so detection is based on
    # the source's own levels rather than the scaled output.
    filter_complex = (
        f"[0:a]silenceremove=start_periods=1:start_threshold=-40dB,"
        f"volume={overlay_volume / 100},"
        f"aresample={pcm_format.sample_rate},"
        f"aformat=channel_layouts={channel_layout}[overlay];"
        "[1:a][overlay]amix=inputs=2:duration=first:normalize=0[mixed]"
    )
    async with FFMpeg(
        audio_input=audio_input,
        # The overlay is input 0, so ffmpeg probes it before the PCM input and
        # mutates input_format with its metadata. Keep that mutation local.
        input_format=copy(pcm_format),
        output_format=pcm_format,
        extra_input_args=overlay_input_args,
        extra_output_args=["-filter_complex", filter_complex, "-map", "[mixed]"],
        collect_log_history=True,
    ) as ffmpeg_proc:
        iterator = ffmpeg_proc.iter_chunked(chunk_size) if chunk_size else ffmpeg_proc.iter_any()
        async for chunk in iterator:
            yield chunk
        # reap the process before trusting returncode: a stream aborted mid-decode (e.g.
        # excessive decode errors) closes stdout early, which ends the loop above before
        # the OS process has actually exited, leaving returncode as None if checked directly
        with suppress(TimeoutError):
            await ffmpeg_proc.wait_with_timeout(5)
        if ffmpeg_proc.returncode not in (None, 0):
            # unclean exit of ffmpeg - raise error with log tail
            log_tail = "\n" + "\n".join(list(ffmpeg_proc.log_history)[-5:])
            raise AudioError(log_tail)


def get_ffmpeg_resample_filter(
    input_format: AudioFormat,
    output_format: AudioFormat,
    filter_params: Sequence[str | ComplexFilter],
) -> str | None:
    """
    Return the resampling and dithering filter required for a format conversion.

    :param input_format: Format entering FFmpeg.
    :param output_format: Requested FFmpeg output format.
    :param filter_params: Filters that run before resampling.
    """
    if input_format.sample_rate == output_format.sample_rate and not (
        input_format.bit_depth > 16 and output_format.bit_depth == 16
    ):
        return None
    libsoxr_support = get_global_cache_value(CACHE_ATTR_LIBSOXR_PRESENT)
    # loudnorm and libsoxr cannot be combined due to https://trac.ffmpeg.org/ticket/11323
    if libsoxr_support and not any(
        "loudnorm" in value for value in filter_params if isinstance(value, str)
    ):
        resample_filter = "aresample=resampler=soxr:precision=30"
    else:
        resample_filter = "aresample=resampler=swr"
    if input_format.sample_rate != output_format.sample_rate:
        resample_filter += f":osr={output_format.sample_rate}"
    if output_format.bit_depth == 16 and input_format.bit_depth > 16:
        resample_filter += ":osf=s16:dither_method=triangular_hp"
    return resample_filter


def get_ffmpeg_args(
    input_format: AudioFormat,
    output_format: AudioFormat,
    filter_params: Sequence[str | ComplexFilter],
    extra_args: list[str] | None = None,
    input_path: str = "-",
    output_path: str = "-",
    extra_input_args: list[str] | None = None,
    extra_output_args: list[str] | None = None,
    loglevel: str = "error",
) -> list[str]:
    """Collect all args to send to the ffmpeg process."""
    filter_params = list(filter_params)
    if extra_args is None:
        extra_args = []
    if extra_input_args is None:
        extra_input_args = []
    if extra_output_args is None:
        extra_output_args = []
    # generic args
    generic_args = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        loglevel,
        "-nostats",
        "-ignore_unknown",
        "-protocol_whitelist",
        "file,hls,http,https,tcp,tls,crypto,pipe,data,fd,rtp,udp,concat",
        "-probesize",
        "8096",
        "-analyzeduration",
        "500000",  # 0.5 seconds should be enough to detect the format
    ]
    # collect input args
    if "-f" in extra_input_args:
        # input format is already specified in the extra input args
        input_args = extra_input_args
    else:
        input_args = [*extra_input_args]
        if input_path.startswith("http"):
            # append reconnect options for direct stream from http
            input_args += [
                # Reconnect automatically when disconnected before EOF is hit.
                "-reconnect",
                "1",
                # Set the maximum delay in seconds after which to give up reconnecting.
                "-reconnect_delay_max",
                "10",
                # If set then even streamed/non seekable streams will be reconnected on errors.
                "-reconnect_streamed",
                "1",
                # Reconnect automatically in case of TCP/TLS errors during connect.
                "-reconnect_on_network_error",
                "0",
                # A comma separated list of HTTP status codes to reconnect on.
                # The list can include specific status codes (e.g. 503) or the strings 4xx / 5xx.
                "-reconnect_on_http_error",
                "5xx,429",
            ]
            if "-post_data" in extra_input_args:
                # ffmpeg does not include Range headers on POST reconnects, so byte-range
                # seeking via reconnect is not available. Mark the stream non-seekable so
                # demuxers do not attempt end-of-file probes (e.g. OGG duration detection)
                # that would trigger Range-less restarts from byte 0. MA-initiated seeks
                # still work via -ss decode-and-discard.
                input_args += ["-seekable", "0"]
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
        if input_format.codec_type != ContentType.UNKNOWN:
            input_args += ["-acodec", input_format.codec_type.name.lower()]

        # add input path at the end
        input_args += ["-i", input_path]

    # collect output args
    output_args = [
        "-ac",
        str(output_format.channels),
        "-channel_layout",
        "mono" if output_format.channels == 1 else "stereo",
    ]
    if output_path.upper() == "NULL":
        # devnull stream
        output_path = "-"
        output_args = ["-f", "null"]
    elif output_format.content_type.is_pcm():
        # use explicit format identifier for pcm formats
        output_args += [
            "-ar",
            str(output_format.sample_rate),
            "-acodec",
            output_format.content_type.name.lower(),
            "-f",
            output_format.content_type.value,
        ]
    elif output_format.content_type == ContentType.NUT:
        # passthrough-mode (for creating the cache) using NUT container
        output_args = [
            "-vn",
            "-dn",
            "-sn",
            "-acodec",
            "copy",
            "-f",
            "nut",
        ]
    elif output_format.content_type == ContentType.AAC:
        output_args = ["-f", "adts", "-c:a", "aac", "-b:a", "256k"]
    elif output_format.content_type == ContentType.MP3:
        output_args = ["-f", "mp3", "-b:a", f"{DEFAULT_MP3_BIT_RATE}k"]
    elif output_format.content_type == ContentType.WAV:
        pcm_format = ContentType.from_bit_depth(output_format.bit_depth)
        output_args = [
            "-ar",
            str(output_format.sample_rate),
            "-acodec",
            pcm_format.name.lower(),
            "-f",
            "wav",
        ]
    elif output_format.content_type == ContentType.FLAC:
        # use level 0 compression for fastest encoding
        sample_fmt = "s32" if output_format.bit_depth > 16 else "s16"
        output_args += [
            "-sample_fmt",
            sample_fmt,
            "-ar",
            str(output_format.sample_rate),
            "-f",
            "flac",
            "-compression_level",
            "0",
        ]
    else:
        raise RuntimeError("Invalid/unsupported output format specified")

    output_args += extra_output_args  # append the extra output args
    # append (final) output path at the end of the args
    output_args.append(output_path)

    # edge case: source file is not stereo - downmix to stereo
    if input_format.channels > 2 and output_format.channels == 2:
        filter_params = [
            "pan=stereo|FL=1.0*FL+0.707*FC+0.707*SL+0.707*LFE|FR=1.0*FR+0.707*FC+0.707*SR+0.707*LFE",
            *filter_params,
        ]

    if resample_filter := get_ffmpeg_resample_filter(
        input_format,
        output_format,
        filter_params,
    ):
        filter_params.append(resample_filter)

    if filter_params and "-filter_complex" not in extra_args:
        extra_args += _build_filtergraph_args(filter_params)

    return generic_args + input_args + extra_args + output_args


async def check_ffmpeg_version() -> None:
    """Check if ffmpeg is present (with libsoxr support)."""
    # check for FFmpeg presence
    try:
        returncode, output = await check_output("ffmpeg", "-version")
    except FileNotFoundError:
        raise AudioError(
            "FFmpeg binary is missing from system. "
            "Please install ffmpeg on your OS to enable playback."
        )
    if returncode != 0:
        err_msg = "Error determining FFmpeg version on your system."
        if returncode < 0:
            # error below 0 is often illegal instruction
            err_msg += " - Your CPU may be too old to run this version of FFmpeg."
        err_msg += f" - Additional info: {returncode} {output.decode().strip()}"
        raise AudioError(err_msg)
    # parse version number from output
    try:
        version = output.decode().split("ffmpeg version ")[1].split(" ")[0].split("-")[0]
    except IndexError:
        raise AudioError(
            "Error determining FFmpeg version on your system."
            f"Additional info: {returncode} {output.decode().strip()}"
        )
    libsoxr_support = "enable-libsoxr" in output.decode()
    # use globals as in-memory cache
    await set_global_cache_values(
        {CACHE_ATTR_LIBSOXR_PRESENT: libsoxr_support, CACHE_ATTR_FFMPEG_VERSION: version}
    )

    major_version = int("".join(char for char in version.split(".")[0] if not char.isalpha()))
    if major_version < MINIMAL_FFMPEG_VERSION:
        raise AudioError(
            f"FFmpeg version {version} is not supported. "
            f"Minimal version required is {MINIMAL_FFMPEG_VERSION}."
        )

    LOGGER.info(
        "Detected ffmpeg version %s %s",
        version,
        "with libsoxr support" if libsoxr_support else "",
    )


def _build_filtergraph_args(filter_params: list[str | ComplexFilter]) -> list[str]:
    """
    Render a DSP filter chain to FFmpeg command-line arguments.

    :param filter_params: Ordered chain of plain filter strings and/or complex
        fragments that need extra source inputs.
    """
    if not any(isinstance(item, ComplexFilter) for item in filter_params):
        simple = [item for item in filter_params if isinstance(item, str) and item]
        return ["-af", ",".join(simple)] if simple else []

    parts: list[str] = []
    pending: list[str] = []
    current = "0:a"
    counter = 0

    def next_label() -> str:
        nonlocal counter
        counter += 1
        return f"dsp{counter}"

    def flush_pending() -> None:
        nonlocal current
        if not pending:
            return
        label = next_label()
        parts.append(f"[{current}]{','.join(pending)}[{label}]")
        current = label
        pending.clear()

    for item in filter_params:
        if isinstance(item, str):
            if item:
                pending.append(item)
            continue
        # a complex fragment closes the current simple run, pulls its own source
        # inputs into the graph, then consumes the main pad plus those sources
        flush_pending()
        source_labels: list[str] = []
        for source in item.sources:
            label = next_label()
            parts.append(f"{source}[{label}]")
            source_labels.append(label)
        label = next_label()
        inputs = f"[{current}]" + "".join(f"[{sl}]" for sl in source_labels)
        parts.append(f"{inputs}{item.body}[{label}]")
        current = label
    flush_pending()

    return ["-filter_complex", ";".join(parts), "-map", f"[{current}]"]
