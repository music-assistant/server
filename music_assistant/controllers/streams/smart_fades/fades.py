"""Smart Fades - Audio fade implementations."""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING

import aiofiles
import numpy as np
import numpy.typing as npt
import shortuuid

from music_assistant.constants import VERBOSE_LOG_LEVEL
from music_assistant.controllers.streams.smart_fades.filters import (
    CrossfadeFilter,
    Filter,
    FrequencySweepFilter,
    GradualTimeStretchFilter,
    TrimFilter,
)
from music_assistant.controllers.streams.smart_fades.helpers import (
    SMART_CROSSFADE_DURATION,
    compute_gradual_tempo_steps,
    extrapolate_downbeats,
    generate_synthetic_timestamps,
)
from music_assistant.helpers.audio import (
    align_audio_to_frame_boundary,
    iter_pcm_slices,
    strip_silence,
)
from music_assistant.helpers.process import AsyncProcess
from music_assistant.helpers.util import remove_file
from music_assistant.models.audio_analysis import AudioAnalysisData

if TYPE_CHECKING:
    from music_assistant_models.media_items import AudioFormat


@dataclass(slots=True)
class CrossfadeTimingInfo:
    """Timing breakdown of a crossfade mix output: PRE | CF | POST."""

    pre_crossfade_duration: float = 0.0
    crossfade_duration: float = 0.0
    fadein_trimmed_duration: float = 0.0
    post_crossfade_duration: float = 0.0


class SmartFade(ABC):
    """Abstract base class for Smart Fades."""

    filters: list[Filter]
    timing_info: CrossfadeTimingInfo

    def __init__(self, logger: logging.Logger) -> None:
        """Initialize SmartFade base class."""
        self.filters = []
        self.logger = logger

    @abstractmethod
    def _build(
        self,
        fade_out_bytes_len: int,
        fade_in_bytes_len: int,
        pcm_format: AudioFormat,
    ) -> None:
        """Build the filter chain and assign ``self.timing_info``."""
        ...

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
                raise RuntimeError(
                    f"Smart crossfade FFmpeg failed (rc={proc.returncode}): {stderr_msg}"
                )
            if not got_output:
                msg = "Smart crossfade FFmpeg produced no output"
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


