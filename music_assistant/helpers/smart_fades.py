"""Smart Fades - Object-oriented implementation with intelligent fades and adaptive filtering."""

# TODO: Figure out if we can achieve shared buffer with StreamController on full
# current and next track for more EQ options.
# TODO: Refactor the Analyzer into a metadata controller after we have split the controllers
# TODO: Refactor the Mixer into a stream controller after we have split the controllers
from __future__ import annotations

import asyncio
import logging
import time
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import aiofiles
import librosa
import numpy as np
import shortuuid
from music_assistant_models.enums import ContentType, MediaType
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.smart_fades import SmartFadesAnalysis

from music_assistant.constants import VERBOSE_LOG_LEVEL
from music_assistant.helpers.audio import crossfade_pcm_parts, get_media_stream
from music_assistant.helpers.process import communicate
from music_assistant.helpers.util import remove_file

if TYPE_CHECKING:
    from music_assistant_models.streamdetails import StreamDetails

    from music_assistant.mass import MusicAssistant

MAX_SMART_CROSSFADE_DURATION = 45  # Increased from 30s to support 16-bar crossfades at slower BPMs
ANALYSIS_FPS = 100
ANALYSIS_PCM_FORMAT = AudioFormat(
    content_type=ContentType.PCM_F32LE, sample_rate=44100, bit_depth=32, channels=2
)


class DJStyleMode(StrEnum):
    """DJ transition style modes."""

    AUTO = "auto"  # Automatically select based on BPM compatibility
    CLASSIC = "classic"  # Traditional HP/LP complementary filters
    MODERN = "modern"  # Swapped LP/HP filters (club style)
    OFF = "off"  # No frequency filtering, volume crossfade only


