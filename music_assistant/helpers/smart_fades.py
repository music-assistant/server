"""Smart Fades - Object-oriented implementation with intelligent fades and adaptive filtering."""

from __future__ import annotations

import asyncio
import logging
import time
import warnings
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

import aiofiles
import librosa
import numpy as np
import numpy.typing as npt
import shortuuid

from music_assistant.constants import VERBOSE_LOG_LEVEL
from music_assistant.helpers.audio import (
    align_audio_to_frame_boundary,
    strip_silence,
)
from music_assistant.helpers.process import communicate
from music_assistant.helpers.util import remove_file
from music_assistant.models.smart_fades import (
    SmartFadesAnalysis,
    SmartFadesAnalysisFragment,
    SmartFadesMode,
)

if TYPE_CHECKING:
    from music_assistant_models.media_items import AudioFormat
    from music_assistant_models.streamdetails import StreamDetails

    from music_assistant.mass import MusicAssistant

SMART_CROSSFADE_DURATION = 45
ANALYSIS_FPS = 100


class SmartFadesAnalyzer:
    """Smart fades analyzer that performs audio analysis."""

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize smart fades analyzer."""
        self.mass = mass
        self.logger = logging.getLogger(__name__)

    async def analyze(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        fragment: SmartFadesAnalysisFragment,
        audio_data: bytes,
        pcm_format: AudioFormat,
    ) -> SmartFadesAnalysis | None:
        """Analyze a track's beats for BPM matching smart fade."""
        stream_details_name = f"{provider_instance_id_or_domain}://{item_id}"
        start_time = time.perf_counter()
        self.logger.debug(
            "Starting %s beat analysis for track : %s", fragment.name, stream_details_name
        )

        # Validate input audio data is frame-aligned
        audio_data = align_audio_to_frame_boundary(audio_data, pcm_format)

        fragment_duration = len(audio_data) / (pcm_format.pcm_sample_size)
        try:
            self.logger.log(
                VERBOSE_LOG_LEVEL,
                "Audio data: %.2fs, %d bytes",
                fragment_duration,
                len(audio_data),
            )
            # Convert PCM bytes to numpy array and then to mono for analysis
            audio_array = np.frombuffer(audio_data, dtype=np.float32)
            if pcm_format.channels > 1:
                # Ensure array size is divisible by channel count
                samples_per_channel = len(audio_array) // pcm_format.channels
                valid_samples = samples_per_channel * pcm_format.channels
                if valid_samples != len(audio_array):
                    self.logger.warning(
                        "Audio buffer size (%d) not divisible by channels (%d), "
                        "truncating %d samples",
                        len(audio_array),
                        pcm_format.channels,
                        len(audio_array) - valid_samples,
                    )
                    audio_array = audio_array[:valid_samples]

                # Reshape to separate channels and take average for mono conversion
                audio_array = audio_array.reshape(-1, pcm_format.channels)
                mono_audio = np.asarray(np.mean(audio_array, axis=1, dtype=np.float32))
            else:
                # Single channel - ensure consistent array type
                mono_audio = np.asarray(audio_array, dtype=np.float32)

            # Validate that the audio is finite (no NaN or Inf values)
            if not np.all(np.isfinite(mono_audio)):
                self.logger.error(
                    "Audio buffer contains non-finite values (NaN/Inf) for %s, cannot analyze",
                    stream_details_name,
                )
                return None

            analysis = await self._analyze_track_beats(mono_audio, fragment, pcm_format.sample_rate)

            total_time = time.perf_counter() - start_time
            if not analysis:
                self.logger.debug(
                    "No analysis results found after analyzing audio for: %s (took %.2fs).",
                    stream_details_name,
                    total_time,
                )
                return None
            self.logger.debug(
                "Smart fades analysis completed for %s: BPM=%.1f, %d beats, "
                "%d downbeats, confidence=%.2f (took %.2fs)",
                stream_details_name,
                analysis.bpm,
                len(analysis.beats),
                len(analysis.downbeats),
                analysis.confidence,
                total_time,
            )
            self.mass.create_task(
                self.mass.music.set_smart_fades_analysis(
                    item_id, provider_instance_id_or_domain, analysis
                )
            )
            return analysis
        except Exception as e:
            total_time = time.perf_counter() - start_time
            self.logger.exception(
                "Beat analysis error for %s: %s (took %.2fs)",
                stream_details_name,
                e,
                total_time,
            )
            return None

    def _librosa_beat_analysis(
        self,
        audio_array: npt.NDArray[np.float32],
        fragment: SmartFadesAnalysisFragment,
        sample_rate: int,
    ) -> SmartFadesAnalysis | None:
        """Perform beat analysis using librosa."""
        try:
            # Suppress librosa UserWarnings about empty mel filters
            # These warnings are harmless and occur with certain audio characteristics
            with warnings.catch_warnings():
                warnings.filterwarnings(
                    "ignore",
                    message="Empty filters detected in mel frequency basis",
                    category=UserWarning,
                )
                tempo, beats_array = librosa.beat.beat_track(
                    y=audio_array,
                    sr=sample_rate,
                    units="time",
                )
            # librosa returns np.float64 arrays when units="time"

            if len(beats_array) < 2:
                self.logger.warning("Insufficient beats detected: %d", len(beats_array))
                return None

            bpm = float(tempo.item()) if hasattr(tempo, "item") else float(tempo)

            # Calculate confidence based on consistency of intervals
            if len(beats_array) > 2:
                intervals = np.diff(beats_array)
                interval_std = np.std(intervals)
                interval_mean = np.mean(intervals)
                # Lower coefficient of variation = higher confidence
                cv = interval_std / interval_mean if interval_mean > 0 else 1.0
                confidence = max(0.1, 1.0 - cv)
            else:
                confidence = 0.5  # Low confidence with few beats

            downbeats = self._estimate_musical_downbeats(beats_array, bpm)

            # Store complete fragment analysis
            fragment_duration = len(audio_array) / sample_rate

            return SmartFadesAnalysis(
                fragment=fragment,
                bpm=float(bpm),
                beats=beats_array,
                downbeats=downbeats,
                confidence=float(confidence),
                duration=fragment_duration,
            )

        except Exception as e:
            self.logger.exception("Librosa beat analysis failed: %s", e)
            return None

    def _estimate_musical_downbeats(
        self, beats_array: npt.NDArray[np.float64], bpm: float
    ) -> npt.NDArray[np.float64]:
        """Estimate downbeats using musical logic and beat consistency."""
        if len(beats_array) < 4:
            return beats_array[:1] if len(beats_array) > 0 else np.array([])

        # Calculate expected beat interval from BPM
        expected_beat_interval = 60.0 / bpm

        # Look for the most likely starting downbeat by analyzing beat intervals
        # In 4/4 time, downbeats should be every 4 beats
        best_offset = 0
        best_consistency = 0.0

        # Try different starting offsets (0, 1, 2, 3) to find most consistent downbeat pattern
        for offset in range(min(4, len(beats_array))):
            downbeat_candidates = beats_array[offset::4]

            if len(downbeat_candidates) < 2:
                continue

            # Calculate consistency score based on interval regularity
            intervals = np.diff(downbeat_candidates)
            expected_downbeat_interval = 4 * expected_beat_interval

            # Score based on how close intervals are to expected 4-beat interval
            interval_errors = (
                np.abs(intervals - expected_downbeat_interval) / expected_downbeat_interval
            )
            consistency = 1.0 - np.mean(interval_errors)

            if consistency > best_consistency:
                best_consistency = float(consistency)
                best_offset = offset

        # Use the best offset to generate final downbeats
        downbeats = beats_array[best_offset::4]

        self.logger.debug(
            "Downbeat estimation: offset=%d, consistency=%.2f, %d downbeats from %d beats",
            best_offset,
            best_consistency,
            len(downbeats),
            len(beats_array),
        )

        return downbeats

    async def _analyze_track_beats(
        self,
        audio_data: npt.NDArray[np.float32],
        fragment: SmartFadesAnalysisFragment,
        sample_rate: int,
    ) -> SmartFadesAnalysis | None:
        """Analyze track for beat tracking using librosa."""
        try:
            return await asyncio.to_thread(
                self._librosa_beat_analysis, audio_data, fragment, sample_rate
            )
        except Exception as e:
            self.logger.exception("Beat tracking analysis failed: %s", e)
            return None