class SmartCrossFade(SmartFade):
    """Smart fades class that implements a Smart Fade mode."""

    # Only apply time stretching if BPM difference is < this %
    time_stretch_bpm_percentage_threshold: float = 5.0

    def __init__(
        self,
        logger: logging.Logger,
        fade_out_analysis: AudioAnalysisData,
        fade_in_analysis: AudioAnalysisData,
    ) -> None:
        """Initialize SmartFades with analysis data.

        :param logger: Logger for debug output.
        :param fade_out_analysis: Analysis data for the outgoing track.
        :param fade_in_analysis: Analysis data for the incoming track.
        """
        if (
            fade_out_analysis.bpm is None
            or fade_in_analysis.bpm is None
            or fade_out_analysis.beats is None
            or fade_in_analysis.beats is None
        ):
            raise ValueError("AudioAnalysisData must have bpm and beats set for smart crossfade")
        self.fade_out_analysis = fade_out_analysis
        self.fade_in_analysis = fade_in_analysis
        # Store validated non-optional fields for type narrowing
        self.fade_out_bpm: float = fade_out_analysis.bpm
        self.fade_in_bpm: float = fade_in_analysis.bpm
        self.fade_in_beats: npt.NDArray[np.float32] = fade_in_analysis.beats
        self.fade_in_downbeats: npt.NDArray[np.float32] = (
            fade_in_analysis.downbeats
            if fade_in_analysis.downbeats is not None
            else fade_in_analysis.beats
        )
        # Shift fade-out beats from full-track to buffer-local coordinates
        buffer_offset = max(0.0, (fade_out_analysis.duration or 0.0) - SMART_CROSSFADE_DURATION)
        self.fade_out_beats: npt.NDArray[np.float32] = fade_out_analysis.beats - buffer_offset
        self.fade_out_downbeats: npt.NDArray[np.float32] = (
            fade_out_analysis.downbeats - buffer_offset
            if fade_out_analysis.downbeats is not None
            else np.array([], dtype=np.float32)
        )
        super().__init__(logger)

    def _build(
        self,
        fade_out_bytes_len: int,
        fade_in_bytes_len: int,
        pcm_format: AudioFormat,
    ) -> None:
        """Build the smart fades filter chain and assign ``self.timing_info``."""
        self.timing_info = CrossfadeTimingInfo()
        # Calculate tempo factor for time stretching
        bpm_ratio = self.fade_in_bpm / self.fade_out_bpm
        bpm_diff_percent = abs(1.0 - bpm_ratio) * 100

        # Extrapolate downbeats for better bar calculation
        self.extrapolated_fadeout_downbeats = extrapolate_downbeats(
            self.fade_out_downbeats,
            bpm=self.fade_out_bpm,
        )

        # Additional verbose logging to debug rare failures
        self.logger.log(
            VERBOSE_LOG_LEVEL,
            "SmartCrossFade build: fade_out: %s, fade_in: %s",
            self.fade_out_analysis,
            self.fade_in_analysis,
        )

        # Calculate optimal crossfade bars that fit in available buffer
        crossfade_bars = self._calculate_optimal_crossfade_bars()

        # Calculate beat positions for the selected bar count
        fadein_start_pos = self._calculate_optimal_fade_timing(crossfade_bars)

        # Calculate initial crossfade duration (may be adjusted later for downbeat alignment)
        crossfade_duration = self._calculate_crossfade_duration(crossfade_bars=crossfade_bars)

        # Add gradual time stretch filter if needed
        is_stretched = (
            0.1 < bpm_diff_percent <= self.time_stretch_bpm_percentage_threshold
            and crossfade_bars > 4
        )
        if is_stretched:
            self._apply_gradual_time_stretch(bpm_ratio, bpm_diff_percent, crossfade_duration)

        if (
            fadein_start_pos is not None
            and fadein_start_pos + crossfade_duration <= SMART_CROSSFADE_DURATION
        ):
            self.filters.append(TrimFilter(logger=self.logger, fadein_start_pos=fadein_start_pos))
            self.timing_info.fadein_trimmed_duration = fadein_start_pos
        else:
            self.logger.log(
                VERBOSE_LOG_LEVEL,
                "Skipping beat alignment: not enough audio after trim (%s + %.1fs > %.1fs)",
                fadein_start_pos,
                crossfade_duration,
                SMART_CROSSFADE_DURATION,
            )

        # Adjust crossfade duration to align with outgoing track's downbeats.
        # When stretching, only consider downbeats after the stretch window
        # to ensure the outgoing track has reached the target tempo.
        crossfade_start = SMART_CROSSFADE_DURATION - crossfade_duration
        crossfade_duration = self._adjust_crossfade_to_downbeats(
            crossfade_duration=crossfade_duration,
            fadein_start_pos=fadein_start_pos,
            min_downbeat_pos=crossfade_start if is_stretched else 0.0,
        )

        # Compensate crossfade duration for time-stretch compression.
        if is_stretched:
            crossfade_duration = crossfade_duration / bpm_ratio

        # 90 BPM -> 1500Hz, 140 BPM -> 2500Hz
        avg_bpm = (self.fade_out_bpm + self.fade_in_bpm) / 2
        crossover_freq = int(np.clip(1500 + (avg_bpm - 90) * 20, 1500, 2500))

        # Adjust for BPM mismatch
        if abs(bpm_ratio - 1.0) > 0.3:
            crossover_freq = int(crossover_freq * 0.85)

        # For shorter fades, use exp/exp curves to avoid abruptness
        if crossfade_bars < 8:
            fadeout_curve = "exponential"
            fadein_curve = "exponential"
        # For long fades, use log/linear curves
        else:
            # Use logarithmic curve to give the next track more space
            fadeout_curve = "logarithmic"
            # Use linear curve for transition, predictable and not too abrupt
            fadein_curve = "linear"

        # Create lowpass filter on the outgoing track (unfiltered → low-pass)
        # Extended lowpass effect to gradually remove bass frequencies
        fadeout_eq_duration = min(max(crossfade_duration * 2.5, 8.0), SMART_CROSSFADE_DURATION)
        # The crossfade always happens at the END of the buffer
        fadeout_eq_start = max(0, SMART_CROSSFADE_DURATION - fadeout_eq_duration)
        fadeout_sweep = FrequencySweepFilter(
            logger=self.logger,
            sweep_type="lowpass",
            target_freq=crossover_freq,
            duration=fadeout_eq_duration,
            start_time=fadeout_eq_start,
            sweep_direction="fade_in",
            poles=1,
            curve_type=fadeout_curve,
            stream_type="fadeout",
        )
        self.filters.append(fadeout_sweep)

        # Create high pass filter on the incoming track (high-pass → unfiltered)
        # Quicker highpass removal to avoid lingering vocals after crossfade
        fadein_eq_duration = crossfade_duration / 1.5
        fadein_sweep = FrequencySweepFilter(
            logger=self.logger,
            sweep_type="highpass",
            target_freq=crossover_freq,
            duration=fadein_eq_duration,
            start_time=0,
            sweep_direction="fade_out",
            poles=1,
            curve_type=fadein_curve,
            stream_type="fadein",
        )
        self.filters.append(fadein_sweep)

        # Add final crossfade filter
        crossfade_filter = CrossfadeFilter(
            logger=self.logger, crossfade_duration=crossfade_duration
        )
        self.filters.append(crossfade_filter)

        fade_out_seconds = fade_out_bytes_len / pcm_format.pcm_sample_size
        fade_in_seconds = fade_in_bytes_len / pcm_format.pcm_sample_size
        # clamp CF to fit shorter inputs (defensive — normally full buffers)
        self.timing_info.crossfade_duration = min(
            crossfade_duration,
            fade_out_seconds,
            max(0.0, fade_in_seconds - self.timing_info.fadein_trimmed_duration),
        )
        self.timing_info.pre_crossfade_duration = max(
            0.0, fade_out_seconds - self.timing_info.crossfade_duration
        )
        self.timing_info.post_crossfade_duration = max(
            0.0,
            fade_in_seconds
            - self.timing_info.fadein_trimmed_duration
            - self.timing_info.crossfade_duration,
        )

    def _apply_gradual_time_stretch(
        self,
        bpm_ratio: float,
        bpm_diff_percent: float,
        crossfade_duration: float,
    ) -> None:
        """Apply gradual time stretch in the 10s window before the crossfade."""
        stretch_duration = 10.0
        crossfade_start = SMART_CROSSFADE_DURATION - crossfade_duration
        stretch_start = max(0.0, crossfade_start - stretch_duration)
        stretch_end = crossfade_start

        # Collect timing points within the stretch window
        beat_mask = (self.fade_out_beats >= stretch_start) & (self.fade_out_beats <= stretch_end)
        db_mask = (self.extrapolated_fadeout_downbeats >= stretch_start) & (
            self.extrapolated_fadeout_downbeats <= stretch_end
        )
        window_beats = self.fade_out_beats[beat_mask] - stretch_start
        window_downbeats = self.extrapolated_fadeout_downbeats[db_mask] - stretch_start

        # >3% BPM diff: beat-level stepping (more steps = smoother)
        # <=3%: downbeat-level stepping, fall back to beats if too few
        if bpm_diff_percent > 3.0:
            stretch_timestamps = window_beats
        elif len(window_downbeats) >= 2:
            stretch_timestamps = window_downbeats
        else:
            stretch_timestamps = window_beats

        # Fall back to synthetic timestamps when < 2 real timestamps
        if len(stretch_timestamps) < 2:
            stretch_timestamps = generate_synthetic_timestamps(
                stretch_end - stretch_start, self.fade_out_bpm
            )

        tempo_steps = compute_gradual_tempo_steps(
            start_ratio=1.0,
            end_ratio=bpm_ratio,
            downbeats=stretch_timestamps,
        )
        if not tempo_steps:
            tempo_steps = [(0.0, bpm_ratio)]

        # Shift timestamps back to buffer-relative coordinates for FFmpeg
        tempo_steps = [(ts + stretch_start, ratio) for ts, ratio in tempo_steps]

        self.filters.append(GradualTimeStretchFilter(self.logger, tempo_steps))

    def _calculate_crossfade_duration(self, crossfade_bars: int) -> float:
        """Calculate final crossfade duration based on musical bars and BPM."""
        # Calculate crossfade duration based on incoming track's BPM
        beats_per_bar = 4
        seconds_per_beat = 60.0 / self.fade_in_bpm
        musical_duration = crossfade_bars * beats_per_bar * seconds_per_beat

        # Apply buffer constraint
        actual_duration = min(musical_duration, SMART_CROSSFADE_DURATION)

        # Log if we had to constrain the duration
        if musical_duration > SMART_CROSSFADE_DURATION:
            self.logger.log(
                VERBOSE_LOG_LEVEL,
                "Constraining crossfade duration from %.1fs to %.1fs (buffer limit)",
                musical_duration,
                actual_duration,
            )

        return actual_duration

    def _calculate_optimal_crossfade_bars(self) -> int:
        """Calculate optimal crossfade bars that fit in available buffer."""
        bpm_in = self.fade_in_bpm
        bpm_out = self.fade_out_bpm
        bpm_diff_percent = abs(1.0 - bpm_in / bpm_out) * 100

        # Calculate ideal bars based on BPM compatibility
        ideal_bars = 10 if bpm_diff_percent <= self.time_stretch_bpm_percentage_threshold else 6

        # Reduce bars until it fits in the fadein buffer
        for bars in [ideal_bars, 8, 6, 4, 2, 1]:
            if bars > ideal_bars:
                continue

            fadein_start_pos = self._calculate_optimal_fade_timing(bars)
            if fadein_start_pos is None:
                continue

            # Calculate what the duration would be
            test_duration = self._calculate_crossfade_duration(crossfade_bars=bars)

            # Check if it fits in fadein buffer
            fadein_buffer = SMART_CROSSFADE_DURATION - fadein_start_pos
            if test_duration <= fadein_buffer:
                if bars < ideal_bars:
                    self.logger.log(
                        VERBOSE_LOG_LEVEL,
                        "Reduced crossfade from %d to %d bars (fadein buffer=%.1fs, needed=%.1fs)",
                        ideal_bars,
                        bars,
                        fadein_buffer,
                        test_duration,
                    )
                return bars

        # Fall back to 1 bar if nothing else fits
        return 1

    def _calculate_optimal_fade_timing(self, crossfade_bars: int) -> float | None:
        """Calculate beat positions for alignment."""
        beats_per_bar = 4

        def calculate_beat_positions(
            fade_out_beats: npt.NDArray[np.float32],
            fade_in_beats: npt.NDArray[np.float32],
            num_beats: int,
        ) -> float | None:
            """Calculate start positions from beat arrays."""
            if len(fade_out_beats) < num_beats or len(fade_in_beats) < num_beats:
                return None

            fade_in_slice = fade_in_beats[:num_beats]
            return float(fade_in_slice[0])

        # Try downbeats first for most musical timing
        downbeat_positions = calculate_beat_positions(
            self.extrapolated_fadeout_downbeats, self.fade_in_downbeats, crossfade_bars
        )
        if downbeat_positions is not None:
            return downbeat_positions

        # Try regular beats if downbeats insufficient
        required_beats = crossfade_bars * beats_per_bar
        beat_positions = calculate_beat_positions(
            self.fade_out_beats, self.fade_in_beats, required_beats
        )
        if beat_positions is not None:
            return beat_positions

        # Fallback: No beat alignment possible
        self.logger.log(VERBOSE_LOG_LEVEL, "No beat alignment possible (insufficient beats)")
        return None

    def _adjust_crossfade_to_downbeats(
        self,
        crossfade_duration: float,
        fadein_start_pos: float | None,
        min_downbeat_pos: float = 0.0,
    ) -> float:
        """Adjust crossfade duration to align with outgoing track's downbeats."""
        # If we don't have downbeats or beat alignment is disabled, return original duration
        if len(self.extrapolated_fadeout_downbeats) == 0 or fadein_start_pos is None:
            return crossfade_duration

        # Calculate where the crossfade would start in the buffer
        ideal_start_pos = SMART_CROSSFADE_DURATION - crossfade_duration

        # Debug logging
        self.logger.log(
            VERBOSE_LOG_LEVEL,
            "Downbeat adjustment - ideal_start=%.2fs (buffer=%.1fs - crossfade=%.2fs), "
            "fadein_start=%.2fs",
            ideal_start_pos,
            SMART_CROSSFADE_DURATION,
            crossfade_duration,
            fadein_start_pos,
        )

        # Find the closest downbeats (earlier and later)
        earlier_downbeat = None
        later_downbeat = None

        for downbeat in self.extrapolated_fadeout_downbeats:
            if downbeat < min_downbeat_pos:
                continue
            if downbeat <= ideal_start_pos:
                earlier_downbeat = downbeat
            elif downbeat > ideal_start_pos and later_downbeat is None:
                later_downbeat = downbeat
                break

        # Try earlier downbeat first (longer crossfade)
        if earlier_downbeat is not None:
            adjusted_duration = float(SMART_CROSSFADE_DURATION - earlier_downbeat)
            if fadein_start_pos + adjusted_duration <= SMART_CROSSFADE_DURATION:
                if abs(adjusted_duration - crossfade_duration) > 0.1:
                    self.logger.log(
                        VERBOSE_LOG_LEVEL,
                        "Adjusted crossfade duration from %.2fs to %.2fs to align with "
                        "downbeat at %.2fs (earlier)",
                        crossfade_duration,
                        adjusted_duration,
                        earlier_downbeat,
                    )
                return adjusted_duration

        # Try later downbeat (shorter crossfade)
        if later_downbeat is not None:
            adjusted_duration = float(SMART_CROSSFADE_DURATION - later_downbeat)
            if fadein_start_pos + adjusted_duration <= SMART_CROSSFADE_DURATION:
                if abs(adjusted_duration - crossfade_duration) > 0.1:
                    self.logger.log(
                        VERBOSE_LOG_LEVEL,
                        "Adjusted crossfade duration from %.2fs to %.2fs to align with "
                        "downbeat at %.2fs (later)",
                        crossfade_duration,
                        adjusted_duration,
                        later_downbeat,
                    )
                return adjusted_duration

        # If no suitable downbeat found, return original duration
        self.logger.log(
            VERBOSE_LOG_LEVEL,
            "Could not adjust crossfade duration to downbeats, using original %.2fs",
            crossfade_duration,
        )
        return crossfade_duration


