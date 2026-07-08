"""Smart Fades - Audio fade implementations."""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from contextlib import suppress
from typing import TYPE_CHECKING

import aiofiles
import shortuuid

from music_assistant.constants import VERBOSE_LOG_LEVEL
from music_assistant.controllers.streams.smart_fades.filters import CrossfadeFilter, Filter
from music_assistant.controllers.streams.smart_fades.helpers import SMART_CROSSFADE_DURATION
from music_assistant.controllers.streams.smart_fades.models import (
    CrossfadeTimingInfo,
    SmartFadeNotApplicable,
    TransitionPlan,
)
from music_assistant.controllers.streams.smart_fades.planner import SmartCrossFadePlanner
from music_assistant.controllers.streams.smart_fades.renderer import TransitionRenderer
from music_assistant.helpers.audio import iter_pcm_slices
from music_assistant.helpers.process import AsyncProcess
from music_assistant.helpers.util import remove_file

if TYPE_CHECKING:
    from music_assistant_models.media_items import AudioFormat

    from music_assistant.models.audio_analysis import AudioAnalysisData

__all__ = [
    "CrossfadeTimingInfo",
    "SmartCrossFade",
    "SmartFade",
    "SmartFadeNotApplicable",
    "StandardCrossFade",
]


class SmartFade(ABC):
    """Abstract base class for Smart Fades."""

    filters: list[Filter]
    timing_info: CrossfadeTimingInfo

    def __init__(self, logger: logging.Logger) -> None:
        """Initialize SmartFade base class."""
        self.filters = []
        self.logger = logger

    @abstractmethod
    def build(
        self,
        fade_out_bytes_len: int,
        fade_in_bytes_len: int,
        pcm_format: AudioFormat,
    ) -> None:
        """
        Build the filter chain and assign ``self.timing_info``.

        Must be called once before ``apply()``.

        :param fade_out_bytes_len: Length in bytes of the outgoing track's tail buffer.
        :param fade_in_bytes_len: Length in bytes of the incoming track's head buffer.
        :param pcm_format: Audio format of both input buffers.
        """
        ...

    async def apply(
        self,
        fade_out_part: bytes,
        fade_in_part: bytes | AsyncGenerator[bytes],
        pcm_format: AudioFormat,
    ) -> AsyncGenerator[bytes]:
        """
        Apply the smart fade, yielding PCM audio chunks as they become available.

        :param fade_out_part: Raw PCM bytes for the outgoing track's tail.
        :param fade_in_part: Raw PCM bytes or async generator for the incoming track's head.
        :param pcm_format: Audio format of both input parts and the output.
        """
        # Write the fade_out_part to a temporary file
        fadeout_filename = f"/tmp/{shortuuid.random(20)}.pcm"  # noqa: S108
        async with aiofiles.open(fadeout_filename, "wb") as outfile:
            await outfile.write(fade_out_part)

        args = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            # Input 1: fadeout part (as file)
            "-acodec",
            pcm_format.content_type.name.lower(),  # e.g., "pcm_f32le" not just "f32le"
            "-ac",
            str(pcm_format.channels),
            "-ar",
            str(pcm_format.sample_rate),
            "-channel_layout",
            "mono" if pcm_format.channels == 1 else "stereo",
            "-f",
            pcm_format.content_type.value,
            "-i",
            fadeout_filename,
            # Input 2: fade_in part (stdin)
            "-acodec",
            pcm_format.content_type.name.lower(),
            "-ac",
            str(pcm_format.channels),
            "-ar",
            str(pcm_format.sample_rate),
            "-channel_layout",
            "mono" if pcm_format.channels == 1 else "stereo",
            "-f",
            pcm_format.content_type.value,
            "-i",
            "-",
        ]
        smart_fade_filters = self._get_ffmpeg_filters()
        self.logger.debug(
            "Applying smartfade: %s",
            self,
        )
        args.extend(
            [
                "-filter_complex",
                ";".join(smart_fade_filters),
                # Output format specification - must match input codec format
                "-acodec",
                pcm_format.content_type.name.lower(),
                "-ac",
                str(pcm_format.channels),
                "-ar",
                str(pcm_format.sample_rate),
                "-channel_layout",
                "mono" if pcm_format.channels == 1 else "stereo",
                "-f",
                pcm_format.content_type.value,
                "-",
            ]
        )
        self.logger.log(VERBOSE_LOG_LEVEL, "FFmpeg command args: %s", " ".join(args))

        got_output = False
        stderr_lines: list[str] = []
        try:
            proc = AsyncProcess(args, stdin=True, stdout=True, stderr=True, name="smartfade")
            async with proc:

                async def _feed_stdin() -> None:
                    if isinstance(fade_in_part, bytes):
                        await proc.write(fade_in_part)
                    else:
                        async for fade_chunk in fade_in_part:
                            await proc.write(fade_chunk)
                    await proc.write_eof()

                async def _drain_stderr() -> None:
                    """Read stderr to prevent pipe deadlock."""
                    async for line in proc.iter_stderr():
                        stderr_lines.append(line)

                feed_task = asyncio.create_task(_feed_stdin())
                stderr_task = asyncio.create_task(_drain_stderr())
                try:
                    async for chunk in proc.iter_any():
                        got_output = True
                        yield chunk
                finally:
                    if not feed_task.done():
                        feed_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await feed_task
                    # Bounded wait on stderr_task so its output is still captured
                    # for error reporting on the happy/error paths, but we don't
                    # hang on consumer abort — ffmpeg is still alive then and
                    # stderr won't EOF until proc.close() closes stdin, which
                    # only runs via the async-with __aexit__ *after* this finally.
                    # wait_for cancels stderr_task on timeout so cleanup proceeds.
                    with suppress(TimeoutError, asyncio.CancelledError):
                        await asyncio.wait_for(stderr_task, timeout=2)

            if proc.returncode != 0:
                stderr_msg = "; ".join(stderr_lines) if stderr_lines else "(no stderr)"
                raise RuntimeError(f"Crossfade FFmpeg failed (rc={proc.returncode}): {stderr_msg}")
            if not got_output:
                msg = "Crossfade FFmpeg produced no output"
                if stderr_lines:
                    msg += f": {'; '.join(stderr_lines)}"
                raise RuntimeError(msg)
        finally:
            # Always cleanup temp file, even if ffmpeg fails
            await remove_file(fadeout_filename)

    def __repr__(self) -> str:
        """Return string representation of SmartFade showing the filter chain."""
        if not self.filters:
            return f"<{self.__class__.__name__}: 0 filters>"

        chain = " → ".join(repr(f) for f in self.filters)
        return f"<{self.__class__.__name__}: {len(self.filters)} filters> {chain}"

    def _get_ffmpeg_filters(
        self,
        input_fadein_label: str = "[1]",
        input_fadeout_label: str = "[0]",
    ) -> list[str]:
        """Get FFmpeg filters for smart fades."""
        if not self.filters:
            raise RuntimeError("SmartFade not built — call Mixer.build() first")
        filters = []
        _cur_fadein_label = input_fadein_label
        _cur_fadeout_label = input_fadeout_label
        for audio_filter in self.filters:
            filter_strings = audio_filter.apply(_cur_fadein_label, _cur_fadeout_label)
            filters.extend(filter_strings)
            _cur_fadein_label = f"[{audio_filter.output_fadein_label}]"
            _cur_fadeout_label = f"[{audio_filter.output_fadeout_label}]"
        return filters


