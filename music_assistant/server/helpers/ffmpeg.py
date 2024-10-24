"""FFMpeg related helpers."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

from music_assistant.common.helpers.global_cache import get_global_cache_value
from music_assistant.common.models.errors import AudioError
from music_assistant.common.models.media_items import AudioFormat, ContentType
from music_assistant.constants import VERBOSE_LOG_LEVEL

from .process import AsyncProcess
from .util import TimedAsyncGenerator, close_async_generator

LOGGER = logging.getLogger("ffmpeg")
MINIMAL_FFMPEG_VERSION = 6


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
            else:
                clean_args.append(arg)
        args_str = " ".join(clean_args)
        self.logger.log(VERBOSE_LOG_LEVEL, "started with args: %s", args_str)
        self._logger_task = asyncio.create_task(self._log_reader_task())
        if isinstance(self.audio_input, AsyncGenerator):
            self._stdin_task = asyncio.create_task(self._feed_stdin())

    async def close(self, send_signal: bool = True) -> None:
        """Close/terminate the process and wait for exit."""
        if self.closed:
            return
        if self._stdin_task and not self._stdin_task.done():
            self._stdin_task.cancel()
        await super().close(send_signal)

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
                    self.logger.debug(
                        "Detected (input) content type: %s (%s)", content_type, content_type_raw
                    )
                    self.input_format.content_type = content_type
            del line

    async def _feed_stdin(self) -> None:
        """Feed stdin with audio chunks from an AsyncGenerator."""
        if TYPE_CHECKING:
            self.audio_input: AsyncGenerator[bytes, None]
        generator_exhausted = False
        audio_received = False
        try:
            async for chunk in TimedAsyncGenerator(self.audio_input, 300):
                audio_received = True
                if self.proc and self.proc.returncode is not None:
                    raise AudioError("Parent process already exited")
                await self.write(chunk)
            generator_exhausted = True
            if not audio_received:
                raise AudioError("No audio data received from source")
        except Exception as err:
            if isinstance(err, asyncio.CancelledError):
                return
            self.logger.error(
                "Stream error: %s",
                str(err) or err.__class__.__name__,
                exc_info=err if self.logger.isEnabledFor(VERBOSE_LOG_LEVEL) else None,
            )
        finally:
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
    ) as ffmpeg_proc:
        # read final chunks from stdout
        iterator = ffmpeg_proc.iter_chunked(chunk_size) if chunk_size else ffmpeg_proc.iter_any()
        async for chunk in iterator:
            yield chunk


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
    if major_version < MINIMAL_FFMPEG_VERSION:
        msg = (
            f"FFmpeg version {version} is not supported. "
            f"Minimal version required is {MINIMAL_FFMPEG_VERSION}."
        )
        raise AudioError(msg)

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
        output_args = ["-f", "adts", "-c:a", "aac", "-b:a", "256k", output_path]
    elif output_format.content_type == ContentType.MP3:
        output_args = ["-f", "mp3", "-b:a", "320k", output_path]
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
        ]
        if output_format.output_format_str == "flac":
            # use level 0 compression for fastest encoding
            output_args += ["-compression_level", "0"]
        output_args += [output_path]

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
        if output_format.bit_depth == 16 and input_format.bit_depth > 16:
            resample_filter += ":osf=s16:dither_method=triangular_hp"

        filter_params.append(resample_filter)

    if filter_params and "-filter_complex" not in extra_args:
        extra_args += ["-af", ",".join(filter_params)]

    return generic_args + input_args + extra_args + output_args
