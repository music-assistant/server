"""Smart Fades - Object-oriented implementation with intelligent fades and adaptive filtering."""

# TODO: Figure out if we can achieve shared buffer with StreamController on full
# current and next track for more EQ options.
# TODO: Refactor the Analyzer into a metadata controller after we have split the controllers
# TODO: Refactor the Mixer into a stream controller after we have split the controllers
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import aiofiles
import librosa
import numpy as np
import shortuuid
from mashumaro import DataClassDictMixin
from mashumaro.config import BaseConfig
from music_assistant_models.enums import ContentType, MediaType
from music_assistant_models.media_items import AudioFormat

from music_assistant.constants import VERBOSE_LOG_LEVEL
from music_assistant.helpers.audio import crossfade_pcm_parts, get_media_stream
from music_assistant.helpers.process import communicate
from music_assistant.helpers.util import remove_file

if TYPE_CHECKING:
    from music_assistant_models.streamdetails import StreamDetails

    from music_assistant.mass import MusicAssistant

MAX_SMART_CROSSFADE_DURATION = 45
ANALYSIS_FPS = 100
ANALYSIS_PCM_FORMAT = AudioFormat(
    content_type=ContentType.PCM_F32LE, sample_rate=44100, bit_depth=32, channels=2
)


class SmartFadesMode(StrEnum):
    """Smart fades modes."""

    SMART_FADES = "smart_fades"  # Use smart fades with beat matching and EQ filters
    DEFAULT_CROSSFADE = "default_crossfade"  # Use standard crossfade only
    DISABLED = "disabled"  # No crossfade


class SmartFadesDJStyleMode(StrEnum):
    """DJ transition style modes for Smartg."""

    AUTO = "auto"  # Automatically select based on BPM compatibility
    CLASSIC = "classic"  # Traditional HP/LP complementary filters
    MODERN = "modern"  # Swapped LP/HP filters
    DISABLED = "disabled"  # No frequency filtering, volume crossfade only


