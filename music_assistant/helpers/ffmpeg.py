"""FFMpeg related helpers."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import AsyncGenerator
from contextlib import suppress
from typing import TYPE_CHECKING, Final

from music_assistant_models.enums import ContentType
from music_assistant_models.errors import AudioError
from music_assistant_models.helpers import get_global_cache_value, set_global_cache_values

from music_assistant.constants import VERBOSE_LOG_LEVEL

from .process import AsyncProcess, check_output
from .util import close_async_generator

if TYPE_CHECKING:
    from music_assistant_models.media_items import AudioFormat

LOGGER = logging.getLogger("ffmpeg")
MINIMAL_FFMPEG_VERSION = 6
CACHE_ATTR_LIBSOXR_PRESENT: Final[str] = "libsoxr_present"


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
            loglevel=loglevel,
        )
        self.audio_input = audio_input
        self.input_format = input_format
        self.collect_log_history = collect_log_history
        self.log_history: deque[str] = deque(maxlen=100)
        self._stdin_task: asyncio.Task | None = None
        self._logger_task: asyncio.Task | None = None
        self._input_codec_parsed = False
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
        self._logger_task = asyncio.create_task(self._log_reader_task())
        if isinstance(self.audio_input, AsyncGenerator):
            self._stdin_task = asyncio.create_task(self._feed_stdin())

    async def communicate(
        self,
        input: bytes | None = None,  # noqa: A002
        timeout: float | None = None,
    ) -> tuple[bytes, bytes]:
        """Override communicate to avoid blocking."""
        if self._stdin_task and not self._stdin_task.done():
            self._stdin_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._stdin_task
        if self._logger_task and not self._logger_task.done():
            self._logger_task.cancel()
        return await super().communicate(input, timeout)

    async def close(self, send_signal: bool = True) -> None:
        """Close/terminate the process and wait for exit."""
        if self.closed:
            return
        if self._stdin_task and not self._stdin_task.done():
            self._stdin_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._stdin_task
        await super().close(send_signal)
        if self._logger_task and not self._logger_task.done():
            self._logger_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._logger_task

    async def _log_reader_task(self) -> None:
        """Read ffmpeg log from stderr."""
        decode_errors = 0
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
            if decode_errors >= 50:
                self.logger.error(line)
                await super().close(True)

            # if streamdetails contenttype is unknown, try parse it from the ffmpeg log
            if line.startswith("Stream #") and ": Audio: " in line:
                if not self._input_codec_parsed:
                    content_type_raw = line.split(": Audio: ")[1].split(" ")[0]
                    content_type_raw = content_type_raw.split(",")[0]
                    content_type = ContentType.try_parse(content_type_raw)
                    self.logger.log(
                        VERBOSE_LOG_LEVEL,
                        "Detected (input) content type: %s (%s)",
                        content_type,
                        content_type_raw,
                    )
                    if self.input_format.content_type == ContentType.UNKNOWN:
                        self.input_format.content_type = content_type
                    self.input_format.codec_type = content_type
                    self._input_codec_parsed = True
            del line

    async def _feed_stdin(self) -> None:
        """Feed stdin with audio chunks from an AsyncGenerator."""
        if TYPE_CHECKING:
            self.audio_input: AsyncGenerator[bytes, None]
        generator_exhausted = False
        cancelled = False
        try:
            start = time.time()
            self.logger.debug("Start reading audio data from source...")
            async for chunk in self.audio_input:
                if self.closed:
                    return
                await self.write(chunk)
            self.logger.debug("Audio data source exhausted in %.2fs", time.time() - start)
            generator_exhausted = True
        except Exception as err:
            cancelled = isinstance(err, asyncio.CancelledError)
            self.logger.error(
                "Stream error: %s",
                str(err) or err.__class__.__name__,
                exc_info=err if self.logger.isEnabledFor(logging.DEBUG) else None,
            )
            raise
        finally:
            if not cancelled:
                await self.write_eof()
            # we need to ensure that we close the async generator
            # if we get cancelled otherwise it keeps lingering forever
            if not generator_exhausted:
                await close_async_generator(self.audio_input)


async def get_ffmpeg_stream(
    audio_input: AsyncGenerator[bytes, None] | str,
    input_format: AudioFormat,
    output_format: AudioFormat,
    filter_params: list[str] | None = None,
    extra_args: list[str] | None = None,
    chunk_size: int | None = None,
    extra_input_args: list[str] | None = None,
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
        collect_log_history=True,
    ) as ffmpeg_proc:
        # read final chunks from stdout
        iterator = ffmpeg_proc.iter_chunked(chunk_size) if chunk_size else ffmpeg_proc.iter_any()
        async for chunk in iterator:
            yield chunk
        if ffmpeg_proc.returncode not in (None, 0):
            # dump the last 5 lines of the log in case of an unclean exit
            log_tail = "\n" + "\n".join(list(ffmpeg_proc.log_history)[-5:])
            ffmpeg_proc.logger.error(log_tail)


def get_ffmpeg_args(  # noqa: PLR0915
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
    if extra_input_args is None:
        extra_input_args = []
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
                "30",
                # If set then even streamed/non seekable streams will be reconnected on errors.
                "-reconnect_streamed",
                "1",
                # Reconnect automatically in case of TCP/TLS errors during connect.
                "-reconnect_on_network_error",
                "1",
                # A comma separated list of HTTP status codes to reconnect on.
                # The list can include specific status codes (e.g. 503) or the strings 4xx / 5xx.
                "-reconnect_on_http_error",
                "5xx,4xx",
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
        output_args = ["-f", "mp3", "-b:a", "320k"]
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

    # append (final) output path at the end of the args
    output_args.append(output_path)

    # edge case: source file is not stereo - downmix to stereo
    if input_format.channels > 2 and output_format.channels == 2:
        filter_params = [
            "pan=stereo|FL=1.0*FL+0.707*FC+0.707*SL+0.707*LFE|FR=1.0*FR+0.707*FC+0.707*SR+0.707*LFE",
            *filter_params,
        ]

    # determine if we need to do resampling (or dithering)
    if input_format.sample_rate != output_format.sample_rate or (
        input_format.bit_depth > 16 and output_format.bit_depth == 16
    ):
        libsoxr_support = get_global_cache_value(CACHE_ATTR_LIBSOXR_PRESENT)
        # prefer resampling with libsoxr due to its high quality
        # but skip if loudnorm filter is present, due to this bug:
        # https://trac.ffmpeg.org/ticket/11323
        loudnorm_present = any("loudnorm" in f for f in filter_params)
        if libsoxr_support and not loudnorm_present:
            resample_filter = "aresample=resampler=soxr:precision=30"
        else:
            resample_filter = "aresample=resampler=swr"

        # sample rate conversion
        if input_format.sample_rate != output_format.sample_rate:
            resample_filter += f":osr={output_format.sample_rate}"

        # bit depth conversion: apply dithering when going down to 16 bits
        # this is only needed when we need to back to 16 bits
        # when going from 32bits FP to 24 bits no dithering is needed
        if output_format.bit_depth == 16 and input_format.bit_depth > 16:
            resample_filter += ":osf=s16:dither_method=triangular_hp"

        filter_params.append(resample_filter)

    if filter_params and "-filter_complex" not in extra_args:
        extra_args += ["-af", ",".join(filter_params)]

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
    await set_global_cache_values({CACHE_ATTR_LIBSOXR_PRESENT: libsoxr_support})

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