#############################
# SMART FADES EQ LOGIC
#############################


class Filter(ABC):
    """Abstract base class for audio filters."""

    output_fadeout_label: str
    output_fadein_label: str

    @abstractmethod
    def apply(self, input_fadein_label: str, input_fadeout_label: str) -> list[str]:
        """Apply the filter and return the FFmpeg filter strings."""


class TimeStretchFilter(Filter):
    """Filter that applies time stretching to match BPM using rubberband."""

    output_fadeout_label: str = "fadeout_stretched"
    output_fadein_label: str = "fadein_unchanged"

    def __init__(
        self,
        stretch_ratio: float,
    ):
        """Initialize time stretch filter."""
        self.stretch_ratio = stretch_ratio

    def apply(self, input_fadein_label: str, input_fadeout_label: str) -> list[str]:
        """Create FFmpeg filters to gradually adjust tempo from original BPM to target BPM."""
        return [
            f"{input_fadeout_label}rubberband=tempo={self.stretch_ratio:.6f}:transients=mixed:detector=soft:pitchq=quality"
            f"[{self.output_fadeout_label}]",
            f"{input_fadein_label}anull[{self.output_fadein_label}]",  # codespell:ignore anull
        ]

    def __repr__(self) -> str:
        """Return string representation of TimeStretchFilter."""
        return f"TimeStretch(ratio={self.stretch_ratio:.2f})"