class StandardCrossFade(SmartFade):
    """Standard crossfade class that implements a standard crossfade mode."""

    def __init__(self, logger: logging.Logger, crossfade_duration: float = 10.0) -> None:
        """Initialize StandardCrossFade with crossfade duration."""
        super().__init__(logger)
        self.crossfade_duration = crossfade_duration

    def _build(
        self,
        fade_out_bytes_len: int,
        fade_in_bytes_len: int,
        pcm_format: AudioFormat,
    ) -> None:
        """Build the standard crossfade filter chain and assign ``self.timing_info``."""
        self.filters = [
            CrossfadeFilter(logger=self.logger, crossfade_duration=self.crossfade_duration),
        ]
        fade_out_seconds = fade_out_bytes_len / pcm_format.pcm_sample_size
        fade_in_seconds = fade_in_bytes_len / pcm_format.pcm_sample_size
        # clamp CF to fit shorter inputs (defensive — normally full buffers)
        effective_cf = min(self.crossfade_duration, fade_out_seconds, fade_in_seconds)
        self.timing_info = CrossfadeTimingInfo(
            pre_crossfade_duration=max(0.0, fade_out_seconds - effective_cf),
            crossfade_duration=effective_cf,
            fadein_trimmed_duration=0.0,
            post_crossfade_duration=max(0.0, fade_in_seconds - effective_cf),
        )

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
        fade_out_part = await strip_silence(fade_out_part, pcm_format=pcm_format, reverse=True)
        fade_out_part = align_audio_to_frame_boundary(fade_out_part, pcm_format)
        crossfade_size = int(pcm_format.pcm_sample_size * self.crossfade_duration)
        # Pre-crossfade: outgoing track minus the crossfaded portion
        pre_crossfade = fade_out_part[:-crossfade_size]
        adjusted_fade_out_part = fade_out_part[-crossfade_size:]

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
