"""Smart Fades - Object-oriented implementation with intelligent fades and adaptive filtering."""

from __future__ import annotations

import asyncio
import logging
import multiprocessing
import time
from typing import TYPE_CHECKING

import aiofiles
import madmom
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
        self.logger.info("Starting beat analysis for track : %s", stream_details_name)
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
                "Smart fades analysis completed for %s: BPM=%.1f, confidence=%.2f",
                stream_details_name,
                analysis.bpm,
                analysis.confidence,
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
        """Analyze track for beat tracking."""
        try:
            audio_array = self._prepare_audio_for_madmom(audio_data)
            analysis = await asyncio.to_thread(self._madmom_beat_analysis, audio_array)
            if analysis:
                # Set duration from audio data
                analysis.duration = len(audio_data) / ANALYSIS_PCM_FORMAT.pcm_sample_size
            return analysis
        except Exception as e:
            self.logger.exception("Beat tracking analysis failed: %s", e)
            return None

    def _prepare_audio_for_madmom(self, pcm_data: bytes) -> np.ndarray:
        """Convert PCM bytes to numpy array for madmom."""
        # Convert stereo 32-bit float PCM to mono numpy array
        audio_array = np.frombuffer(pcm_data, dtype=np.float32)
        if len(audio_array) % 2 == 0:  # Stereo to mono
            audio_array = audio_array.reshape(-1, 2).mean(axis=1)

        self.logger.debug(
            "Prepared %.2fs audio (%d samples)",
            len(pcm_data) / (ANALYSIS_PCM_FORMAT.sample_rate * 8),  # 4 bytes * 2 channels
            len(audio_array),
        )
        return audio_array

    def _madmom_beat_analysis(self, audio_array: np.ndarray) -> SmartFadesAnalysis:
        """Perform beat analysis using madmom."""
        # Use most cores but leave some headroom for the main app
        num_cores = max(1, multiprocessing.cpu_count() - 2)

        # RNN Beat Processing
        start_time = time.perf_counter()
        beat_processor = madmom.features.beats.RNNBeatProcessor()
        beat_activations = beat_processor.process(
            audio_array, sample_rate=ANALYSIS_PCM_FORMAT.sample_rate
        )
        rnn_duration = time.perf_counter() - start_time
        self.logger.log(VERBOSE_LOG_LEVEL, "RNNBeatProcessor.process() took %.3fs", rnn_duration)

        # Beat Tracking
        start_time = time.perf_counter()
        beat_tracker = madmom.features.beats.BeatTrackingProcessor(fps=ANALYSIS_FPS)
        beats = beat_tracker.process(beat_activations)
        tracking_duration = time.perf_counter() - start_time
        self.logger.log(
            VERBOSE_LOG_LEVEL, "BeatTrackingProcessor.process() took %.3fs", tracking_duration
        )

        # Downbeat tracking
        try:
            # RNN Downbeat Processing
            start_time = time.perf_counter()
            downbeat_processor = madmom.features.downbeats.RNNDownBeatProcessor()
            downbeat_activations = downbeat_processor.process(
                audio_array, sample_rate=ANALYSIS_PCM_FORMAT.sample_rate
            )
            rnn_downbeat_duration = time.perf_counter() - start_time
            self.logger.log(
                VERBOSE_LOG_LEVEL,
                "RNNDownBeatProcessor.process() took %.3fs",
                rnn_downbeat_duration,
            )

            # DBN Downbeat Tracking (with threading)
            start_time = time.perf_counter()
            downbeat_tracker = madmom.features.downbeats.DBNDownBeatTrackingProcessor(
                beats_per_bar=4, fps=ANALYSIS_FPS, num_threads=num_cores
            )
            downbeat_output = downbeat_tracker.process(downbeat_activations)
            dbn_downbeat_duration = time.perf_counter() - start_time
            self.logger.log(
                VERBOSE_LOG_LEVEL,
                "DBNDownBeatTrackingProcessor.process() took %.3fs",
                dbn_downbeat_duration,
            )

            # Extract only the downbeats (beat_number == 1)
            if len(downbeat_output) > 0 and downbeat_output.ndim == 2:
                downbeats = downbeat_output[downbeat_output[:, 1] == 1][:, 0]
            else:
                # Fallback if output format is unexpected
                downbeats = beats[::4] if len(beats) >= 4 else beats

        except Exception as e:
            self.logger.warning("Downbeat analysis failed: %s", e)
            # Fallback: estimate downbeats every 4 beats
            downbeats = beats[::4] if len(beats) >= 4 else beats

        # BPM estimation from beats
        if len(beats) > 1:
            beat_intervals = np.diff(beats)
            avg_interval = np.mean(beat_intervals)
            raw_bpm = float(60.0 / avg_interval) if avg_interval > 0 else 120.0

            # Double BPM if detected as half-time (too slow)
            bpm = raw_bpm * 2 if raw_bpm < 80 and 90 <= raw_bpm * 2 <= 180 else raw_bpm

            self.logger.debug(
                "BPM: %d beats, interval=%.3fs, raw=%.1f, final=%.1f",
                len(beats),
                avg_interval,
                raw_bpm,
                bpm,
            )
        else:
            bpm = 120.0  # Default BPM

        # Confidence based on beat consistency (coefficient of variation)
        confidence = 0.0
        if len(beats) > 4:
            beat_intervals = np.diff(beats)
            if (avg_interval := np.mean(beat_intervals)) > 0:
                confidence = float(1.0 - min(np.std(beat_intervals) / avg_interval, 1.0))

        analysis = SmartFadesAnalysis(
            bpm=bpm,
            beats=beats,
            downbeats=downbeats,
            confidence=confidence,
        )

        self.logger.info(
            "Analysis: BPM=%.1f, %d beats, %d downbeats, conf=%.2f",
            bpm,
            len(beats),
            len(downbeats),
            confidence,
        )

        return analysis


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
    ) -> bytes:
        """Apply crossfade with internal state management and smart/standard fallback logic."""
        # Decide between smart crossfade and standard crossfade
        if (
            fade_out_analysis
            and fade_in_analysis
            and fade_out_analysis.confidence > 0.3
            and fade_in_analysis.confidence > 0.3
        ):
            # Calculate optimal crossfade bars based on BPM compatibility
            optimal_bars = self._calculate_optimal_crossfade_bars(
                fade_out_analysis.bpm, fade_in_analysis.bpm
            )

            # Use smart crossfade with BPM matching

            try:
                return await self._apply_smart_crossfade(
                    fade_out_analysis,
                    fade_in_analysis,
                    fade_out_part,
                    fade_in_part,
                    pcm_format,
                    optimal_bars,
                )
            except Exception as e:
                self.logger.warning(
                    "Smart crossfade failed: %s, falling back to standard crossfade", e
                )

        # Use standard crossfade
        return await self.default_crossfade(
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
        crossfade_bars: int,
    ) -> bytes:
        """Apply smart crossfade with beat-perfect timing and adaptive filtering."""
        self.logger.info(
            "Applying smart fade: fade_out_bpm=%.1f, fade_in_bpm=%.1f, %d bars",
            fade_out_analysis.bpm,
            fade_in_analysis.bpm,
            crossfade_bars,
        )

        # Calculate optimal fade duration using beat analysis
        optimal_duration, fadeout_start_pos, fadein_start_pos = self._calculate_optimal_fade_timing(
            fade_out_analysis, fade_in_analysis, crossfade_bars
        )

        self.logger.debug(
            "Smart fade: %.2fs, %d bars%s",
            optimal_duration,
            crossfade_bars,
            ", beat-aligned" if fadeout_start_pos else "",
        )

        # Write the fade_out_part to a temporary file (revert to full buffer approach)
        fadeout_filename = f"/tmp/{shortuuid.random(20)}.pcm"  # noqa: S108
        async with aiofiles.open(fadeout_filename, "wb") as outfile:
            await outfile.write(fade_out_part)

        # Build FFmpeg command for enhanced smart fade
        args = [
            # Generic args
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            # fadeout part (as file)
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
            fadeout_filename,
            # fade_in part (stdin)
            "-acodec",
            pcm_format.content_type.name.lower(),
            "-ac",
            str(pcm_format.channels),
            "-channel_layout",
            "mono" if pcm_format.channels == 1 else "stereo",
            "-ar",
            str(pcm_format.sample_rate),
            "-f",
            pcm_format.content_type.value,
            "-i",
            "-",
        ]

        # Build enhanced filter chain with extended EQ duration
        filter_complex, _ = self._create_enhanced_smart_fade_filters(
            fade_out_analysis,
            fade_in_analysis,
            "[0]",
            "[1]",
            optimal_duration,
            fadeout_start_pos,
            fadein_start_pos,
        )

        args.extend(
            [
                "-filter_complex",
                ";".join(filter_complex),
                "-acodec",
                pcm_format.content_type.name.lower(),
                "-ac",
                str(pcm_format.channels),
                "-channel_layout",
                "mono" if pcm_format.channels == 1 else "stereo",
                "-ar",
                str(pcm_format.sample_rate),
                "-f",
                pcm_format.content_type.value,
                "-",
            ]
        )

        # Debug log the full FFmpeg command and filter complex
        self.logger.debug("FFmpeg command: %s", " ".join(args))
        self.logger.debug("Filter complex: %s", ";".join(filter_complex))

        # Execute the enhanced smart fade with full buffer
        _, raw_crossfade_output, stderr = await communicate(args, fade_in_part)
        await remove_file(fadeout_filename)

        # Use full FFmpeg output directly (includes post-crossfade audio naturally)
        if raw_crossfade_output:
            self.logger.info(
                "Smart fade successful: duration=%.2fs, full buffer processing",
                optimal_duration,
            )
            return raw_crossfade_output
        else:
            stderr_msg = stderr.decode() if stderr else "(no stderr output)"
            raise RuntimeError(f"Smart crossfade failed. FFmpeg stderr: {stderr_msg}")

    # SMART FADE HELPER METHODS

    def _calculate_optimal_crossfade_bars(self, bpm_out: float, bpm_in: float) -> int:
        """Calculate optimal crossfade bars based on BPM compatibility."""
        bpm_diff_percent = abs(1.0 - bpm_in / bpm_out) * 100

        # Mathematical formula for bar calculation based on BPM difference
        # Maps: 0% -> 16 bars, 3% -> 8 bars, 8% -> 4 bars, 15% -> 2 bars, 25%+ -> 1 bar
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

        self.logger.debug(
            "BPM compatibility: fadeout=%.1f, fadein=%.1f, diff=%.1f%% -> %d bars",
            bpm_out,
            bpm_in,
            bpm_diff_percent,
            bars,
        )
        return bars

    def _calculate_optimal_fade_timing(
        self,
        fade_out_analysis: SmartFadesAnalysis,
        fade_in_analysis: SmartFadesAnalysis,
        crossfade_bars: int = 4,
        max_fallback_duration: float = 15.0,
    ) -> tuple[float, float | None, float | None]:
        """
        Calculate precise fade timing and beat positions for alignment.

        Returns:
            (crossfade_duration, fadeout_start_pos, fadein_start_pos)
            where positions are in seconds from audio start, or None if no beat alignment
        """
        beats_per_bar = 4

        # Try downbeats first for most musical timing
        if (
            len(fade_out_analysis.downbeats) >= crossfade_bars
            and len(fade_in_analysis.downbeats) >= crossfade_bars
        ):
            fade_out_downbeats = fade_out_analysis.downbeats[-crossfade_bars:]
            fade_in_downbeats = fade_in_analysis.downbeats[:crossfade_bars]

            if len(fade_out_downbeats) > 1 and len(fade_in_downbeats) > 1:
                fade_out_duration = fade_out_downbeats[-1] - fade_out_downbeats[0]
                fade_in_duration = fade_in_downbeats[-1] - fade_in_downbeats[0]

                if fade_out_duration > 0 and fade_in_duration > 0:
                    optimal_duration = (fade_out_duration + fade_in_duration) / 2
                    smart_duration = min(optimal_duration, MAX_SMART_CROSSFADE_DURATION)

                    self.logger.debug(
                        "Timing from downbeats: %.2fs, fadeout=%.2fs, fadein=%.2fs",
                        smart_duration,
                        fade_out_downbeats[0],
                        fade_in_downbeats[0],
                    )
                    return smart_duration, fade_out_downbeats[0], fade_in_downbeats[0]

        # Try regular beats if downbeats insufficient
        required_beats = crossfade_bars * beats_per_bar
        if (
            len(fade_out_analysis.beats) >= required_beats
            and len(fade_in_analysis.beats) >= required_beats
        ):
            fade_out_duration = (
                fade_out_analysis.beats[-1] - fade_out_analysis.beats[-required_beats]
            )
            fade_in_duration = (
                fade_in_analysis.beats[required_beats - 1] - fade_in_analysis.beats[0]
            )

            optimal_duration = (fade_out_duration + fade_in_duration) / 2
            smart_duration = min(optimal_duration, MAX_SMART_CROSSFADE_DURATION)

            self.logger.debug(
                "Timing from beats: %.2fs, fadeout=%.2fs, fadein=%.2fs",
                smart_duration,
                fade_out_analysis.beats[-required_beats],
                fade_in_analysis.beats[0],
            )
            return (
                smart_duration,
                fade_out_analysis.beats[-required_beats],
                fade_in_analysis.beats[0],
            )

        # Fallback: Calculate from BPM
        seconds_per_beat = 60.0 / fade_out_analysis.bpm
        fallback_duration = min(
            crossfade_bars * beats_per_bar * seconds_per_beat, max_fallback_duration
        )

        self.logger.debug("BPM fallback timing: %.2fs (no beat alignment)", fallback_duration)
        return fallback_duration, None, None

    def _create_enhanced_smart_fade_filters(
        self,
        fade_out_analysis: SmartFadesAnalysis,
        fade_in_analysis: SmartFadesAnalysis,
        fade_out_input: str,
        fade_in_input: str,
        crossfade_duration: float,
        fadeout_start_pos: float | None = None,
        fadein_start_pos: float | None = None,
    ) -> tuple[list[str], str]:
        """
        Create smart fade filters with perfect timing and adaptive filtering.

        Focuses on beat-perfect timing and intelligent frequency separation
        for smooth, natural fades.

        Returns:
            (filter_list, fade_in_label) - where fade_in_label is the final input label for fade_in
        """
        filters = []
        current_fade_out_label = fade_out_input
        current_fade_in_label = fade_in_input

        # No tempo modification - preserve original audio quality

        # Step 1a: Beat alignment preprocessing
        fadeout_buffer_pos = None

        if fadeout_start_pos is not None and fadein_start_pos is not None:
            # Translate fadeout position to buffer coordinates (fadein needs no translation)
            fadeout_buffer_pos = self._translate_fadeout_position_to_buffer(
                fadeout_start_pos, fade_out_analysis
            )

            # Check if both positions are within buffer ranges
            if fadeout_buffer_pos is not None and fadein_start_pos <= MAX_SMART_CROSSFADE_DURATION:
                # Apply beat alignment: trim fadein track, keep fadeout intact
                filters.append(f"{fade_out_input}anull[fadeout_aligned]")  # codespell:ignore
                filters.append(
                    f"{fade_in_input}atrim=start={fadein_start_pos},asetpts=PTS-STARTPTS[fadein_aligned]"
                )
                current_fade_out_label = "[fadeout_aligned]"
                current_fade_in_label = "[fadein_aligned]"

                self.logger.debug(
                    "Beat alignment: fadeout %.2fs->%.2fs, fadein %.2fs",
                    fadeout_start_pos,
                    fadeout_buffer_pos,
                    fadein_start_pos,
                )
            else:
                # Beat positions outside buffer range, use standard processing
                filters.append(f"{fade_out_input}anull[fadeout_clean]")  # codespell:ignore
                filters.append(f"{fade_in_input}anull[fadein_clean]")  # codespell:ignore
                current_fade_out_label = "[fadeout_clean]"
                current_fade_in_label = "[fadein_clean]"
        else:
            # No beat alignment - pass through audio unchanged
            filters.append(f"{fade_out_input}anull[fadeout_clean]")  # codespell:ignore
            filters.append(f"{fade_in_input}anull[fadein_clean]")  # codespell:ignore
            current_fade_out_label = "[fadeout_clean]"
            current_fade_in_label = "[fadein_clean]"
            # Calculate approximate position where crossfade happens in buffer
            # The buffer contains the last MAX_SMART_CROSSFADE_DURATION seconds
            # Crossfade happens at the very end
            fadeout_buffer_pos = MAX_SMART_CROSSFADE_DURATION - crossfade_duration / 2

        # Step 2: Apply gentle 2-band complementary filtering with opposing ramps
        frequency_filters = self._create_gentle_complementary_filters(
            fade_out_analysis,
            fade_in_analysis,
            current_fade_out_label,
            current_fade_in_label,
            crossfade_duration,
            fadeout_buffer_pos,
        )
        filters.extend(frequency_filters)

        # Step 3: Apply linear crossfade (no curves to avoid interfering with gradual EQ ramping)
        filters.append(f"[fadeout_eq][fadein_eq]acrossfade=d={crossfade_duration}")

        return filters, current_fade_in_label
    
    def _create_gentle_complementary_filters(
        self,
        fade_out_analysis: SmartFadesAnalysis,
        fade_in_analysis: SmartFadesAnalysis,
        fade_out_label: str,
        fade_in_label: str,
        crossfade_duration: float,
        fadeout_buffer_pos: float | None = None,
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

        # EQ ramp duration: 1.5x crossfade, minimum 8 seconds
        eq_ramp_duration = max(crossfade_duration * 1.5, 8.0)

        # Calculate EQ start times
        fadeout_eq_start = 0.0
        if fadeout_buffer_pos is not None:
            eq_start_offset = (eq_ramp_duration - crossfade_duration) / 2
            fadeout_eq_start = max(0.0, fadeout_buffer_pos - eq_start_offset)

        self.logger.debug(
            "EQ: %dHz, %.1fs ramp, BPM avg=%.1f ratio=%.2f",
            crossover_freq,
            eq_ramp_duration,
            avg_bpm,
            bpm_ratio,
        )

        # Generate filter expressions using helper function
        def volume_ramp(start_time: float, duration: float, direction: str = "up") -> str:
            if direction == "up":
                return f"'min(max(t-{start_time},0),{duration})/{duration}':eval=frame"
            else:
                return f"'1-min(max(t-{start_time},0),{duration})/{duration}':eval=frame"

        return [
            # Fadeout: unfiltered → high-pass filtered
            f"{fade_out_label}asplit=2[fadeout_orig][fadeout_tohp]",
            f"[fadeout_tohp]highpass=f={crossover_freq}:poles=1[fadeout_filtered]",
            f"[fadeout_orig]volume={volume_ramp(fadeout_eq_start, eq_ramp_duration, 'down')}"
            "[fadeout_orig_faded]",
            f"[fadeout_filtered]volume={volume_ramp(fadeout_eq_start, eq_ramp_duration, 'up')}"
            "[fadeout_filtered_faded]",
            "[fadeout_orig_faded][fadeout_filtered_faded]amix=inputs=2:duration=longest:"
            "normalize=0[fadeout_eq]",
            # Fadein: low-pass filtered → unfiltered
            f"{fade_in_label}asplit=2[fadein_orig][fadein_tolp]",
            f"[fadein_tolp]lowpass=f={crossover_freq}:poles=1[fadein_filtered]",
            f"[fadein_filtered]volume={volume_ramp(0, eq_ramp_duration, 'down')}"
            "[fadein_filtered_faded]",
            f"[fadein_orig]volume={volume_ramp(0, eq_ramp_duration, 'up')}[fadein_orig_faded]",
            "[fadein_filtered_faded][fadein_orig_faded]amix=inputs=2:duration=longest:"
            "normalize=0[fadein_eq]",
        ]

    def _add_lowpass_highpass_filters(
        self,
        fade_out_analysis: SmartFadesAnalysis,
        fade_in_analysis: SmartFadesAnalysis,
        fade_out_label: str,
        fade_in_label: str,
        crossfade_duration: float,
        fadeout_buffer_pos: float | None = None,
    ) -> list[str]:
        """Create gradual complementary filters using frequency sweeps for smooth transitions."""
        # Calculate target frequency based on average BPM (for DJ software style)
        avg_bpm = (fade_out_analysis.bpm + fade_in_analysis.bpm) / 2
        bpm_ratio = fade_in_analysis.bpm / fade_out_analysis.bpm

        # For swapped filters (DJ software style): 90 BPM -> 1500Hz, 140 BPM -> 2500Hz
        crossover_freq = int(np.clip(1500 + (avg_bpm - 90) * 20, 1500, 3000))

        # Adjust for BPM mismatch
        if abs(bpm_ratio - 1.0) > 0.3:
            crossover_freq = int(crossover_freq * 0.85)

        # EQ ramp duration: 2.5x crossfade for more noticeable effect, minimum 8 seconds
        eq_ramp_duration = max(crossfade_duration * 2.5, 8.0)

        # Calculate when the EQ sweep should start
        # The crossfade always happens at the END of the buffer, regardless of beat alignment
        # The fadeout_buffer_pos is only used for beat alignment, not for EQ timing
        fadeout_eq_start = max(0, MAX_SMART_CROSSFADE_DURATION - eq_ramp_duration)

        self.logger.debug(
            "EQ: %dHz, %.1fs ramp, start=%.2fs, pos=%.2fs, BPM=%.1f r=%.2f",
            crossover_freq,
            eq_ramp_duration,
            fadeout_eq_start,
            fadeout_buffer_pos if fadeout_buffer_pos is not None else -1,
            avg_bpm,
            bpm_ratio,
        )

        # Generate filter expressions using helper function
        def volume_ramp(start_time: float, duration: float, direction: str = "up") -> str:
            if direction == "up":
                return f"'min(max(t-{start_time},0),{duration})/{duration}':eval=frame"
            else:
                return f"'1-min(max(t-{start_time},0),{duration})/{duration}':eval=frame"

        return [
            # Fadeout: unfiltered → low-pass filtered (swapped from high-pass)
            f"{fade_out_label}asplit=2[fadeout_orig][fadeout_tolp]",
            f"[fadeout_tolp]lowpass=f={crossover_freq}:poles=1[fadeout_filtered]",
            f"[fadeout_orig]volume={volume_ramp(fadeout_eq_start, eq_ramp_duration, 'down')}"
            "[fadeout_orig_faded]",
            f"[fadeout_filtered]volume={volume_ramp(fadeout_eq_start, eq_ramp_duration, 'up')}"
            "[fadeout_filtered_faded]",
            "[fadeout_orig_faded][fadeout_filtered_faded]amix=inputs=2:duration=longest:"
            "normalize=0[fadeout_eq]",
            # Fadein: high-pass filtered → unfiltered (swapped from low-pass)
            f"{fade_in_label}asplit=2[fadein_orig][fadein_tohp]",
            f"[fadein_tohp]highpass=f={crossover_freq}:poles=1[fadein_filtered]",
            f"[fadein_filtered]volume={volume_ramp(0, eq_ramp_duration, 'down')}"
            "[fadein_filtered_faded]",
            f"[fadein_orig]volume={volume_ramp(0, eq_ramp_duration, 'up')}[fadein_orig_faded]",
            "[fadein_filtered_faded][fadein_orig_faded]amix=inputs=2:duration=longest:"
            "normalize=0[fadein_eq]",
        ]

    def _translate_fadeout_position_to_buffer(
        self,
        fadeout_start_pos: float,
        fade_out_analysis: SmartFadesAnalysis,
    ) -> float | None:
        """
        Translate fadeout beat position from full-track coordinates to buffer coordinates.

        Buffer contains LAST MAX_SMART_CROSSFADE_DURATION seconds of fadeout track.
        """
        if not fade_out_analysis.duration:
            return None

        if fade_out_analysis.duration > MAX_SMART_CROSSFADE_DURATION:
            # Buffer contains seconds [duration-MAX, duration] mapped to [0, MAX]
            buffer_start = fade_out_analysis.duration - MAX_SMART_CROSSFADE_DURATION
            return fadeout_start_pos - buffer_start if fadeout_start_pos >= buffer_start else None
        else:
            # Short track - entire track fits in buffer (direct mapping)
            return fadeout_start_pos

    # FALLBACK DEFAULT CROSSFADE
    async def default_crossfade(
        self,
        fade_in_part: bytes,
        fade_out_part: bytes,
        pcm_format: AudioFormat,
        crossfade_duration: int = 10,
    ) -> bytes:
        """Apply a standard crossfade without smart analysis."""
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
