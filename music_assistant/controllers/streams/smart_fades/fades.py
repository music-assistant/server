"""Smart Fades - Audio fade implementations."""

from __future__ import annotations

import asyncio
import logging
import os
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from contextlib import suppress
from typing import TYPE_CHECKING

from music_assistant.constants import VERBOSE_LOG_LEVEL
from music_assistant.controllers.streams.smart_fades.filters import (
    Filter,
    StreamingCrossfadeFilter,
)
from music_assistant.controllers.streams.smart_fades.helpers import SMART_CROSSFADE_DURATION
from music_assistant.controllers.streams.smart_fades.models import (
    CrossfadeTimingInfo,
    SmartFadeNotApplicable,
    TransitionPlan,
)
from music_assistant.controllers.streams.smart_fades.planner import SmartCrossFadePlanner
from music_assistant.controllers.streams.smart_fades.renderer import TransitionRenderer
from music_assistant.helpers.audio import iter_pcm_slices
from music_assistant.helpers.ffmpeg import get_ffmpeg_channel_args
from music_assistant.helpers.process import AsyncProcess

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


def _close_if_open(*fds: int) -> None:
    """Close the given fds, ignoring ones already handed off (marked -1)."""
    for fd in fds:
        if fd != -1:
            os.close(fd)


def _feed_pipe_blocking(write_fd: int, payload: bytes) -> None:
    """
    Write a payload into a pipe fd with plain blocking writes, then close it.

    Blocking on purpose (run in a thread): the pipe applies the backpressure,
    and a consumer that went away surfaces as a broken pipe, which simply ends
    the feed — the consumer's own exit status tells the story.

    :param write_fd: Write end of the pipe; closed when done, whatever happens.
    :param payload: The bytes to deliver.
    """
    try:
        view = memoryview(payload)
        while view:
            written = os.write(write_fd, view[: 1024 * 1024])
            view = view[written:]
    except BrokenPipeError:
        # ffmpeg stopped reading (its filter took all it needed, or it exited)
        pass
    finally:
        os.close(write_fd)