class TrimFilter(Filter):
    """Filter that trims incoming track to align with downbeats."""

    output_fadeout_label: str = "fadeout_beatalign"
    output_fadein_label: str = "fadein_beatalign"

    def __init__(self, fadein_start_pos: float):
        """Initialize beat align filter.

        Args:
            fadein_start_pos: Position in seconds to trim the incoming track to
        """
        self.fadein_start_pos = fadein_start_pos

    def apply(self, input_fadein_label: str, input_fadeout_label: str) -> list[str]:
        """Trim the incoming track to align with downbeats."""
        return [
            f"{input_fadeout_label}anull[{self.output_fadeout_label}]",  # codespell:ignore anull
            f"{input_fadein_label}atrim=start={self.fadein_start_pos},asetpts=PTS-STARTPTS[{self.output_fadein_label}]",
        ]

    def __repr__(self) -> str:
        """Return string representation of TrimFilter."""
        return f"Trim(trim={self.fadein_start_pos:.2f}s)"


class FrequencySweepFilter(Filter):
    """Filter that creates frequency sweep effects (lowpass/highpass transitions)."""

    output_fadeout_label: str = "frequency_sweep"
    output_fadein_label: str = "frequency_sweep"

    def __init__(
        self,
        sweep_type: str,
        target_freq: int,
        duration: float,
        start_time: float,
        sweep_direction: str,
        poles: int,
        curve_type: str,
        stream_type: str = "fadeout",
    ):
        """Initialize frequency sweep filter.

        Args:
            sweep_type: 'lowpass' or 'highpass'
            target_freq: Target frequency for the filter
            duration: Duration of the sweep in seconds
            start_time: When to start the sweep
            sweep_direction: 'fade_in' (unfiltered->filtered) or 'fade_out' (filtered->unfiltered)
            poles: Number of poles for the filter
            curve_type: 'linear', 'exponential', or 'logarithmic'
            stream_type: 'fadeout' or 'fadein' - which stream to process
        """
        self.sweep_type = sweep_type
        self.target_freq = target_freq
        self.duration = duration
        self.start_time = start_time
        self.sweep_direction = sweep_direction
        self.poles = poles
        self.curve_type = curve_type
        self.stream_type = stream_type

        # Set output labels based on stream type
        if stream_type == "fadeout":
            self.output_fadeout_label = f"fadeout_{sweep_type}"
            self.output_fadein_label = "fadein_passthrough"
        else:
            self.output_fadeout_label = "fadeout_passthrough"
            self.output_fadein_label = f"fadein_{sweep_type}"

    def _generate_volume_expr(self, start: float, dur: float, direction: str, curve: str) -> str:
        t_expr = f"t-{start}"  # Time relative to start
        norm_t = f"min(max({t_expr},0),{dur})/{dur}"  # Normalized 0-1

        if curve == "exponential":
            # Exponential curve for smoother transitions
            if direction == "up":
                return f"'pow({norm_t},2)':eval=frame"
            else:
                return f"'1-pow({norm_t},2)':eval=frame"
        elif curve == "logarithmic":
            # Logarithmic curve for more aggressive initial change
            if direction == "up":
                return f"'sqrt({norm_t})':eval=frame"
            else:
                return f"'1-sqrt({norm_t})':eval=frame"
        elif direction == "up":
            return f"'{norm_t}':eval=frame"
        else:
            return f"'1-{norm_t}':eval=frame"

    def apply(self, input_fadein_label: str, input_fadeout_label: str) -> list[str]:
        """Generate FFmpeg filters for frequency sweep effect."""
        # Select the correct input based on stream type
        if self.stream_type == "fadeout":
            input_label = input_fadeout_label
            output_label = self.output_fadeout_label
            passthrough_label = self.output_fadein_label
            passthrough_input = input_fadein_label
        else:
            input_label = input_fadein_label
            output_label = self.output_fadein_label
            passthrough_label = self.output_fadeout_label
            passthrough_input = input_fadeout_label

        orig_label = f"{output_label}_orig"
        filter_label = f"{output_label}_to{self.sweep_type[:2]}"
        filtered_label = f"{output_label}_filtered"
        orig_faded_label = f"{output_label}_orig_faded"
        filtered_faded_label = f"{output_label}_filtered_faded"

        # Determine volume ramp directions based on sweep direction
        if self.sweep_direction == "fade_in":
            # Fade from dry to wet (unfiltered to filtered)
            orig_direction = "down"
            filter_direction = "up"
        else:  # fade_out
            # Fade from wet to dry (filtered to unfiltered)
            orig_direction = "up"
            filter_direction = "down"

        # Build filter chain
        orig_volume_expr = self._generate_volume_expr(
            self.start_time, self.duration, orig_direction, self.curve_type
        )
        filtered_volume_expr = self._generate_volume_expr(
            self.start_time, self.duration, filter_direction, self.curve_type
        )

        return [
            # Pass through the other stream unchanged
            f"{passthrough_input}anull[{passthrough_label}]",  # codespell:ignore anull
            # Split input into two paths
            f"{input_label}asplit=2[{orig_label}][{filter_label}]",
            # Apply frequency filter to one path
            f"[{filter_label}]{self.sweep_type}=f={self.target_freq}:poles={self.poles}[{filtered_label}]",
            # Apply time-varying volume to original path
            f"[{orig_label}]volume={orig_volume_expr}[{orig_faded_label}]",
            # Apply time-varying volume to filtered path
            f"[{filtered_label}]volume={filtered_volume_expr}[{filtered_faded_label}]",
            # Mix the two paths together
            f"[{orig_faded_label}][{filtered_faded_label}]amix=inputs=2:duration=longest:normalize=0[{output_label}]",
        ]

    def __repr__(self) -> str:
        """Return string representation of FrequencySweepFilter."""
        return f"FreqSweep({self.sweep_type}@{self.target_freq}Hz)"