@dataclass
class SmartFadesAnalysis(DataClassDictMixin):
    """Beat tracking analysis data for BPM matching crossfade."""

    bpm: float
    beats: np.ndarray[Any, np.dtype[np.float64]]  # Beat positions
    downbeats: np.ndarray[Any, np.dtype[np.float64]]  # Downbeat positions
    confidence: float  # Analysis confidence score 0-1
    duration: float = 0.0  # Duration of the track in seconds

    class Config(BaseConfig):  # noqa: D106
        serialization_strategy = {
            np.ndarray: {"serialize": lambda x: x.tolist(), "deserialize": lambda x: np.array(x)}
        }


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
        """Perform beat analysis using librosa."""
        try:
            # Convert to mono for analysis (average stereo channels)
            audio_mono = np.mean(audio_array, axis=1).astype(np.float32)

            sample_rate = ANALYSIS_PCM_FORMAT.sample_rate
            tempo, beats_array = librosa.beat.beat_track(
                y=audio_mono,
                sr=sample_rate,
                units="time",
            )
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

            # Store complete track analysis
            track_duration = len(audio_mono) / sample_rate

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
            audio_array = self._prepare_audio_for_librosa(audio_data)
            return await asyncio.to_thread(self._librosa_beat_analysis, audio_array)
        except Exception as e:
            self.logger.exception("Beat tracking analysis failed: %s", e)
            return None

    def _prepare_audio_for_librosa(self, pcm_data: bytes) -> np.ndarray[Any, np.dtype[np.float32]]:
        """Convert PCM bytes to numpy array for librosa."""
        audio_array = np.frombuffer(pcm_data, dtype=np.float32)

        # Reshape based on the number of channels specified in ANALYSIS_PCM_FORMAT
        channels = ANALYSIS_PCM_FORMAT.channels

        # Ensure we have complete frames (samples must be divisible by channel count)
        if len(audio_array) % channels != 0:
            # Pad to complete the last frame (edge case for truncated audio)
            padding_needed = channels - (len(audio_array) % channels)
            self.logger.warning(
                "Incomplete audio frame detected, padding %d samples", padding_needed
            )
            audio_array = np.pad(audio_array, (0, padding_needed), constant_values=0.0)

        # Reshape to (num_frames, channels)
        return audio_array.reshape(-1, channels)


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
        dj_style_mode: SmartFadesDJStyleMode = SmartFadesDJStyleMode.AUTO,
        mode: SmartFadesMode = SmartFadesMode.SMART_FADES,
    ) -> bytes:
        """Apply crossfade with internal state management and smart/standard fallback logic."""
        if (
            fade_out_analysis
            and fade_in_analysis
            and fade_out_analysis.confidence > 0.3
            and fade_in_analysis.confidence > 0.3
            and mode == SmartFadesMode.SMART_FADES
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
        dj_style_mode: SmartFadesDJStyleMode = SmartFadesDJStyleMode.AUTO,
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
        dj_style_mode: SmartFadesDJStyleMode = SmartFadesDJStyleMode.AUTO,
    ) -> list[str]:
        """Create smart fade filters with perfect timing and adaptive filtering."""
        # Calculate optimal crossfade bars that fit in available buffer
        crossfade_bars = self._calculate_optimal_crossfade_bars(fade_out_analysis, fade_in_analysis)

        # Calculate beat positions for the selected bar count
        fadeout_start_pos, fadein_start_pos = self._calculate_optimal_fade_timing(
            fade_out_analysis, fade_in_analysis, crossfade_bars
        )

        # Log the final selected timing
        if fadeout_start_pos is not None and fadein_start_pos is not None:
            self.logger.debug(
                "Beat timing selected: fadeout=%.2fs, fadein=%.2fs (%d bars)",
                fadeout_start_pos,
                fadein_start_pos,
                crossfade_bars,
            )

        filters: list[str] = []

        time_stretch_filters, tempo_factor = self._create_time_stretch_filters(
            fade_out_analysis=fade_out_analysis,
            fade_in_analysis=fade_in_analysis,
            crossfade_bars=crossfade_bars,
        )
        filters.extend(time_stretch_filters)

        crossfade_duration = self._calculate_crossfade_duration(
            crossfade_bars=crossfade_bars,
            fade_in_analysis=fade_in_analysis,
        )

        # Check if we would have enough audio after beat alignment for the crossfade
        if (
            fadein_start_pos is not None
            and fadein_start_pos + crossfade_duration > MAX_SMART_CROSSFADE_DURATION
        ):
            self.logger.debug(
                "Skipping beat alignment: not enough audio after trim (%.1fs + %.1fs > %.1fs)",
                fadein_start_pos,
                crossfade_duration,
                MAX_SMART_CROSSFADE_DURATION,
            )
            # Skip beat alignment
            fadein_start_pos = None

        beat_align_filters = self._perform_beat_alignment(
            fadein_start_pos=fadein_start_pos,
            tempo_factor=tempo_factor,
            fadeout_input_label="[fadeout_stretched]",
            fadein_input_label="[1]",
        )
        filters.extend(beat_align_filters)

        self.logger.debug(
            "Smart fade: out_bpm=%.1f, in_bpm=%.1f, %d bars, crossfade: %.2fs, dj_mode=%s%s",
            fade_out_analysis.bpm,
            fade_in_analysis.bpm,
            crossfade_bars,
            crossfade_duration,
            dj_style_mode,
            ", beat-aligned" if fadein_start_pos else "",
        )

        if dj_style_mode == SmartFadesDJStyleMode.AUTO:
            dj_style_mode = self._determine_dj_style_mode(fade_out_analysis, fade_in_analysis)

        if dj_style_mode == SmartFadesDJStyleMode.DISABLED:
            # No frequency filtering, just pass through
            filters.extend(
                [
                    "[fadeout_beatalign]anull[fadeout_eq]",  # codespell:ignore anull
                    "[fadein_beatalign]anull[fadein_eq]",  # codespell:ignore anull
                ]
            )
        elif dj_style_mode == SmartFadesDJStyleMode.MODERN:
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

        # Apply linear crossfade for now since we use EQ sweeps for smoothness
        filters.append(f"[fadeout_eq][fadein_eq]acrossfade=d={crossfade_duration}")

        return filters

    def _calculate_crossfade_duration(
        self,
        crossfade_bars: int,
        fade_in_analysis: SmartFadesAnalysis,
    ) -> float:
        """Calculate final crossfade duration based on musical bars and BPM."""
        # Calculate crossfade duration based on incoming track's BPM
        # This ensures a musically consistent crossfade length regardless of beat positions
        beats_per_bar = 4
        seconds_per_beat = 60.0 / fade_in_analysis.bpm
        musical_duration = crossfade_bars * beats_per_bar * seconds_per_beat

        # Apply buffer constraint
        actual_duration = min(musical_duration, MAX_SMART_CROSSFADE_DURATION)

        # Log if we had to constrain the duration
        if musical_duration > MAX_SMART_CROSSFADE_DURATION:
            self.logger.debug(
                "Constraining crossfade duration from %.1fs to %.1fs (buffer limit)",
                musical_duration,
                actual_duration,
            )

        return actual_duration

    def _calculate_optimal_crossfade_bars(
        self, fade_out_analysis: SmartFadesAnalysis, fade_in_analysis: SmartFadesAnalysis
    ) -> int:
        """Calculate optimal crossfade bars that fit in available buffer."""
        bpm_in = fade_in_analysis.bpm
        bpm_out = fade_out_analysis.bpm
        bpm_diff_percent = abs(1.0 - bpm_in / bpm_out) * 100

        # Calculate ideal bars based on BPM compatibility
        if bpm_diff_percent < 1.5:
            ideal_bars = 16
        elif bpm_diff_percent < 3.0:
            ideal_bars = 8
        elif bpm_diff_percent < 8.0:
            ideal_bars = 4
        elif bpm_diff_percent < 25.0:
            ideal_bars = 2
        else:
            ideal_bars = 1

        # We could encounter songs that have a long athmospheric intro without any downbeats
        # In those cases, we need to reduce the bars until it fits in the fadein buffer.
        for bars in [ideal_bars, 8, 4, 2, 1]:
            if bars > ideal_bars:
                continue  # Skip bars longer than optimal

            fadeout_start_pos, fadein_start_pos = self._calculate_optimal_fade_timing(
                fade_out_analysis, fade_in_analysis, bars
            )
            if fadeout_start_pos is None or fadein_start_pos is None:
                continue

            # Calculate what the duration would be
            test_duration = self._calculate_crossfade_duration(
                crossfade_bars=bars,
                fade_in_analysis=fade_in_analysis,
            )

            # Check if it fits in fadein buffer
            fadein_buffer = MAX_SMART_CROSSFADE_DURATION - fadein_start_pos
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

    def _calculate_optimal_fade_timing(
        self,
        fade_out_analysis: SmartFadesAnalysis,
        fade_in_analysis: SmartFadesAnalysis,
        crossfade_bars: int,
    ) -> tuple[float | None, float | None]:
        """Calculate beat positions for alignment."""
        beats_per_bar = 4

        # Helper function to calculate beat positions from beat arrays
        def calculate_beat_positions(
            fade_out_beats: Any, fade_in_beats: Any, num_beats: int
        ) -> tuple[float, float] | None:
            """Calculate start positions from beat arrays with phantom downbeat support."""
            if len(fade_out_beats) < num_beats or len(fade_in_beats) < num_beats:
                return None

            fade_out_slice = fade_out_beats[-num_beats:]

            # For fadein, find the earliest downbeat that fits in buffer
            fade_in_slice = fade_in_beats[:num_beats]
            fadein_start_pos = fade_in_slice[0]

            fadeout_start_pos = fade_out_slice[0]
            return fadeout_start_pos, fadein_start_pos

        # Try downbeats first for most musical timing
        downbeat_positions = calculate_beat_positions(
            fade_out_analysis.downbeats, fade_in_analysis.downbeats, crossfade_bars
        )
        if downbeat_positions:
            return downbeat_positions

        # Try regular beats if downbeats insufficient
        required_beats = crossfade_bars * beats_per_bar
        beat_positions = calculate_beat_positions(
            fade_out_analysis.beats, fade_in_analysis.beats, required_beats
        )
        if beat_positions:
            return beat_positions

        # Fallback: No beat alignment possible
        self.logger.debug("No beat alignment possible (insufficient beats)")
        return None, None

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
        """Generate FFmpeg filters for frequency sweep effect."""
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
        fadein_start_pos: float | None,
        tempo_factor: float,
        fadeout_input_label: str = "[0]",
        fadein_input_label: str = "[1]",
    ) -> list[str]:
        """Perform beat alignment preprocessing."""
        # Just relabel in case we cannot perform beat alignment
        if fadein_start_pos is None:
            return [
                f"{fadeout_input_label}anull[fadeout_beatalign]",  # codespell:ignore anull
                f"{fadein_input_label}anull[fadein_beatalign]",  # codespell:ignore anull
            ]

        # When time stretching is applied, adjust the fadein start position
        # The fadein needs to start later to sync with the stretched fadeout beats
        adjusted_fadein_start_pos = fadein_start_pos * tempo_factor

        # Apply beat alignment: fadeout passes through, fadein trims to adjusted position
        return [
            f"{fadeout_input_label}anull[fadeout_beatalign]",  # codespell:ignore anull
            f"{fadein_input_label}atrim=start={adjusted_fadein_start_pos},asetpts=PTS-STARTPTS[fadein_beatalign]",
        ]

    def _create_time_stretch_filters(
        self,
        fade_out_analysis: SmartFadesAnalysis,
        fade_in_analysis: SmartFadesAnalysis,
        crossfade_bars: int,
    ) -> tuple[list[str], float]:
        """Create FFmpeg filters to gradually adjust tempo from original BPM to target BPM."""
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

        # Calculate expected crossfade duration from bars for comparison
        beats_per_bar = 4
        seconds_per_beat = 60.0 / original_bpm
        expected_crossfade_duration = crossfade_bars * beats_per_bar * seconds_per_beat

        # For BPM differences < 3%, tempo_factor will be between 0.97 and 1.03
        # This is well within atempo's range

        # Validate tempo factor is within ffmpeg's atempo range
        if not 0.5 <= tempo_factor <= 2.0:
            self.logger.warning(
                "Tempo factor %.4f out of range [0.5, 2.0], skipping time stretch",
                tempo_factor,
            )
            return ["[0]anull[fadeout_stretched]"], 1.0  # codespell:ignore anull

        # If the crossfade takes up most of the buffer, use simple linear stretch
        if buffer_duration - expected_crossfade_duration < 5.0:
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
        self, fade_out_analysis: SmartFadesAnalysis, fade_in_analysis: SmartFadesAnalysis
    ) -> SmartFadesDJStyleMode:
        """Determine DJ style mode based on user settings or defaults."""
        avg_bpm = (fade_in_analysis.bpm + fade_out_analysis.bpm) / 2
        effective_bpm_ratio = fade_in_analysis.bpm / fade_out_analysis.bpm

        # Always use CLASSIC for slower tempos
        if avg_bpm <= 110:
            return SmartFadesDJStyleMode.CLASSIC
        # Use MODERN only for similar BPMs at dance music tempos (house, techno, trance)
        if 110 < avg_bpm <= 145 and abs(effective_bpm_ratio - 1.0) < 0.1:
            return SmartFadesDJStyleMode.MODERN
        # Default to CLASSIC for mismatched BPMs to prevent frequency clashing
        return SmartFadesDJStyleMode.CLASSIC

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

        # Gradual high-pass for outgoing
        fadeout_eq_duration = min(crossfade_duration * 2.0, 8.0)
        # Quicker low-pass removal for incoming track
        fadein_eq_duration = min(crossfade_duration * 1.5, 6.0)

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
            sweep_direction="fade_in",
            poles=1,
            curve_type="linear",
        )

        # Use the new frequency sweep method for fadein (low-pass → unfiltered)
        fadein_filters = self._create_frequency_sweep_filter(
            input_label=fade_in_label,
            output_label="fadein_eq",
            sweep_type="lowpass",
            target_freq=crossover_freq,
            duration=fadein_eq_duration,
            start_time=0,
            sweep_direction="fade_out",
            poles=1,
            curve_type="linear",
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
        # Calculate target frequency based on average BPM
        avg_bpm = (fade_out_analysis.bpm + fade_in_analysis.bpm) / 2
        bpm_ratio = fade_in_analysis.bpm / fade_out_analysis.bpm

        # For swapped filters in DJ style: 90 BPM -> 1500Hz, 140 BPM -> 2500Hz
        crossover_freq = int(np.clip(1500 + (avg_bpm - 90) * 20, 1500, 2500))

        # Adjust for BPM mismatch
        if abs(bpm_ratio - 1.0) > 0.3:
            crossover_freq = int(crossover_freq * 0.85)

        # Asymmetric EQ durations for better musical flow
        # Extended lowpass effect
        fadeout_eq_duration = min(max(crossfade_duration * 2.5, 8.0), MAX_SMART_CROSSFADE_DURATION)
        # Quicker highpass removal
        fadein_eq_duration = crossfade_duration

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

        # fadeout (unfiltered → low-pass)
        fadeout_filters = self._create_frequency_sweep_filter(
            input_label=fade_out_label,
            output_label="fadeout_eq",
            sweep_type="lowpass",
            target_freq=crossover_freq,
            duration=fadeout_eq_duration,
            start_time=fadeout_eq_start,
            sweep_direction="fade_in",
            poles=1,
            curve_type="exponential",  # Use exponential curve for smoother DJ-style transitions
        )

        # fadein (high-pass → unfiltered)
        fadein_filters = self._create_frequency_sweep_filter(
            input_label=fade_in_label,
            output_label="fadein_eq",
            sweep_type="highpass",
            target_freq=crossover_freq,
            duration=fadein_eq_duration,
            start_time=0,
            sweep_direction="fade_out",
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
        self.logger.debug("Applying standard crossfade of %ds", crossfade_duration)
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