class SmartFadesAnalyzer:
    """Smart fades analyzer that performs audio analysis."""

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize smart fades analyzer."""
        self.mass = mass
        self.logger = logging.getLogger(__name__)

    async def analyze(
        self,
        streamdetails: StreamDetails,
    ) -> SmartFadesAnalysis | None:
        """Analyze a track's beats for BPM matching smart fade."""
        # Only analyze tracks (not radio streams)
        stream_details_name = f"{streamdetails.provider}://{streamdetails.item_id}"
        if streamdetails.media_type != MediaType.TRACK:
            self.logger.debug(
                "Skipping smart fades analysis for non-track item: %s", stream_details_name
            )
            return None

        start_time = time.perf_counter()
        self.logger.debug("Starting beat analysis for track : %s", stream_details_name)
        try:
            audio_data = await self._get_audio_bytes_from_stream_details(streamdetails)
            self.logger.log(
                VERBOSE_LOG_LEVEL,
                "Audio data: %.2fs, %d bytes",
                streamdetails.duration or 0,
                len(audio_data),
            )
            # Perform beat analysis
            analysis = await self._analyze_track_beats(audio_data)
            total_time = time.perf_counter() - start_time
            if not analysis:
                self.logger.debug(
                    "No analysis results found after analyzing audio for: %s (took %.2fs).",
                    stream_details_name,
                    total_time,
                )
                return None
            self.logger.info(
                "Smart fades analysis completed for %s: BPM=%.1f, confidence=%.2f (took %.2fs)",
                stream_details_name,
                analysis.bpm,
                analysis.confidence,
                total_time,
            )
            # Store analysis results in database for future use
            self.mass.create_task(
                self.mass.music.set_smart_fades_analysis(
                    streamdetails.item_id, streamdetails.provider, analysis
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
        self, audio_array: np.ndarray[Any, np.dtype[np.float32]]
    ) -> SmartFadesAnalysis | None:
        """Perform beat analysis using librosa.

        Uses librosa.beat.beat_track() for reliable BPM and beat detection.
        This runs in a thread pool via asyncio.to_thread() for async compatibility.
        """
        try:
            # Convert stereo to mono for analysis
            if audio_array.shape[1] == 2:
                audio_mono = np.mean(audio_array, axis=1).astype(np.float32)
            else:
                audio_mono = audio_array[:, 0].astype(np.float32)

            sample_rate = ANALYSIS_PCM_FORMAT.sample_rate

            # Use librosa for beat tracking (CPU-intensive operation)
            tempo, beats_array = librosa.beat.beat_track(
                y=audio_mono,
                sr=sample_rate,
                units="time",  # Return beat times in seconds
            )

            if len(beats_array) < 2:
                self.logger.warning("Insufficient beats detected: %d", len(beats_array))
                return None

            # Use tempo from librosa (more accurate than manual calculation)
            # Handle numpy scalar deprecation warning
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

            # Estimate downbeats using improved musical logic
            downbeats = self._estimate_musical_downbeats(beats_array, bpm)

            # Store complete track analysis
            track_duration = len(audio_mono) / sample_rate

            # Validation logging for mixer compatibility
            self.logger.debug(
                "Librosa analysis: BPM=%.1f, %d beats, %d downbeats, duration=%.1fs, conf=%.2f",
                bpm,
                len(beats_array),
                len(downbeats),
                track_duration,
                confidence,
            )

            return SmartFadesAnalysis(
                bpm=float(bpm),
                beats=beats_array,
                downbeats=downbeats,
                confidence=float(confidence),
                duration=track_duration,
            )

        except Exception as e:
            self.logger.exception("Librosa beat analysis failed: %s", e)
            return None

    def _estimate_musical_downbeats(
        self, beats_array: np.ndarray[Any, np.dtype[np.float64]], bpm: float
    ) -> np.ndarray[Any, np.dtype[np.float64]]:
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

    async def _get_audio_bytes_from_stream_details(self, streamdetails: StreamDetails) -> bytes:
        """Retrieve bytes from the audio stream."""
        audio_data = b""
        async for chunk in get_media_stream(
            self.mass,
            streamdetails=streamdetails,
            pcm_format=ANALYSIS_PCM_FORMAT,
            filter_params=[],
        ):
            audio_data += chunk
        if not audio_data:
            self.logger.warning(
                "No audio data received for analysis: %s",
                f"{streamdetails.provider}/{streamdetails.item_id}",
            )
            return b""
        return audio_data

    async def _analyze_track_beats(
        self,
        audio_data: bytes,
    ) -> SmartFadesAnalysis | None:
        """Analyze track for beat tracking using librosa."""
        try:
            # Prepare audio data in main thread (lightweight)
            audio_array = self._prepare_audio_for_librosa(audio_data)

            # Run CPU-intensive librosa analysis in thread pool
            return await asyncio.to_thread(self._librosa_beat_analysis, audio_array)
        except Exception as e:
            self.logger.exception("Beat tracking analysis failed: %s", e)
            return None

    def _prepare_audio_for_librosa(self, pcm_data: bytes) -> np.ndarray[Any, np.dtype[np.float32]]:
        """Convert PCM bytes to numpy array for librosa."""
        # Convert 32-bit float PCM to numpy array
        audio_array = np.frombuffer(pcm_data, dtype=np.float32)
        if len(audio_array) % 2 == 0:  # Stereo
            audio_array = audio_array.reshape(-1, 2)
        else:  # Mono (pad to make even)
            audio_array = np.pad(audio_array, (0, 1)).reshape(-1, 2)
        return audio_array


class SmartFadesMixer:
    """Smart fades mixer class that mixes tracks based on analysis data."""

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize smart fades mixer."""
        self.mass = mass
        self.logger = logging.getLogger(__name__)

    async def mix(
        self,
        fade_in_part: bytes,
        fade_out_part: bytes,
        fade_in_analysis: SmartFadesAnalysis,
        fade_out_analysis: SmartFadesAnalysis,
        pcm_format: AudioFormat,
        fallback_crossfade_duration: int = 10,
        dj_style_mode: DJStyleMode = DJStyleMode.AUTO,
    ) -> bytes:
        """Apply crossfade with internal state management and smart/standard fallback logic."""
        if (
            fade_out_analysis
            and fade_in_analysis
            and fade_out_analysis.confidence > 0.3
            and fade_in_analysis.confidence > 0.3
        ):
            try:
                return await self._apply_smart_crossfade(
                    fade_out_analysis,
                    fade_in_analysis,
                    fade_out_part,
                    fade_in_part,
                    pcm_format,
                    dj_style_mode,
                )
            except Exception as e:
                self.logger.warning(
                    "Smart crossfade failed: %s, falling back to standard crossfade", e
                )

        # Use standard crossfade
        return await self._default_crossfade(
            fade_in_part,
            fade_out_part,
            pcm_format,
            fallback_crossfade_duration,
        )

    async def _apply_smart_crossfade(
        self,
        fade_out_analysis: SmartFadesAnalysis,
        fade_in_analysis: SmartFadesAnalysis,
        fade_out_part: bytes,
        fade_in_part: bytes,
        pcm_format: AudioFormat,
        dj_style_mode: DJStyleMode = DJStyleMode.AUTO,
    ) -> bytes:
        """Apply smart crossfade with beat-perfect timing and adaptive filtering."""
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

        smart_fade_filters = self._create_enhanced_smart_fade_filters(
            fade_out_analysis,
            fade_in_analysis,
            dj_style_mode,
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

        self.logger.debug("FFmpeg command args: %s", " ".join(args))

        # Execute the enhanced smart fade with full buffer
        _, raw_crossfade_output, stderr = await communicate(args, fade_in_part)
        await remove_file(fadeout_filename)

        if raw_crossfade_output:
            return raw_crossfade_output
        else:
            stderr_msg = stderr.decode() if stderr else "(no stderr output)"
            raise RuntimeError(f"Smart crossfade failed. FFmpeg stderr: {stderr_msg}")

    # SMART FADE HELPER METHODS
    def _create_enhanced_smart_fade_filters(
        self,
        fade_out_analysis: SmartFadesAnalysis,
        fade_in_analysis: SmartFadesAnalysis,
        dj_style_mode: DJStyleMode = DJStyleMode.AUTO,
    ) -> list[str]:
        """Create smart fade filters with perfect timing and adaptive filtering."""
        crossfade_duration, fadeout_start_pos, fadein_start_pos = self._calculate_optimal_fade_timing(
            fade_out_analysis, fade_in_analysis
        )

        self.logger.debug(
            "Smart fade: out_bpm=%.1f, in_bpm=%.1f, crossfade duration: %.2fs, mode=%s%s",
            fade_out_analysis.bpm,
            fade_in_analysis.bpm,
            crossfade_duration,
            dj_style_mode,
            ", beat-aligned" if fadeout_start_pos else "",
        )

        filters: list[str] = []
        
        time_stretch_filters, tempo_factor = self._create_time_stretch_filters(
            fade_out_analysis=fade_out_analysis,
            fade_in_analysis=fade_in_analysis,
            crossfade_duration=crossfade_duration
        )
        filters.extend(time_stretch_filters)

        beat_align_filters = self._perform_beat_alignment(
            fadeout_start_pos,
            fadein_start_pos,
            fade_out_analysis,
            "[fadeout_stretched]",
            tempo_factor,
        )
        filters.extend(beat_align_filters)

        if dj_style_mode == DJStyleMode.AUTO:
            dj_style_mode = self._determine_dj_style_mode(fade_out_analysis, fade_in_analysis)

        if dj_style_mode == DJStyleMode.OFF:
            # No frequency filtering, just pass through
            filters.extend(
                [
                    "[fadeout_beatalign]anull[fadeout_eq]",  # codespell:ignore anull
                    "[fadein_beatalign]anull[fadein_eq]",  # codespell:ignore anull
                ]
            )
        elif dj_style_mode == DJStyleMode.MODERN:
            frequency_filters = self._dj_modern(
                fade_out_analysis=fade_out_analysis,
                fade_in_analysis=fade_in_analysis,
                fade_out_label="[fadeout_beatalign]",
                fade_in_label="[fadein_beatalign]",
                crossfade_duration=crossfade_duration,
            )
            filters.extend(frequency_filters)
        else:
            frequency_filters = self._dj_classic(
                fade_out_analysis=fade_out_analysis,
                fade_in_analysis=fade_in_analysis,
                fade_out_label="[fadeout_beatalign]",
                fade_in_label="[fadein_beatalign]",
                crossfade_duration=crossfade_duration,
            )
            filters.extend(frequency_filters)

        # Apply linear crossfade (no curves to avoid interfering with gradual EQ ramping)
        filters.append(f"[fadeout_eq][fadein_eq]acrossfade=d={crossfade_duration}")

        return filters

    def _calculate_optimal_crossfade_bars(
        self, fade_out_analysis: SmartFadesAnalysis, fade_in_analysis: SmartFadesAnalysis
    ) -> int:
        """Calculate optimal crossfade bars based on BPM compatibility."""
        bpm_in = fade_in_analysis.bpm
        bpm_out = fade_out_analysis.bpm
        bpm_diff_percent = abs(1.0 - bpm_in / bpm_out) * 100

        # For now, calculate based on bpm difference only. In the future we can add phrase length, energy etc.
        if bpm_diff_percent < 1.5:
            bars = 16
        elif bpm_diff_percent < 3.0:
            bars = 8
        elif bpm_diff_percent < 8.0:
            bars = 4
        elif bpm_diff_percent < 25.0:
            bars = 2
        else:
            bars = 1
        return bars

    def _calculate_optimal_fade_timing(
        self,
        fade_out_analysis: SmartFadesAnalysis,
        fade_in_analysis: SmartFadesAnalysis,
    ) -> tuple[float, float | None, float | None]:
        """
        Calculate precise fade timing and beat positions for alignment.

        Returns:
            (crossfade_duration, fadeout_start_pos, fadein_start_pos)
            where positions are in seconds from audio start, or None if no beat alignment
        """
        # Calculate optimal crossfade bars based on BPM compatibility (e.g. 2, 4, 8 or 16 bars)
        crossfade_bars = self._calculate_optimal_crossfade_bars(fade_out_analysis, fade_in_analysis)
        beats_per_bar = 4

        # Helper function to calculate duration from beat arrays
        def calculate_beat_duration(
            fade_out_beats: Any, fade_in_beats: Any, num_beats: int
        ) -> tuple[float, float, float] | None:
            """Calculate average duration and start positions from beat arrays."""
            if len(fade_out_beats) < num_beats or len(fade_in_beats) < num_beats:
                return None
            # For single beat/bar, we can't calculate duration from beats
            if num_beats == 1:
                return None

            fade_out_slice = fade_out_beats[-num_beats:]
            fade_in_slice = fade_in_beats[:num_beats]

            fade_out_duration = fade_out_slice[-1] - fade_out_slice[0]
            fade_in_duration = fade_in_slice[-1] - fade_in_slice[0]

            # Calculate average and apply maximum limit
            optimal_duration = (fade_out_duration + fade_in_duration) / 2
            smart_duration = min(optimal_duration, MAX_SMART_CROSSFADE_DURATION)

            return smart_duration, fade_out_slice[0], fade_in_slice[0]

        # Try downbeats first for most musical timing
        downbeat_duration = calculate_beat_duration(
            fade_out_analysis.downbeats, fade_in_analysis.downbeats, crossfade_bars
        )
        if downbeat_duration:
            duration, fadeout_start, fadein_start = downbeat_duration
            self.logger.debug(
                "Timing from downbeats: %.2fs, fadeout=%.2fs, fadein=%.2fs",
                duration,
                fadeout_start,
                fadein_start,
            )
            return duration, fadeout_start, fadein_start

        # Try regular beats if downbeats insufficient
        required_beats = crossfade_bars * beats_per_bar
        beat_duration = calculate_beat_duration(
            fade_out_analysis.beats, fade_in_analysis.beats, required_beats
        )
        if beat_duration:
            duration, fadeout_start, fadein_start = beat_duration
            self.logger.debug(
                "Timing from beats: %.2fs, fadeout=%.2fs, fadein=%.2fs",
                duration,
                fadeout_start,
                fadein_start,
            )
            return duration, fadeout_start, fadein_start

        # Fallback: Calculate from BPM
        seconds_per_beat = 60.0 / fade_out_analysis.bpm
        fallback_duration = crossfade_bars * beats_per_bar * seconds_per_beat
        fallback_duration = min(fallback_duration, MAX_SMART_CROSSFADE_DURATION)

        self.logger.debug("BPM fallback timing: %.2fs (no beat alignment)", fallback_duration)
        return fallback_duration, None, None

    def _create_frequency_sweep_filter(
        self,
        input_label: str,
        output_label: str,
        sweep_type: str,  # 'lowpass' or 'highpass'
        target_freq: int,
        duration: float,
        start_time: float = 0.0,
        sweep_direction: str = "fade_in",  # 'fade_in' or 'fade_out'
        poles: int = 2,
        curve_type: str = "linear",  # 'linear', 'exponential', 'logarithmic'
    ) -> list[str]:
        """Generate FFmpeg filter chain for frequency sweep effect.

        This creates a perceptual frequency sweep by blending between filtered
        and unfiltered signals using time-varying volume controls.
        """
        # Generate unique intermediate labels
        orig_label = f"{output_label}_orig"
        filter_label = f"{output_label}_to{sweep_type[:2]}"
        filtered_label = f"{output_label}_filtered"
        orig_faded_label = f"{output_label}_orig_faded"
        filtered_faded_label = f"{output_label}_filtered_faded"

        # Generate volume expression based on curve type
        def generate_volume_expr(start: float, dur: float, direction: str, curve: str) -> str:
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

        # Determine volume ramp directions based on sweep direction
        if sweep_direction == "fade_in":
            # Fade from dry to wet (unfiltered to filtered)
            orig_direction = "down"
            filter_direction = "up"
        else:  # fade_out
            # Fade from wet to dry (filtered to unfiltered)
            orig_direction = "up"
            filter_direction = "down"

        # Build filter chain
        return [
            # Split input into two paths
            f"{input_label}asplit=2[{orig_label}][{filter_label}]",
            # Apply frequency filter to one path
            f"[{filter_label}]{sweep_type}=f={target_freq}:poles={poles}[{filtered_label}]",
            # Apply time-varying volume to original path
            (
                f"[{orig_label}]volume="
                f"{generate_volume_expr(start_time, duration, orig_direction, curve_type)}"
                f"[{orig_faded_label}]"
            ),
            # Apply time-varying volume to filtered path
            (
                f"[{filtered_label}]volume="
                f"{generate_volume_expr(start_time, duration, filter_direction, curve_type)}"
                f"[{filtered_faded_label}]"
            ),
            # Mix the two paths together
            (
                f"[{orig_faded_label}][{filtered_faded_label}]"
                f"amix=inputs=2:duration=longest:normalize=0[{output_label}]"
            ),
        ]

    def _perform_beat_alignment(
        self,
        fadeout_start_pos: float | None,
        fadein_start_pos: float | None,
        fade_out_analysis: SmartFadesAnalysis,
        fadeout_input_label: str = "[0]",
        tempo_factor: float = 1.0,
    ) -> list[str]:
        """Perform beat alignment preprocessing with custom input label."""
        # Early return if beat positions are not available
        if fadeout_start_pos is None or fadein_start_pos is None:
            return [
                f"{fadeout_input_label}anull[fadeout_beatalign]",  # codespell:ignore anull
                "[1]anull[fadein_beatalign]",  # codespell:ignore anull
            ]
        
        # Check if fadeout position is within the buffer range
        fadeout_in_buffer = False
        if fade_out_analysis.duration > MAX_SMART_CROSSFADE_DURATION:
            # Buffer contains seconds [duration-MAX, duration]
            buffer_start = fade_out_analysis.duration - MAX_SMART_CROSSFADE_DURATION
            fadeout_in_buffer = fadeout_start_pos >= buffer_start
        else:
            # Short track - entire track fits in buffer
            fadeout_in_buffer = True

        # Early return if positions are not valid for beat alignment
        if not fadeout_in_buffer or fadein_start_pos > MAX_SMART_CROSSFADE_DURATION:
            return [
                f"{fadeout_input_label}anull[fadeout_beatalign]",  # codespell:ignore anull
                "[1]anull[fadein_beatalign]",  # codespell:ignore anull
            ]
        
        # Debug logging for time stretch adjustment (for troubleshooting)
        # if tempo_factor != 1.0:
        #     if fade_out_analysis.duration > MAX_SMART_CROSSFADE_DURATION:
        #         buffer_start = fade_out_analysis.duration - MAX_SMART_CROSSFADE_DURATION
        #         original_buffer_pos = fadeout_start_pos - buffer_start
        #     else:
        #         original_buffer_pos = fadeout_start_pos
        #     adjusted_buffer_pos = original_buffer_pos / tempo_factor
        #     self.logger.debug(
        #         "Beat alignment with time stretch: buffer pos %.2fs -> %.2fs (factor=%.4f)",
        #         original_buffer_pos,
        #         adjusted_buffer_pos,
        #         tempo_factor,
        #     )
        
        # Apply beat alignment: trim fadein track to start at downbeat
        # Adjust fadein position for tempo stretching - when fadeout is stretched,
        # we need to align with the new stretched timing
        adjusted_fadein_pos = fadein_start_pos * tempo_factor
        
        return [
            f"{fadeout_input_label}anull[fadeout_beatalign]",  # codespell:ignore anull
            f"[1]atrim=start={adjusted_fadein_pos},asetpts=PTS-STARTPTS[fadein_beatalign]",
        ]

    def _create_time_stretch_filters(
        self,
        fade_out_analysis: SmartFadesAnalysis,
        fade_in_analysis: SmartFadesAnalysis,
        crossfade_duration: float,
    ) -> tuple[list[str], float]:
        """Create FFmpeg filters for gradual time stretching.

        Uses the entire buffer duration (45s) to gradually adjust tempo from
        original BPM to target BPM, ensuring the smoothest possible transition.
        Returns passthrough filter if time stretching is not needed.

        Args:
            fade_out_analysis: Analysis data for the outgoing track
            fade_in_analysis: Analysis data for the incoming track
            crossfade_duration: Duration of the crossfade in seconds

        Returns:
            Tuple of (FFmpeg filter strings, tempo factor for position adjustment)
        """
        # Check if time stretching should be applied (BPM difference < 3%)
        original_bpm = fade_out_analysis.bpm
        target_bpm = fade_in_analysis.bpm
        bpm_ratio = target_bpm / original_bpm
        bpm_diff_percent = abs(1.0 - bpm_ratio) * 100
        
        # If no time stretching needed, return passthrough filter and no tempo change
        if not (0.1 < bpm_diff_percent < 3.0):
            return ["[0]anull[fadeout_stretched]"], 1.0  # codespell:ignore anull
        
        # Log that we're applying time stretching
        self.logger.debug(
            "Time stretch: %.1f%% BPM diff, adjusting %.1f -> %.1f BPM over buffer",
            bpm_diff_percent,
            original_bpm,
            target_bpm,
        )
        
        # Calculate the tempo change factor
        # atempo accepts values between 0.5 and 2.0 (can be chained for larger changes)
        tempo_factor = bpm_ratio
        buffer_duration = MAX_SMART_CROSSFADE_DURATION  # 45 seconds

        # For BPM differences < 3%, tempo_factor will be between 0.97 and 1.03
        # This is well within atempo's range

        # If the crossfade takes up most of the buffer, use simple linear stretch
        if buffer_duration - crossfade_duration < 5.0:
            self.logger.debug(
                "Time stretch filter (linear): %.1f BPM -> %.1f BPM (factor=%.4f)",
                original_bpm,
                target_bpm,
                tempo_factor,
            )
            return [f"[0]atempo={tempo_factor:.6f}[fadeout_stretched]"], tempo_factor

        # Implement segmented time stretching with exponential curve
        num_segments = 4  # Balance between smoothness and filter complexity
        filters = []

        # Split the input into segments
        filters.append(
            f"[0]asplit={num_segments}" + "".join(f"[seg{i}]" for i in range(num_segments))
        )

        # Process each segment with progressively more tempo adjustment
        for i in range(num_segments):
            # Calculate segment timing
            segment_start = (i * buffer_duration) / num_segments
            segment_end = ((i + 1) * buffer_duration) / num_segments

            # Calculate progress through the buffer (0 to 1)
            progress = (i + 0.5) / num_segments  # Use midpoint of segment

            # Apply exponential easing curve (ease-in-out cubic)
            # This creates minimal change at start, accelerating in middle, decelerating at end
            if progress < 0.5:
                # First half: ease in (slow start)
                eased_progress = 4 * progress * progress * progress
            else:
                # Second half: ease out (slow finish)
                p = 2 * progress - 2
                eased_progress = 1 + p * p * p / 2

            # Calculate tempo for this segment
            segment_tempo = 1.0 + (tempo_factor - 1.0) * eased_progress

            # Clamp to atempo's valid range (should never exceed for < 3% changes)
            segment_tempo = max(0.5, min(2.0, segment_tempo))

            # Trim segment and apply tempo adjustment
            filters.append(
                f"[seg{i}]atrim=start={segment_start:.3f}:end={segment_end:.3f},"
                f"asetpts=PTS-STARTPTS,atempo={segment_tempo:.6f}[seg{i}_stretched]"
            )

            self.logger.debug(
                "Segment %d: %.1f-%.1fs, tempo factor=%.4f (%.1f%% of change)",
                i + 1,
                segment_start,
                segment_end,
                segment_tempo,
                eased_progress * 100,
            )

        # Concatenate all stretched segments
        concat_inputs = "".join(f"[seg{i}_stretched]" for i in range(num_segments))
        filters.append(f"{concat_inputs}concat=n={num_segments}:v=0:a=1[fadeout_stretched]")

        self.logger.debug(
            "Time stretch filter (segmented): %.1f BPM -> %.1f BPM (factor=%.4f) with %d segments",
            original_bpm,
            target_bpm,
            tempo_factor,
            num_segments,
        )

        return filters, tempo_factor

    def _determine_dj_style_mode(
        self,
        fade_out_analysis: SmartFadesAnalysis,
        fade_in_analysis: SmartFadesAnalysis) -> DJStyleMode:
        """Determine DJ style mode based on user settings or defaults."""        
        avg_bpm = (fade_in_analysis.bpm + fade_out_analysis.bpm) / 2
        effective_bpm_ratio = fade_in_analysis.bpm / fade_out_analysis.bpm

        # Always use CLASSIC for slower tempos (hip-hop, R&B, downtempo)
        if avg_bpm <= 110:
            return DJStyleMode.CLASSIC
        # Use MODERN only for similar BPMs at dance music tempos (house, techno, trance)
        if 110 < avg_bpm <= 145 and abs(effective_bpm_ratio - 1.0) < 0.1:
            return DJStyleMode.MODERN
        # Default to CLASSIC for mismatched BPMs to prevent frequency clashing
        return DJStyleMode.CLASSIC

    def _dj_classic(
        self,
        fade_out_analysis: SmartFadesAnalysis,
        fade_in_analysis: SmartFadesAnalysis,
        fade_out_label: str,
        fade_in_label: str,
        crossfade_duration: float,
    ) -> list[str]:
        """Create gradual complementary filters using frequency sweeps for smooth transitions."""
        # Calculate crossover frequency based on average BPM
        avg_bpm = (fade_out_analysis.bpm + fade_in_analysis.bpm) / 2
        bpm_ratio = fade_in_analysis.bpm / fade_out_analysis.bpm

        # Linear interpolation: 90 BPM -> 800Hz, 140 BPM -> 1200Hz
        crossover_freq = int(np.clip(800 + (avg_bpm - 90) * 8, 800, 1200))

        # Reduce frequency for mismatched BPMs
        if abs(bpm_ratio - 1.0) > 0.3:
            crossover_freq = int(crossover_freq * 0.8)

        # Asymmetric EQ durations for better musical flow
        fadeout_eq_duration = min(crossfade_duration * 2.0, 8.0)   # Gradual high-pass for outgoing
        fadein_eq_duration = min(crossfade_duration * 1.5, 6.0)    # Quicker low-pass removal for incoming

        # Calculate when the EQ sweep should start
        # The crossfade always happens at the END of the buffer, EQ ramps up before it
        fadeout_eq_start = max(0.0, MAX_SMART_CROSSFADE_DURATION - fadeout_eq_duration)

        self.logger.debug(
            "DJ Classic: EQ: crossover=%dHz, fadeout=%.1fs, fadein=%.1fs, BPM avg=%.1f ratio=%.2f",
            crossover_freq,
            fadeout_eq_duration,
            fadein_eq_duration,
            avg_bpm,
            bpm_ratio,
        )

        # Use the new frequency sweep method for fadeout (unfiltered → high-pass)
        fadeout_filters = self._create_frequency_sweep_filter(
            input_label=fade_out_label,
            output_label="fadeout_eq",
            sweep_type="highpass",
            target_freq=crossover_freq,
            duration=fadeout_eq_duration,
            start_time=fadeout_eq_start,
            sweep_direction="fade_in",  # Fade IN the highpass effect
            poles=1,
        )

        # Use the new frequency sweep method for fadein (low-pass → unfiltered)
        fadein_filters = self._create_frequency_sweep_filter(
            input_label=fade_in_label,
            output_label="fadein_eq",
            sweep_type="lowpass",
            target_freq=crossover_freq,
            duration=fadein_eq_duration,
            start_time=0,
            sweep_direction="fade_out",  # Fade OUT the lowpass effect
            poles=1,
        )

        return fadeout_filters + fadein_filters

    def _dj_modern(
        self,
        fade_out_analysis: SmartFadesAnalysis,
        fade_in_analysis: SmartFadesAnalysis,
        fade_out_label: str,
        fade_in_label: str,
        crossfade_duration: float,
    ) -> list[str]:
        """Create DJ-style complementary filters using frequency sweeps for smooth transitions."""
        # Calculate target frequency based on average BPM (for DJ software style)
        avg_bpm = (fade_out_analysis.bpm + fade_in_analysis.bpm) / 2
        bpm_ratio = fade_in_analysis.bpm / fade_out_analysis.bpm

        # For swapped filters (DJ software style): 90 BPM -> 1500Hz, 140 BPM -> 2500Hz
        crossover_freq = int(np.clip(1500 + (avg_bpm - 90) * 20, 1500, 3000))

        # Adjust for BPM mismatch
        if abs(bpm_ratio - 1.0) > 0.3:
            crossover_freq = int(crossover_freq * 0.85)

        # Asymmetric EQ durations for better musical flow
        fadeout_eq_duration = max(crossfade_duration * 2.5, 8.0)  # Extended lowpass effect
        fadein_eq_duration = crossfade_duration # Quick highpass removal

        # Calculate when the EQ sweep should start
        # The crossfade always happens at the END of the buffer, regardless of beat alignment
        fadeout_eq_start = max(0, MAX_SMART_CROSSFADE_DURATION - fadeout_eq_duration)

        self.logger.debug(
            "DJ Modern: EQ: crossover=%dHz, EQ fadeout duration=%.1fs"
            " EQ fadein duration=%.1fs, BPM=%.1f BPM ratio=%.2f",
            crossover_freq,
            fadeout_eq_duration,
            fadein_eq_duration,
            avg_bpm,
            bpm_ratio,
        )

        # Use the new frequency sweep method for fadeout (unfiltered → low-pass)
        fadeout_filters = self._create_frequency_sweep_filter(
            input_label=fade_out_label,
            output_label="fadeout_eq",
            sweep_type="lowpass",
            target_freq=crossover_freq,
            duration=fadeout_eq_duration,
            start_time=fadeout_eq_start,
            sweep_direction="fade_in",  # Fade IN the lowpass effect
            poles=1,
            curve_type="exponential",  # Use exponential curve for smoother DJ-style transitions
        )

        # Use the new frequency sweep method for fadein (high-pass → unfiltered)
        fadein_filters = self._create_frequency_sweep_filter(
            input_label=fade_in_label,
            output_label="fadein_eq",
            sweep_type="highpass",
            target_freq=crossover_freq,
            duration=fadein_eq_duration,
            start_time=0,
            sweep_direction="fade_out",  # Fade OUT the highpass effect
            poles=1,
            curve_type="exponential",  # Use exponential curve for smoother DJ-style transitions
        )

        return fadeout_filters + fadein_filters

    # FALLBACK DEFAULT CROSSFADE
    async def _default_crossfade(
        self,
        fade_in_part: bytes,
        fade_out_part: bytes,
        pcm_format: AudioFormat,
        crossfade_duration: int = 10,
    ) -> bytes:
        """Apply a standard crossfade without smart analysis."""
        self.logger.debug(
            "Applying standard crossfade of %ds (no beat analysis)", crossfade_duration
        )
        crossfade_size = int(pcm_format.pcm_sample_size * crossfade_duration)
        # Pre-crossfade: outgoing track minus the crossfaded portion
        pre_crossfade = fade_out_part[:-crossfade_size]
        # Crossfaded portion: user's configured duration
        crossfaded_section = await crossfade_pcm_parts(
            fade_in_part[:crossfade_size],
            fade_out_part[-crossfade_size:],
            pcm_format=pcm_format,
        )
        # Post-crossfade: incoming track minus the crossfaded portion
        post_crossfade = fade_in_part[crossfade_size:]
        # Full result: everything concatenated
        return pre_crossfade + crossfaded_section + post_crossfade