class SmartCrossFade(SmartFade):
    """
    Smart fades class that implements a Smart Fade mode.

    Delegates the decision-making to a ``SmartCrossFadePlanner`` (pure, over the
    stored analysis) and the filter/timing construction to a ``TransitionRenderer``.
    Alternative transition strategies are siblings that swap in their own planner.
    """

    def __init__(
        self,
        logger: logging.Logger,
        fade_out_analysis: AudioAnalysisData,
        fade_in_analysis: AudioAnalysisData,
    ) -> None:
        """
        Initialize SmartCrossFade with analysis data.

        :param logger: Logger for debug output.
        :param fade_out_analysis: Analysis data for the outgoing track.
        :param fade_in_analysis: Analysis data for the incoming track.
        """
        super().__init__(logger)
        self.fade_out_analysis = fade_out_analysis
        self.fade_in_analysis = fade_in_analysis
        self.planner = SmartCrossFadePlanner(logger)
        self.renderer = TransitionRenderer(logger)
        self.plan: TransitionPlan | None = None
        # populated by build(); read by the timing/lyrics-sync tests
        self.effective_end: float = float(SMART_CROSSFADE_DURATION)
        self.tempo_steps: list[tuple[float, float]] = []

    def build(
        self,
        fade_out_bytes_len: int,
        fade_in_bytes_len: int,
        pcm_format: AudioFormat,
    ) -> None:
        """Plan the transition, then render its filter chain and ``timing_info``."""
        buffer_duration = min(
            float(SMART_CROSSFADE_DURATION),
            fade_out_bytes_len / pcm_format.pcm_sample_size,
        )
        self.plan = self.planner.plan(
            self.fade_out_analysis, self.fade_in_analysis, buffer_duration
        )
        self.filters, self.timing_info = self.renderer.render(
            self.plan, pcm_format, fade_in_bytes_len
        )
        # convenience copies for the timing/lyrics-sync tests
        self.effective_end = self.plan.fade_out_window
        self.tempo_steps = self.plan.tempo_plan.steps
        self.fade_out_beats = self.planner.outgoing.beats