async def _feed_ffmpeg_stdin(
    proc: AsyncProcess, fade_in_part: bytes | AsyncGenerator[bytes]
) -> None:
    """
    Write the incoming track's head to the mixer, always ending with an EOF.

    :param proc: The mixer process to feed.
    :param fade_in_part: Raw PCM bytes, or a stream delivering them.
    """
    try:
        if isinstance(fade_in_part, bytes):
            await proc.write(fade_in_part)
        else:
            async for fade_chunk in fade_in_part:
                await proc.write(fade_chunk)
    finally:
        # a feed that stops without an EOF leaves ffmpeg waiting for input
        # while its consumer waits for output
        await proc.write_eof()


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
        # The fade-out side goes in through its own pipe: ffmpeg takes any number
        # of pipe:<fd> inputs, so no temp file has to touch the disk. The pipe far
        # exceeds the kernel buffer, so it is fed alongside the stdin feeder below.
        fadeout_read_fd, fadeout_write_fd = os.pipe()

        self.logger.debug(
            "Applying smartfade: %s",
            self,
        )
        args = self._mix_ffmpeg_args(pcm_format, fadeout_read_fd)
        self.logger.log(VERBOSE_LOG_LEVEL, "FFmpeg command args: %s", " ".join(args))

        got_output = False
        stderr_lines: list[str] = []
        try:
            proc = AsyncProcess(
                args,
                stdin=True,
                stdout=True,
                stderr=True,
                name="smartfade",
                pass_fds=(fadeout_read_fd,),
            )
            async with proc:
                # the child holds its own copy of the read end now
                os.close(fadeout_read_fd)
                fadeout_read_fd = -1

                async def _drain_stderr() -> None:
                    """Read stderr to prevent pipe deadlock."""
                    async for line in proc.iter_stderr():
                        stderr_lines.append(line)

                fadeout_task = asyncio.create_task(
                    asyncio.to_thread(_feed_pipe_blocking, fadeout_write_fd, fade_out_part)
                )
                fadeout_write_fd = -1  # the feeder owns and closes it now
                feed_task = asyncio.create_task(_feed_ffmpeg_stdin(proc, fade_in_part))
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
                    # Bounded wait: on consumer abort a paused-but-alive ffmpeg can
                    # leave the writer blocked on a full pipe until proc.close()
                    # (in __aexit__, after this finally) breaks it — the orphaned
                    # thread then ends on its own, a completed write closed already.
                    with suppress(TimeoutError, asyncio.CancelledError):
                        await asyncio.wait_for(fadeout_task, timeout=2)
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
            # close whichever pipe ends this coroutine still owns (spawn failures)
            _close_if_open(fadeout_read_fd, fadeout_write_fd)

    def __repr__(self) -> str:
        """Return string representation of SmartFade showing the filter chain."""
        if not self.filters:
            return f"<{self.__class__.__name__}: 0 filters>"

        chain = " → ".join(repr(f) for f in self.filters)
        return f"<{self.__class__.__name__}: {len(self.filters)} filters> {chain}"

    def _mix_ffmpeg_args(self, pcm_format: AudioFormat, fadeout_read_fd: int) -> list[str]:
        """
        Build the mix's ffmpeg argv: fade-out on its own pipe, fade-in on stdin.

        Both inputs are fully specified raw PCM, so the demuxer's probe buffer is
        disabled — it would otherwise swallow seconds of a streamed fade-in
        before the filter graph produces its first frame.

        :param pcm_format: Audio format of both inputs and the output.
        :param fadeout_read_fd: Read end of the fade-out pipe (passed to the child).
        """
        input_format = [
            "-probesize",
            "32",
            "-analyzeduration",
            "0",
            "-acodec",
            pcm_format.content_type.name.lower(),  # e.g., "pcm_f32le" not just "f32le"
            *get_ffmpeg_channel_args(pcm_format),
            "-ar",
            str(pcm_format.sample_rate),
            "-f",
            pcm_format.content_type.value,
        ]
        return [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            *input_format,
            "-i",
            f"pipe:{fadeout_read_fd}",
            *input_format,
            "-i",
            "-",
            "-filter_complex",
            ";".join(self._get_ffmpeg_filters()),
            # output format matches the input codec format
            "-acodec",
            pcm_format.content_type.name.lower(),
            *get_ffmpeg_channel_args(pcm_format),
            "-ar",
            str(pcm_format.sample_rate),
            "-f",
            pcm_format.content_type.value,
            "-",
        ]

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
        # the streaming variant, so a fade-in that is still arriving (realtime
        # source) is blended and delivered as it comes in
        self.filters = [
            StreamingCrossfadeFilter(logger=self.logger, crossfade_samples=crossfade_samples),
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
        # Pre-crossfade: outgoing track minus the crossfaded portion. Emitted
        # before the incoming side is touched at all: with a streamed fade-in the
        # overlap is still arriving, and the player keeps playing this meanwhile.
        split = len(fade_out_part) - crossfade_size
        pre_crossfade = fade_out_part[:split]
        adjusted_fade_out_part = fade_out_part[split:]
        for pcm_slice in iter_pcm_slices(pre_crossfade, pcm_format, 1000):
            yield pcm_slice

        if isinstance(fade_in_part, bytes):
            async for chunk in super().apply(
                adjusted_fade_out_part, fade_in_part[:crossfade_size], pcm_format
            ):
                yield chunk
            for pcm_slice in iter_pcm_slices(fade_in_part[crossfade_size:], pcm_format, 1000):
                yield pcm_slice
            return

        # Generator fade-in: hand exactly the overlap to the (streaming) blend as
        # it arrives; whatever the last chunk carried beyond it opens the post part
        overshoot = bytearray()

        async def _overlap_stream() -> AsyncGenerator[bytes]:
            taken = 0
            async for chunk in fade_in_part:
                remaining = crossfade_size - taken
                if len(chunk) >= remaining:
                    taken += remaining
                    overshoot.extend(chunk[remaining:])
                    yield chunk[:remaining]
                    return
                taken += len(chunk)
                yield chunk

        async for chunk in super().apply(adjusted_fade_out_part, _overlap_stream(), pcm_format):
            yield chunk
        if overshoot:
            for pcm_slice in iter_pcm_slices(bytes(overshoot), pcm_format, 1000):
                yield pcm_slice
        async for remaining_chunk in fade_in_part:
            for pcm_slice in iter_pcm_slices(remaining_chunk, pcm_format, 1000):
                yield pcm_slice