class CrossfadeFilter(Filter):
    """Filter that applies the final crossfade between fadeout and fadein streams."""

    output_fadeout_label: str = "crossfade"
    output_fadein_label: str = "crossfade"

    def __init__(self, crossfade_duration: float):
        """Initialize crossfade filter."""
        self.crossfade_duration = crossfade_duration

    def apply(self, input_fadein_label: str, input_fadeout_label: str) -> list[str]:
        """Apply the acrossfade filter."""
        return [f"{input_fadeout_label}{input_fadein_label}acrossfade=d={self.crossfade_duration}"]

    def __repr__(self) -> str:
        """Return string representation of CrossfadeFilter."""
        return f"Crossfade(d={self.crossfade_duration:.1f}s)"


class SmartFade(ABC):
    """Abstract base class for Smart Fades."""

    filters: list[Filter]

    def __init__(self) -> None:
        """Initialize SmartFade base class."""
        self.logger = logging.getLogger(__name__)
        self.filters = []

    @abstractmethod
    def _build(self) -> None:
        """Build the smart fades filter chain."""
        ...

    def _get_ffmpeg_filters(
        self,
        input_fadein_label: str = "[1]",
        input_fadeout_label: str = "[0]",
    ) -> list[str]:
        """Get FFmpeg filters for smart fades."""
        if not self.filters:
            self._build()
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
        fade_in_part: bytes,
        pcm_format: AudioFormat,
    ) -> bytes:
        """Apply the smart fade to the given PCM audio parts."""
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
        self.logger.debug("FFmpeg smartfade args: %s", " ".join(args))
        self.logger.log(VERBOSE_LOG_LEVEL, "FFmpeg command args: %s", " ".join(args))

        # Execute the enhanced smart fade with full buffer
        _, raw_crossfade_output, stderr = await communicate(args, fade_in_part)
        await remove_file(fadeout_filename)

        if raw_crossfade_output:
            return raw_crossfade_output
        else:
            stderr_msg = stderr.decode() if stderr else "(no stderr output)"
            raise RuntimeError(f"Smart crossfade failed. FFmpeg stderr: {stderr_msg}")

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
        self, fade_out_analysis: SmartFadesAnalysis, fade_in_analysis: SmartFadesAnalysis
    ) -> None:
        """Initialize SmartFades with analysis data.

        Args:
            fade_out_analysis: Analysis data for the outgoing track
            fade_in_analysis: Analysis data for the incoming track
            logger: Optional logger for debug output
        """
        self.fade_out_analysis = fade_out_analysis
        self.fade_in_analysis = fade_in_analysis
        super().__init__()

    def _build(self) -> None:
        """Build the smart fades filter chain."""
        # Calculate tempo factor for time stretching
        bpm_ratio = self.fade_in_analysis.bpm / self.fade_out_analysis.bpm
        bpm_diff_percent = abs(1.0 - bpm_ratio) * 100

        # Extrapolate downbeats for better bar calculation
        self.extrapolated_fadeout_downbeats = extrapolate_downbeats(
            self.fade_out_analysis.downbeats,
            tempo_factor=1.0,
            bpm=self.fade_out_analysis.bpm,
        )

        # Calculate optimal crossfade bars that fit in available buffer
        crossfade_bars = self._calculate_optimal_crossfade_bars()

        # Calculate beat positions for the selected bar count
        fadein_start_pos = self._calculate_optimal_fade_timing(crossfade_bars)

        # Calculate initial crossfade duration (may be adjusted later for downbeat alignment)
        crossfade_duration = self._calculate_crossfade_duration(crossfade_bars=crossfade_bars)

        # Add time stretch filter if needed
        if (
            0.1 < bpm_diff_percent <= self.time_stretch_bpm_percentage_threshold
            and crossfade_bars > 4
        ):
            self.filters.append(TimeStretchFilter(stretch_ratio=bpm_ratio))
            # Re-extrapolate downbeats with actual tempo factor for time-stretched audio
            self.extrapolated_fadeout_downbeats = extrapolate_downbeats(
                self.fade_out_analysis.downbeats,
                tempo_factor=bpm_ratio,
                bpm=self.fade_out_analysis.bpm,
            )

        # Check if we would have enough audio after beat alignment for the crossfade
        if fadein_start_pos and fadein_start_pos + crossfade_duration <= SMART_CROSSFADE_DURATION:
            self.filters.append(TrimFilter(fadein_start_pos=fadein_start_pos))
        else:
            self.logger.debug(
                "Skipping beat alignment: not enough audio after trim (%.1fs + %.1fs > %.1fs)",
                fadein_start_pos,
                crossfade_duration,
                SMART_CROSSFADE_DURATION,
            )

        # Adjust crossfade duration to align with outgoing track's downbeats
        crossfade_duration = self._adjust_crossfade_to_downbeats(
            crossfade_duration=crossfade_duration,
            fadein_start_pos=fadein_start_pos,
        )

        # 90 BPM -> 1500Hz, 140 BPM -> 2500Hz
        avg_bpm = (self.fade_out_analysis.bpm + self.fade_in_analysis.bpm) / 2
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
        crossfade_filter = CrossfadeFilter(crossfade_duration=crossfade_duration)
        self.filters.append(crossfade_filter)

    def _calculate_crossfade_duration(self, crossfade_bars: int) -> float:
        """Calculate final crossfade duration based on musical bars and BPM."""
        # Calculate crossfade duration based on incoming track's BPM
        beats_per_bar = 4
        seconds_per_beat = 60.0 / self.fade_in_analysis.bpm
        musical_duration = crossfade_bars * beats_per_bar * seconds_per_beat

        # Apply buffer constraint
        actual_duration = min(musical_duration, SMART_CROSSFADE_DURATION)

        # Log if we had to constrain the duration
        if musical_duration > SMART_CROSSFADE_DURATION:
            self.logger.debug(
                "Constraining crossfade duration from %.1fs to %.1fs (buffer limit)",
                musical_duration,
                actual_duration,
            )

        return actual_duration

    def _calculate_optimal_crossfade_bars(self) -> int:
        """Calculate optimal crossfade bars that fit in available buffer."""
        bpm_in = self.fade_in_analysis.bpm
        bpm_out = self.fade_out_analysis.bpm
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
                    self.logger.debug(
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
            fade_out_beats: npt.NDArray[np.float64],
            fade_in_beats: npt.NDArray[np.float64],
            num_beats: int,
        ) -> float | None:
            """Calculate start positions from beat arrays."""
            if len(fade_out_beats) < num_beats or len(fade_in_beats) < num_beats:
                return None

            fade_in_slice = fade_in_beats[:num_beats]
            return float(fade_in_slice[0])

        # Try downbeats first for most musical timing
        downbeat_positions = calculate_beat_positions(
            self.extrapolated_fadeout_downbeats, self.fade_in_analysis.downbeats, crossfade_bars
        )
        if downbeat_positions:
            return downbeat_positions

        # Try regular beats if downbeats insufficient
        required_beats = crossfade_bars * beats_per_bar
        beat_positions = calculate_beat_positions(
            self.fade_out_analysis.beats, self.fade_in_analysis.beats, required_beats
        )
        if beat_positions:
            return beat_positions

        # Fallback: No beat alignment possible
        self.logger.debug("No beat alignment possible (insufficient beats)")
        return None

    def _adjust_crossfade_to_downbeats(
        self,
        crossfade_duration: float,
        fadein_start_pos: float | None,
    ) -> float:
        """Adjust crossfade duration to align with outgoing track's downbeats."""
        # If we don't have downbeats or beat alignment is disabled, return original duration
        if len(self.extrapolated_fadeout_downbeats) == 0 or fadein_start_pos is None:
            return crossfade_duration

        # Calculate where the crossfade would start in the buffer
        ideal_start_pos = SMART_CROSSFADE_DURATION - crossfade_duration

        # Debug logging
        self.logger.debug(
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
                    self.logger.debug(
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
                    self.logger.debug(
                        "Adjusted crossfade duration from %.2fs to %.2fs to align with "
                        "downbeat at %.2fs (later)",
                        crossfade_duration,
                        adjusted_duration,
                        later_downbeat,
                    )
                return adjusted_duration

        # If no suitable downbeat found, return original duration
        self.logger.debug(
            "Could not adjust crossfade duration to downbeats, using original %.2fs",
            crossfade_duration,
        )
        return crossfade_duration


class StandardCrossFade(SmartFade):
    """Standard crossfade class that implements a standard crossfade mode."""

    def __init__(self, crossfade_duration: float = 10.0) -> None:
        """Initialize StandardCrossFade with crossfade duration."""
        self.crossfade_duration = crossfade_duration
        super().__init__()

    def _build(self) -> None:
        """Build the standard crossfade filter chain."""
        self.filters = [
            CrossfadeFilter(crossfade_duration=self.crossfade_duration),
        ]

    async def apply(
        self, fade_out_part: bytes, fade_in_part: bytes, pcm_format: AudioFormat
    ) -> bytes:
        """Apply the standard crossfade to the given PCM audio parts."""
        # We need to override the default apply here, since standard crossfade only needs to be
        # applied to the overlapping parts, not the full buffers.
        crossfade_size = int(pcm_format.pcm_sample_size * self.crossfade_duration)
        # Pre-crossfade: outgoing track minus the crossfaded portion
        pre_crossfade = fade_out_part[:-crossfade_size]
        # Post-crossfade: incoming track minus the crossfaded portion
        post_crossfade = fade_in_part[crossfade_size:]
        # Adjust portions to exact crossfade size
        adjusted_fade_in_part = fade_in_part[:crossfade_size]
        adjusted_fade_out_part = fade_out_part[-crossfade_size:]
        # Adjust the duration to match actual sizes
        self.crossfade_duration = min(
            len(adjusted_fade_in_part) / pcm_format.pcm_sample_size,
            len(adjusted_fade_out_part) / pcm_format.pcm_sample_size,
        )
        # Crossfaded portion: user's configured duration
        crossfaded_section = await super().apply(
            adjusted_fade_out_part, adjusted_fade_in_part, pcm_format
        )
        # Full result: everything concatenated
        return pre_crossfade + crossfaded_section + post_crossfade


#############################
# SMART FADES MIXER LOGIC
#############################
class SmartFadesMixer:
    """Smart fades mixer class that mixes tracks based on analysis data."""

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize smart fades mixer."""
        self.mass = mass
        self.logger = logging.getLogger(__name__)
        # TODO: Refactor into stream (or metadata) controller after we have split the controllers
        self.analyzer = SmartFadesAnalyzer(mass)

    async def mix(
        self,
        fade_in_part: bytes,
        fade_out_part: bytes,
        fade_in_streamdetails: StreamDetails,
        fade_out_streamdetails: StreamDetails,
        pcm_format: AudioFormat,
        standard_crossfade_duration: int = 10,
        mode: SmartFadesMode = SmartFadesMode.SMART_CROSSFADE,
    ) -> bytes:
        """Apply crossfade with internal state management and smart/standard fallback logic."""
        if mode == SmartFadesMode.DISABLED:
            # No crossfade, just concatenate
            # Note that this should not happen since we check this before calling mix()
            # but just to be sure...
            return fade_out_part + fade_in_part

        # strip silence from end of audio of fade_out_part
        fade_out_part = await strip_silence(
            self.mass,
            fade_out_part,
            pcm_format=pcm_format,
            reverse=True,
        )
        # Ensure frame alignment after silence stripping
        fade_out_part = align_audio_to_frame_boundary(fade_out_part, pcm_format)

        # strip silence from begin of audio of fade_in_part
        fade_in_part = await strip_silence(
            self.mass,
            fade_in_part,
            pcm_format=pcm_format,
            reverse=False,
        )
        # Ensure frame alignment after silence stripping
        fade_in_part = align_audio_to_frame_boundary(fade_in_part, pcm_format)
        if mode == SmartFadesMode.STANDARD_CROSSFADE:
            smart_fade: SmartFade = StandardCrossFade(
                crossfade_duration=standard_crossfade_duration
            )
            return await smart_fade.apply(
                fade_out_part,
                fade_in_part,
                pcm_format,
            )
        # Attempt smart crossfade with analysis data
        fade_out_analysis: SmartFadesAnalysis | None
        if stored_analysis := await self.mass.music.get_smart_fades_analysis(
            fade_out_streamdetails.item_id,
            fade_out_streamdetails.provider,
            SmartFadesAnalysisFragment.OUTRO,
        ):
            fade_out_analysis = stored_analysis
        else:
            fade_out_analysis = await self.analyzer.analyze(
                fade_out_streamdetails.item_id,
                fade_out_streamdetails.provider,
                SmartFadesAnalysisFragment.OUTRO,
                fade_out_part,
                pcm_format,
            )

        fade_in_analysis: SmartFadesAnalysis | None
        if stored_analysis := await self.mass.music.get_smart_fades_analysis(
            fade_in_streamdetails.item_id,
            fade_in_streamdetails.provider,
            SmartFadesAnalysisFragment.INTRO,
        ):
            fade_in_analysis = stored_analysis
        else:
            fade_in_analysis = await self.analyzer.analyze(
                fade_in_streamdetails.item_id,
                fade_in_streamdetails.provider,
                SmartFadesAnalysisFragment.INTRO,
                fade_in_part,
                pcm_format,
            )
        if (
            fade_out_analysis
            and fade_in_analysis
            and fade_out_analysis.confidence > 0.3
            and fade_in_analysis.confidence > 0.3
            and mode == SmartFadesMode.SMART_CROSSFADE
        ):
            try:
                smart_fade = SmartCrossFade(fade_out_analysis, fade_in_analysis)
                return await smart_fade.apply(
                    fade_out_part,
                    fade_in_part,
                    pcm_format,
                )
            except Exception as e:
                self.logger.warning(
                    "Smart crossfade failed: %s, falling back to standard crossfade", e
                )

        # Always fallback to Standard Crossfade in case something goes wrong
        smart_fade = StandardCrossFade(crossfade_duration=standard_crossfade_duration)
        return await smart_fade.apply(
            fade_out_part,
            fade_in_part,
            pcm_format,
        )


# HELPER METHODS
def get_bpm_diff_percentage(bpm1: float, bpm2: float) -> float:
    """Calculate BPM difference percentage between two BPM values."""
    return abs(1.0 - bpm1 / bpm2) * 100


def extrapolate_downbeats(
    downbeats: npt.NDArray[np.float64],
    tempo_factor: float,
    buffer_size: float = SMART_CROSSFADE_DURATION,
    bpm: float | None = None,
) -> npt.NDArray[np.float64]:
    """Extrapolate downbeats based on actual intervals when detection is incomplete.

    This is needed when we want to perform beat alignment in an 'atmospheric' outro
    that does not have any detected downbeats.

    Args:
        downbeats: Array of detected downbeat positions in seconds
        tempo_factor: Tempo adjustment factor for time stretching
        buffer_size: Maximum buffer size in seconds
        bpm: Optional BPM for validation when extrapolating with only 2 downbeats
    """
    # Handle case with exactly 2 downbeats (with BPM validation)
    if len(downbeats) == 2 and bpm is not None:
        interval = float(downbeats[1] - downbeats[0])

        # Expected interval for this BPM (assuming 4/4 time signature)
        expected_interval = (60.0 / bpm) * 4

        # Only extrapolate if interval matches BPM within 15% tolerance
        if abs(interval - expected_interval) / expected_interval < 0.15:
            # Adjust detected downbeats for time stretching first
            adjusted_downbeats = downbeats / tempo_factor
            last_downbeat = adjusted_downbeats[-1]

            # If the last downbeat is close to the buffer end, no extrapolation needed
            if last_downbeat >= buffer_size - 5:
                return adjusted_downbeats

            # Adjust the interval for time stretching
            adjusted_interval = interval / tempo_factor

            # Extrapolate forward from last adjusted downbeat using adjusted interval
            extrapolated = []
            current_pos = last_downbeat + adjusted_interval
            max_extrapolation_distance = 125.0  # Don't extrapolate more than 25s

            while (
                current_pos < buffer_size
                and (current_pos - last_downbeat) <= max_extrapolation_distance
            ):
                extrapolated.append(current_pos)
                current_pos += adjusted_interval

            if extrapolated:
                # Combine adjusted detected downbeats and extrapolated downbeats
                return np.concatenate([adjusted_downbeats, np.array(extrapolated)])

            return adjusted_downbeats
        # else: interval doesn't match BPM, fall through to return original

    if len(downbeats) < 2:
        # Need at least 2 downbeats to extrapolate
        return downbeats / tempo_factor

    # Adjust detected downbeats for time stretching first
    adjusted_downbeats = downbeats / tempo_factor
    last_downbeat = adjusted_downbeats[-1]

    # If the last downbeat is close to the buffer end, no extrapolation needed
    if last_downbeat >= buffer_size - 5:
        return adjusted_downbeats

    # Calculate intervals from ORIGINAL downbeats (before time stretching)
    intervals = np.diff(downbeats)
    median_interval = float(np.median(intervals))
    std_interval = float(np.std(intervals))

    # Only extrapolate if intervals are consistent (low standard deviation)
    if std_interval > 0.2:
        return adjusted_downbeats

    # Adjust the interval for time stretching
    # When slowing down (tempo_factor < 1.0), intervals get longer
    adjusted_interval = median_interval / tempo_factor

    # Extrapolate forward from last adjusted downbeat using adjusted interval
    extrapolated = []
    current_pos = last_downbeat + adjusted_interval
    max_extrapolation_distance = 25.0  # Don't extrapolate more than 25s

    while current_pos < buffer_size and (current_pos - last_downbeat) <= max_extrapolation_distance:
        extrapolated.append(current_pos)
        current_pos += adjusted_interval

    if extrapolated:
        # Combine adjusted detected downbeats and extrapolated downbeats
        return np.concatenate([adjusted_downbeats, np.array(extrapolated)])

    return adjusted_downbeats