class StandardCrossFade(SmartFade):
    """Standard crossfade class that implements a standard crossfade mode."""

    def __init__(
        self,
        logger: logging.Logger,
        crossfade_duration: float = 10.0,
        trailing_silence_bytes: int = 0,
    ) -> None:
        """
        Initialize StandardCrossFade.

        :param logger: Logger for debug output.
        :param crossfade_duration: Length of the crossfade overlap in seconds.
        :param trailing_silence_bytes: Trailing silence in the outgoing tail that
            ``apply()`` slices off before crossfading.
        """
        super().__init__(logger)
        self.crossfade_duration = crossfade_duration
        self.trailing_silence_bytes = trailing_silence_bytes
        self.crossfade_size: int = 0

    def build(
        self,
        fade_out_bytes_len: int,
        fade_in_bytes_len: int,
        pcm_format: AudioFormat,
    ) -> None:
        """Build the standard crossfade filter chain and assign ``self.timing_info``."""
        fade_out_seconds = fade_out_bytes_len / pcm_format.pcm_sample_size
        fade_in_seconds = fade_in_bytes_len / pcm_format.pcm_sample_size
        # clamp CF to fit shorter inputs (defensive — normally full buffers)
        effective_cf = min(self.crossfade_duration, fade_out_seconds, fade_in_seconds)
        # Quantize the overlap to a whole number of PCM frames and drive both the
        # byte slice (in apply) and the acrossfade length from this one integer.
        # apply slices the buffers on frame boundaries, so a fractional effective_cf
        # leaves the rendered buffer a fraction of a sample short of the acrossfade
        # duration — and acrossfade then silently produces no output at all.
        frame_size = (pcm_format.bit_depth // 8) * pcm_format.channels
        crossfade_bytes = int(pcm_format.pcm_sample_size * effective_cf)
        self.crossfade_size = crossfade_bytes // frame_size * frame_size
        crossfade_samples = self.crossfade_size // frame_size
        effective_cf = self.crossfade_size / pcm_format.pcm_sample_size
        self.timing_info = CrossfadeTimingInfo(
            pre_crossfade_duration=max(0.0, fade_out_seconds - effective_cf),
            crossfade_duration=effective_cf,
            fadein_trimmed_duration=0.0,
            post_crossfade_duration=max(0.0, fade_in_seconds - effective_cf),
        )
        self.filters = [
            CrossfadeFilter(logger=self.logger, crossfade_samples=crossfade_samples),
        ]

    async def apply(
        self,
        fade_out_part: bytes,
        fade_in_part: bytes | AsyncGenerator[bytes],
        pcm_format: AudioFormat,
    ) -> AsyncGenerator[bytes]:
        """
        Apply standard crossfade, yielding PCM audio chunks.

        Only the overlapping portions are crossfaded, not the full buffers.
        """
        # crossfade_size legitimately ends up 0 for a silent/tiny buffer, so guard on
        # the filter chain (set in build) to still fail fast on apply-before-build,
        # consistent with SmartFade._get_ffmpeg_filters()
        if not self.filters:
            raise RuntimeError("SmartFade not built — call Mixer.build() first")
        if self.trailing_silence_bytes:
            fade_out_part = fade_out_part[: len(fade_out_part) - self.trailing_silence_bytes]
        # frame-aligned overlap computed once in build, so it exactly matches the
        # acrossfade `ns=` length the filter was built with
        crossfade_size = self.crossfade_size
        if crossfade_size == 0:
            # nothing to blend — concatenate without spawning ffmpeg
            for pcm_slice in iter_pcm_slices(fade_out_part, pcm_format, 1000):
                yield pcm_slice
            if isinstance(fade_in_part, bytes):
                for pcm_slice in iter_pcm_slices(fade_in_part, pcm_format, 1000):
                    yield pcm_slice
            else:
                async for chunk in fade_in_part:
                    for pcm_slice in iter_pcm_slices(chunk, pcm_format, 1000):
                        yield pcm_slice
            return
        # Pre-crossfade: outgoing track minus the crossfaded portion
        split = len(fade_out_part) - crossfade_size
        pre_crossfade = fade_out_part[:split]
        adjusted_fade_out_part = fade_out_part[split:]

        # Collect only the crossfade portion from fade_in, keep the rest as a generator
        if isinstance(fade_in_part, bytes):
            adjusted_fade_in_part = fade_in_part[:crossfade_size]
            post_crossfade: bytes | AsyncGenerator[bytes] = fade_in_part[crossfade_size:]
        else:
            # read exactly crossfade_size bytes from the generator
            buf = bytearray()
            async for chunk in fade_in_part:
                buf.extend(chunk)
                if len(buf) >= crossfade_size:
                    break
            adjusted_fade_in_part = bytes(buf[:crossfade_size])
            # anything beyond crossfade_size plus the remaining generator is post_crossfade
            leftover = bytes(buf[crossfade_size:])

            async def _post_crossfade() -> AsyncGenerator[bytes]:
                if leftover:
                    for pcm_slice in iter_pcm_slices(leftover, pcm_format, 1000):
                        yield pcm_slice
                async for remaining_chunk in fade_in_part:
                    for pcm_slice in iter_pcm_slices(remaining_chunk, pcm_format, 1000):
                        yield pcm_slice

            post_crossfade = _post_crossfade()

        # Yield pre-crossfade, crossfaded section, and post-crossfade
        for pcm_slice in iter_pcm_slices(pre_crossfade, pcm_format, 1000):
            yield pcm_slice
        async for chunk in super().apply(adjusted_fade_out_part, adjusted_fade_in_part, pcm_format):
            yield chunk
        if isinstance(post_crossfade, bytes):
            for pcm_slice in iter_pcm_slices(post_crossfade, pcm_format, 1000):
                yield pcm_slice
        else:
            async for chunk in post_crossfade:
                yield chunk
