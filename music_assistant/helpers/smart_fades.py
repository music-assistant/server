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
                "Collected %.2fs of audio data for analysis "
                "(%d bytes, format: %s, %dHz, %d-bit, %d channels)",
                streamdetails.duration or 0,
                len(audio_data),
                ANALYSIS_PCM_FORMAT.content_type,
                ANALYSIS_PCM_FORMAT.sample_rate,
                ANALYSIS_PCM_FORMAT.bit_depth,
                ANALYSIS_PCM_FORMAT.channels,
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
            # Calculate actual duration from audio data
            actual_duration = len(audio_data) / ANALYSIS_PCM_FORMAT.pcm_sample_size

            audio_array = self._prepare_audio_for_madmom(audio_data)
            analysis = await asyncio.to_thread(self._madmom_beat_analysis, audio_array)
            if analysis:
                # Update the analysis with the calculated duration
                analysis.duration = actual_duration
            return analysis
        except Exception as e:
            self.logger.exception("Beat tracking analysis failed: %s", e)
            return None

    def _prepare_audio_for_madmom(self, pcm_data: bytes) -> np.ndarray:
        """Convert PCM bytes to numpy array for madmom."""
        # Convert from 32-bit float PCM to numpy array
        audio_array = np.frombuffer(pcm_data, dtype=np.float32)

        # Audio is stereo (2 channels), convert to mono by averaging channels
        if len(audio_array) % 2 == 0:  # Ensure even number of samples for stereo
            audio_array = audio_array.reshape(-1, 2)  # Reshape to [samples, channels]
            audio_array = np.mean(audio_array, axis=1)  # Average stereo to mono

        # Calculate actual duration from original bytes and format
        # PCM_F32LE: 4 bytes per sample * 2 channels = 8 bytes per frame
        actual_duration = len(pcm_data) / (
            ANALYSIS_PCM_FORMAT.sample_rate * 4 * 2
        )  # 4 bytes * 2 channels
        self.logger.debug(
            "Prepared %.2fs of audio for madmom analysis (%d samples)",
            actual_duration,
            len(audio_array),
        )

        return audio_array

    def _madmom_beat_analysis(self, audio_array: np.ndarray) -> SmartFadesAnalysis:
        """Perform beat analysis using madmom."""
        # Use most cores but leave some headroom for the main app
        num_cores = max(1, multiprocessing.cpu_count() - 2)
        self.logger.debug(
            "Running madmom beat analysis on %d samples using %d cores (fps=%d)",
            len(audio_array),
            num_cores,
            ANALYSIS_FPS,
        )

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

            # Check if this might be half-time detection (BPM too low)
            # If raw BPM is significantly low, try doubling it
            if raw_bpm < 80:
                doubled_bpm = raw_bpm * 2
                # Check if doubled BPM is in a more reasonable range
                if 90 <= doubled_bpm <= 180:
                    self.logger.debug(
                        "BPM doubled from %.1f to %.1f (half-time detection)",
                        raw_bpm,
                        doubled_bpm,
                    )
                    bpm = doubled_bpm
                else:
                    bpm = raw_bpm
            else:
                bpm = raw_bpm

            self.logger.debug(
                "BPM calculation: %d beats, avg_interval=%.4fs, raw_bpm=%.1f, final_bpm=%.1f",
                len(beats),
                avg_interval,
                raw_bpm,
                bpm,
            )
        else:
            bpm = 120.0  # Default BPM
            self.logger.debug("BPM calculation: Not enough beats, using default 120.0")

        # Confidence based on beat consistency
        if len(beats) > 4:
            beat_intervals = np.diff(beats)
            interval_std = np.std(beat_intervals)
            avg_interval = np.mean(beat_intervals)
            confidence = (
                float(1.0 - min(interval_std / avg_interval, 1.0)) if avg_interval > 0 else 0.0
            )
        else:
            confidence = 0.0

        analysis = SmartFadesAnalysis(
            bpm=bpm,
            beats=beats,
            downbeats=downbeats,
            confidence=confidence,
        )

        self.logger.info(
            "Beat analysis complete : BPM=%.1f, %d beats, %d downbeats, confidence=%.2f",
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
        # Debug logging for crossfade decision
        self.logger.debug(
            "Crossfade analysis check: fadeout_analysis=%s (conf=%.2f), fadein_analysis=%s (conf=%.2f)",
            bool(fade_out_analysis),
            fade_out_analysis.confidence if fade_out_analysis else 0.0,
            bool(fade_in_analysis),
            fade_in_analysis.confidence if fade_in_analysis else 0.0,
        )

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
            self.logger.debug(
                "Using smart crossfade: fadeout_bpm=%.1f, fadein_bpm=%.1f, bars=%d",
                fade_out_analysis.bpm,
                fade_in_analysis.bpm,
                optimal_bars,
            )

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
                # Fall through to standard crossfade
        # Log why we're using standard crossfade
        elif not fade_out_analysis:
            self.logger.debug("Using standard crossfade: no fadeout analysis")
        elif not fade_in_analysis:
            self.logger.debug("Using standard crossfade: no fadein analysis")
        elif fade_out_analysis.confidence <= 0.3:
            self.logger.debug(
                "Using standard crossfade: fadeout confidence too low (%.2f)",
                fade_out_analysis.confidence,
            )
        elif fade_in_analysis.confidence <= 0.3:
            self.logger.debug(
                "Using standard crossfade: fadein confidence too low (%.2f)",
                fade_in_analysis.confidence,
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

        # BPM information for logging (no tempo adjustment in basic implementation)
        self.logger.debug(
            "Track BPMs: fade_out=%.1f, fade_in=%.1f",
            fade_out_analysis.bpm,
            fade_in_analysis.bpm,
        )

        # Calculate optimal fade duration using beat analysis
        optimal_duration, fadeout_start_pos, fadein_start_pos = self._calculate_optimal_fade_timing(
            fade_out_analysis, fade_in_analysis, crossfade_bars
        )

        if fadeout_start_pos is not None and fadein_start_pos is not None:
            self.logger.debug(
                "Smart fade duration: %.2fs (%d bars, beat-aligned at fadeout=%.2fs, fadein=%.2fs)",
                optimal_duration,
                crossfade_bars,
                fadeout_start_pos,
                fadein_start_pos,
            )
        else:
            self.logger.debug(
                "Smart fade duration: %.2fs (%d bars, BPM fallback - no beat alignment)",
                optimal_duration,
                crossfade_bars,
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
            "quiet",
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

        self.logger.debug("Smart fade FFmpeg command: %s", " ".join(args))

        # Execute the enhanced smart fade with full buffer
        _, raw_crossfade_output, stderr = await communicate(args, fade_in_part)
        await remove_file(fadeout_filename)

        self.logger.debug(
            "FFmpeg smart fade execution: input_size=%d bytes, raw_output_size=%d bytes, stderr=%s",
            len(fade_in_part),
            len(raw_crossfade_output) if raw_crossfade_output else 0,
            stderr.decode() if stderr else "(none)",
        )

        # Use full FFmpeg output directly (includes post-crossfade audio naturally)
        if raw_crossfade_output:
            actual_duration = len(raw_crossfade_output) / pcm_format.pcm_sample_size
            self.logger.debug(
                "Smart crossfade output: %.2fs duration, size=%d bytes",
                actual_duration,
                len(raw_crossfade_output),
            )

            self.logger.info(
                "Smart fade successful: duration=%.2fs, full buffer processing",
                optimal_duration,
            )
            return raw_crossfade_output
        else:
            raise RuntimeError(
                f"Smart crossfade failed. FFmpeg stderr: {stderr.decode() if stderr else '(no stderr output)'}"
            )

    # SMART FADE HELPER METHODS

    def _calculate_optimal_crossfade_bars(self, bpm_out: float, bpm_in: float) -> int:
        """Calculate optimal crossfade bars based on BPM compatibility."""
        bpm_diff_percent = abs(1.0 - bpm_in / bpm_out) * 100

        self.logger.debug(
            "BPM compatibility analysis: fadeout=%.1f, fadein=%.1f, difference=%.1f%%",
            bpm_out,
            bpm_in,
            bpm_diff_percent,
        )

        if bpm_diff_percent < 1.5:
            # Very close BPMs - long crossfade sounds natural with beat alignment
            bars = 16
            self.logger.debug(
                "Very compatible BPMs (%.1f%%) - using %d bars", bpm_diff_percent, bars
            )
        elif bpm_diff_percent < 3.0:
            # Close BPMs - medium-long crossfade
            bars = 8
            self.logger.debug("Compatible BPMs (%.1f%%) - using %d bars", bpm_diff_percent, bars)
        elif bpm_diff_percent < 8.0:
            # Small difference - medium crossfade
            bars = 4
            self.logger.debug(
                "Small BPM difference (%.1f%%) - using %d bars", bpm_diff_percent, bars
            )
        elif bpm_diff_percent < 15.0:
            # Medium difference - short crossfade
            bars = 2
            self.logger.debug(
                "Moderate BPM difference (%.1f%%) - using %d bars", bpm_diff_percent, bars
            )
        elif bpm_diff_percent < 25.0:
            # Large difference - very short crossfade
            bars = 2
            self.logger.debug(
                "Large BPM difference (%.1f%%) - using %d bars", bpm_diff_percent, bars
            )
        else:
            # Huge difference - minimal crossfade to avoid drift
            bars = 1
            self.logger.debug(
                "Extreme BPM difference (%.1f%%) - using %d bar only", bpm_diff_percent, bars
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
        # PRIMARY: Try to use downbeats for more musical timing (NO CAP)
        if (
            len(fade_out_analysis.downbeats) >= crossfade_bars
            and len(fade_in_analysis.downbeats) >= crossfade_bars
        ):
            # Use actual downbeat spacing for precise timing
            fade_out_downbeats = fade_out_analysis.downbeats[-crossfade_bars:]
            fade_in_downbeats = fade_in_analysis.downbeats[:crossfade_bars]

            fade_out_duration = (
                fade_out_downbeats[-1] - fade_out_downbeats[0] if len(fade_out_downbeats) > 1 else 0
            )
            fade_in_duration = (
                fade_in_downbeats[-1] - fade_in_downbeats[0] if len(fade_in_downbeats) > 1 else 0
            )

            if fade_out_duration > 0 and fade_in_duration > 0:
                # Use the average for balanced fade
                optimal_duration = (fade_out_duration + fade_in_duration) / 2
                # Apply reasonable absolute limit (much higher than user cap)
                smart_duration = min(
                    optimal_duration, MAX_SMART_CROSSFADE_DURATION
                )  # Reasonable absolute max

                # Calculate beat-aligned positions
                fadeout_start_pos = fade_out_downbeats[0]  # Start fade at first downbeat
                fadein_start_pos = fade_in_downbeats[0]  # Start fadein at first downbeat

                self.logger.debug(
                    "Smart timing from downbeats: %.2fs, fadeout_start=%.2fs, fadein_start=%.2fs",
                    smart_duration,
                    fadeout_start_pos,
                    fadein_start_pos,
                )
                return smart_duration, fadeout_start_pos, fadein_start_pos

        # SECONDARY: Use beats if downbeats insufficient (NO CAP)
        beats_per_bar = 4
        required_beats = crossfade_bars * beats_per_bar

        if (
            len(fade_out_analysis.beats) >= required_beats
            and len(fade_in_analysis.beats) >= required_beats
        ):
            fade_out_beats_duration = (
                fade_out_analysis.beats[-1] - fade_out_analysis.beats[-required_beats]
            )
            fade_in_beats_duration = (
                fade_in_analysis.beats[required_beats - 1] - fade_in_analysis.beats[0]
            )
            optimal_duration = (fade_out_beats_duration + fade_in_beats_duration) / 2
            # Apply reasonable absolute limit
            smart_duration = min(optimal_duration, MAX_SMART_CROSSFADE_DURATION)

            # Calculate beat-aligned positions using regular beats
            fadeout_start_pos = fade_out_analysis.beats[
                -required_beats
            ]  # Start fade at calculated beat
            fadein_start_pos = fade_in_analysis.beats[0]  # Start fadein at first beat

            self.logger.debug(
                "Smart timing from beats: %.2fs, fadeout_start=%.2fs, fadein_start=%.2fs",
                smart_duration,
                fadeout_start_pos,
                fadein_start_pos,
            )
            # Round to madmom's analysis precision (1/ANALYSIS_FPS) for clean FFmpeg processing
            precision_decimals = len(str(ANALYSIS_FPS)) - 1  # 100 FPS = 0.01s = 2 decimal places
            return (
                round(float(smart_duration), precision_decimals),
                fadeout_start_pos,
                fadein_start_pos,
            )

        # FALLBACK: Calculate from BPM (APPLY USER CAP HERE)
        seconds_per_beat = 60.0 / fade_out_analysis.bpm
        fallback_duration = crossfade_bars * beats_per_bar * seconds_per_beat
        capped_duration = min(fallback_duration, max_fallback_duration)

        if capped_duration < fallback_duration:
            self.logger.debug(
                "BPM fallback duration capped: calculated=%.2fs, capped=%.2fs",
                fallback_duration,
                capped_duration,
            )

        self.logger.debug("Using BPM fallback timing: %.2fs (no beat alignment)", capped_duration)
        # Round to madmom's analysis precision for clean FFmpeg processing
        precision_decimals = len(str(ANALYSIS_FPS)) - 1  # 100 FPS = 0.01s = 2 decimal places
        return (
            round(capped_duration, precision_decimals),
            None,
            None,
        )  # No beat positions available for BPM fallback

    def _create_adaptive_frequency_filters(
        self,
        fade_out_analysis: SmartFadesAnalysis,
        fade_in_analysis: SmartFadesAnalysis,
        fade_out_input: str,
        fade_in_input: str,
    ) -> list[str]:
        """Create frequency filters adapted to BPM and track characteristics."""
        # Adjust filter frequencies based on BPM and energy
        # Higher BPM typically = higher energy = need different filter characteristics
        fade_out_energy_factor = min(
            2.0, max(0.8, fade_out_analysis.bpm / 120.0)
        )  # Normalize around 120 BPM
        fade_in_energy_factor = min(2.0, max(0.8, fade_in_analysis.bpm / 120.0))

        # Base frequencies adjusted by energy
        high_pass_freq = int(80 * fade_out_energy_factor)  # 64-160 Hz range
        low_pass_freq = int(8000 - (500 * fade_in_energy_factor))  # 7000-7600 Hz range

        # Additional filtering based on BPM difference
        bpm_ratio = fade_in_analysis.bpm / fade_out_analysis.bpm
        if bpm_ratio > 1.1:  # Incoming track is faster
            # Slightly more aggressive low-pass to smooth the transition
            low_pass_freq = int(low_pass_freq * 0.9)
        elif bpm_ratio < 0.9:  # Incoming track is slower
            # Slightly more aggressive high-pass on outgoing to reduce muddy transition
            high_pass_freq = int(high_pass_freq * 1.1)

        return [
            f"{fade_out_input}highpass=f={high_pass_freq}:poles=2[fadeout_hp]",
            f"{fade_in_input}lowpass=f={low_pass_freq}:poles=2[fadein_lp]",
        ]

    def _calculate_phase_offset(
        self,
        beats_out: np.ndarray,
        beats_in: np.ndarray,
        crossfade_start_out: float,
        crossfade_start_in: float,
    ) -> float:
        """
        Calculate phase offset between two beat arrays at crossfade point.
        Returns offset in seconds that needs to be corrected.
        """
        if len(beats_out) < 2 or len(beats_in) < 2:
            return 0.0

        # Find nearest beats to crossfade points
        out_beat_idx = np.argmin(np.abs(beats_out - crossfade_start_out))
        in_beat_idx = np.argmin(np.abs(beats_in - crossfade_start_in))

        # Calculate beat intervals (tempo)
        out_start_idx = max(0, int(out_beat_idx - 4))
        out_end_idx = int(out_beat_idx + 1)
        out_interval = np.mean(np.diff(beats_out[out_start_idx:out_end_idx]))

        in_end_idx = min(len(beats_in), int(in_beat_idx + 5))
        in_interval = np.mean(np.diff(beats_in[int(in_beat_idx) : in_end_idx]))

        # Calculate phase within the beat grid
        out_phase = (crossfade_start_out - beats_out[out_beat_idx]) % out_interval
        in_phase = (crossfade_start_in - beats_in[in_beat_idx]) % in_interval

        # Normalize phases to same tempo for comparison
        normalized_in_phase = in_phase * (out_interval / in_interval)

        # Calculate phase difference (how much to shift fadein to align)
        phase_diff = out_phase - normalized_in_phase

        # Keep adjustment within half a beat to avoid jumps
        if abs(phase_diff) > out_interval / 2:
            phase_diff = phase_diff - np.sign(phase_diff) * out_interval

        self.logger.debug(
            "Phase analysis: out_phase=%.3fs, in_phase=%.3fs, offset=%.3fs",
            out_phase,
            normalized_in_phase,
            phase_diff,
        )

        return float(phase_diff)

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

        # Step 1: Artifact-free approach - no tempo modification
        # Log BPM information for transparency
        bpm_diff_percent = abs(1.0 - fade_in_analysis.bpm / fade_out_analysis.bpm)
        self.logger.info(
            "Smart fade: fadeout=%.1f BPM, fadein=%.1f BPM, difference=%.1f%%",
            fade_out_analysis.bpm,
            fade_in_analysis.bpm,
            bpm_diff_percent * 100,
        )

        # Step 1a: Beat alignment preprocessing with coordinate translation
        if fadeout_start_pos is not None and fadein_start_pos is not None:
            # Translate full-track beat positions to buffer coordinates
            fadeout_buffer_pos, fadein_buffer_pos = self._translate_beat_positions_to_buffer(
                fadeout_start_pos, fadein_start_pos, fade_out_analysis, fade_in_analysis
            )

            if fadeout_buffer_pos is not None and fadein_buffer_pos is not None:
                # Only trim fadein track for beat alignment, keep fadeout track intact
                filters.append(
                    f"{fade_in_input}atrim=start={fadein_buffer_pos},asetpts=PTS-STARTPTS[fadein_aligned]"
                )

                current_fade_out_label = fade_out_input  # Use original fadeout track (no trimming)
                current_fade_in_label = "[fadein_aligned]"

                self.logger.debug(
                    "Applied simple beat alignment: track_pos(%.2fs,%.2fs) -> buffer_pos(%.2fs,%.2fs)",
                    fadeout_start_pos,
                    fadein_start_pos,
                    fadeout_buffer_pos,
                    fadein_buffer_pos,
                )
            else:
                # Beat positions outside buffer range, use standard processing
                filters.append(f"{fade_out_input}anull[fadeout_clean]")  # codespell:ignore
                filters.append(f"{fade_in_input}anull[fadein_clean]")  # codespell:ignore
                current_fade_out_label = "[fadeout_clean]"
                current_fade_in_label = "[fadein_clean]"

                self.logger.debug(
                    "Beat positions outside buffer range: fadeout=%.2fs, fadein=%.2fs - using standard crossfade",
                    fadeout_start_pos,
                    fadein_start_pos,
                )
        else:
            # No beat alignment - pass through audio unchanged
            filters.append(f"{fade_out_input}anull[fadeout_clean]")  # codespell:ignore
            filters.append(f"{fade_in_input}anull[fadein_clean]")  # codespell:ignore
            current_fade_out_label = "[fadeout_clean]"
            current_fade_in_label = "[fadein_clean]"

        # Step 2: Apply gentle 2-band complementary filtering with opposing ramps
        # Get fadeout_buffer_pos if available from beat alignment
        current_fadeout_buffer_pos = None
        if fadeout_start_pos is not None and fadein_start_pos is not None:
            fadeout_buffer_pos, _ = self._translate_beat_positions_to_buffer(
                fadeout_start_pos, fadein_start_pos, fade_out_analysis, fade_in_analysis
            )
            current_fadeout_buffer_pos = fadeout_buffer_pos

        frequency_filters = self._create_gentle_complementary_filters(
            fade_out_analysis,
            fade_in_analysis,
            current_fade_out_label,
            current_fade_in_label,
            crossfade_duration,
            current_fadeout_buffer_pos,
        )
        filters.extend(frequency_filters)

        # Step 3: Apply linear crossfade (no curves to avoid interfering with gradual EQ ramping)
        filters.append(f"[fadeout_eq][fadein_eq]acrossfade=d={crossfade_duration}")

        return filters, current_fade_in_label

    def _create_professional_eq_filters(
        self,
        fade_out_analysis: SmartFadesAnalysis,
        fade_in_analysis: SmartFadesAnalysis,
        fade_out_label: str,
        fade_in_label: str,
    ) -> list[str]:
        """
        Create professional 3-band EQ filters for smooth frequency transitions.

        Uses complementary filtering approach:
        - Fadeout: Progressively remove low frequencies (bass) and preserve highs
        - Fadein: Progressively remove high frequencies (treble) and preserve lows
        """
        filters = []

        # Analyze frequency characteristics
        # Higher BPM tracks typically have more energy in higher frequencies
        bpm_ratio = fade_in_analysis.bpm / fade_out_analysis.bpm

        # Professional DJ-style frequency splitting points
        low_cutoff = 120  # Bass/Low-Mid boundary
        high_cutoff = 4000  # Mid/High boundary

        # Adjust cutoff points based on BPM characteristics
        if bpm_ratio > 1.2:  # Fadein significantly faster
            # Emphasize bass transition - fadein likely more energetic
            low_cutoff = 150
            high_cutoff = 3500
        elif bpm_ratio < 0.8:  # Fadeout significantly faster
            # Emphasize treble transition - fadeout likely more energetic
            low_cutoff = 100
            high_cutoff = 4500

        self.logger.debug(
            "Professional EQ setup: low_cutoff=%dHz, high_cutoff=%dHz, bpm_ratio=%.2f",
            low_cutoff,
            high_cutoff,
            bpm_ratio,
        )

        # Fadeout track: Progressive high-pass filtering (remove bass gradually)
        # 3-band approach: preserve highs, reduce mids, heavily cut lows
        filters.extend(
            [
                # Low band: High-pass filter to remove bass frequencies
                f"{fade_out_label}highpass=f={low_cutoff}:poles=2:width_type=h:width=0.707[fadeout_hp]",
                # Mid band: Gentle high-pass to clean up low-mids
                f"[fadeout_hp]highpass=f={low_cutoff // 2}:poles=1:width_type=h:width=0.707[fadeout_mid]",
                # High band: Preserve treble with gentle boost
                f"[fadeout_mid]treble=gain=2:frequency={high_cutoff}:width_type=h:width=0.5[fadeout_eq]",
            ]
        )

        # Fadein track: Progressive low-pass filtering (remove treble gradually)
        # 3-band approach: preserve lows, reduce mids, heavily cut highs
        filters.extend(
            [
                # High band: Low-pass filter to remove treble frequencies
                f"{fade_in_label}lowpass=f={high_cutoff}:poles=2:width_type=h:width=0.707[fadein_lp]",
                # Mid band: Gentle low-pass to clean up high-mids
                f"[fadein_lp]lowpass=f={high_cutoff * 1.5}:poles=1:width_type=h:width=0.707[fadein_mid]",
                # Low band: Preserve bass with gentle boost
                f"[fadein_mid]bass=gain=2:frequency={low_cutoff}:width_type=h:width=0.5[fadein_eq]",
            ]
        )

        return filters

    def _create_gentle_complementary_filters(
        self,
        fade_out_analysis: SmartFadesAnalysis,
        fade_in_analysis: SmartFadesAnalysis,
        fade_out_label: str,
        fade_in_label: str,
        crossfade_duration: float,
        fadeout_buffer_pos: float | None = None,
    ) -> list[str]:
        """
        Create gradual complementary filters using volume ramping for smooth transitions.

        Gradual approach using volume filter with time expressions:
        - Fadeout: Gradually ramps UP from unfiltered to high-pass filtered
        - Fadein: Gradually ramps DOWN from low-pass filtered to unfiltered
        - Uses complementary volume curves that sum to 1.0 for constant audio level
        """
        # BPM-adaptive crossover frequency selection
        bpm_ratio = fade_in_analysis.bpm / fade_out_analysis.bpm
        avg_bpm = (fade_out_analysis.bpm + fade_in_analysis.bpm) / 2

        # Conservative crossover range: 800Hz (slow) to 1200Hz (fast)
        # Higher BPM tracks get higher crossover to preserve more energy
        if avg_bpm < 90:
            crossover_freq = 800  # Slower tracks - lower crossover
        elif avg_bpm > 140:
            crossover_freq = 1200  # Faster tracks - higher crossover
        else:
            # Linear interpolation between 90-140 BPM
            crossover_freq = int(800 + (avg_bpm - 90) * (400 / 50))

        # Further adjust based on BPM compatibility
        if abs(bpm_ratio - 1.0) > 0.3:  # BPM difference > 30%
            # Reduce crossover frequency for mismatched BPMs to minimize artifacts
            crossover_freq = int(crossover_freq * 0.8)

        # Extend EQ ramp duration for gentler transitions (longer than crossfade)
        eq_ramp_duration = max(
            crossfade_duration * 1.5, 8.0
        )  # At least 8 seconds for gentle transitions

        # Calculate EQ start time based on crossfade position
        # EQ should start before crossfade: eq_start = crossfade_start - (eq_duration - crossfade_duration) / 2
        # This creates equal EQ time before and after the crossfade (DJ best practice)
        fadeout_eq_start_time = 0.0  # Default for fadeout track
        fadein_eq_start_time = 0.0  # Default for fadein track

        if fadeout_buffer_pos is not None:
            # Fadeout track: EQ starts before crossfade with symmetric distribution
            fadeout_eq_start_time = max(
                0.0, fadeout_buffer_pos - ((eq_ramp_duration - crossfade_duration) / 2)
            )
            # Fadein track: Since it's trimmed to correct position, EQ starts immediately
            fadein_eq_start_time = 0.0

        self.logger.debug(
            "Gradual complementary EQ (normalize=0, duration=longest): crossover=%dHz, eq_duration=%.1fs, crossfade_duration=%.1fs, fadeout_eq_start=%.1fs, fadein_eq_start=%.1fs, avg_bpm=%.1f, bpm_ratio=%.2f",
            crossover_freq,
            eq_ramp_duration,
            crossfade_duration,
            fadeout_eq_start_time,
            fadein_eq_start_time,
            avg_bpm,
            bpm_ratio,
        )

        return [
            # FADEOUT: Gradual ramp UP (unfiltered → filtered) - extended duration for gentler transition
            f"{fade_out_label}asplit=2[fadeout_orig][fadeout_tohp]",
            f"[fadeout_tohp]highpass=f={crossover_freq}:poles=1[fadeout_filtered]",
            f"[fadeout_orig]volume='1-min(max(t-{fadeout_eq_start_time},0),{eq_ramp_duration})/{eq_ramp_duration}':eval=frame[fadeout_orig_faded]",
            f"[fadeout_filtered]volume='min(max(t-{fadeout_eq_start_time},0),{eq_ramp_duration})/{eq_ramp_duration}':eval=frame[fadeout_filtered_faded]",
            "[fadeout_orig_faded][fadeout_filtered_faded]amix=inputs=2:duration=longest:normalize=0[fadeout_eq]",
            # FADEIN: Gradual ramp DOWN (filtered → unfiltered) - extended duration for gentler transition
            f"{fade_in_label}asplit=2[fadein_orig][fadein_tolp]",
            f"[fadein_tolp]lowpass=f={crossover_freq}:poles=1[fadein_filtered]",
            f"[fadein_filtered]volume='1-min(max(t-{fadein_eq_start_time},0),{eq_ramp_duration})/{eq_ramp_duration}':eval=frame[fadein_filtered_faded]",
            f"[fadein_orig]volume='min(max(t-{fadein_eq_start_time},0),{eq_ramp_duration})/{eq_ramp_duration}':eval=frame[fadein_orig_faded]",
            "[fadein_filtered_faded][fadein_orig_faded]amix=inputs=2:duration=longest:normalize=0[fadein_eq]",
        ]

    def _translate_beat_positions_to_buffer(
        self,
        fadeout_start_pos: float,
        fadein_start_pos: float,
        fade_out_analysis: SmartFadesAnalysis,
        fade_in_analysis: SmartFadesAnalysis,
    ) -> tuple[float | None, float | None]:
        """
        Translate beat positions from full-track coordinates to buffer coordinates.

        Buffer layout:
        - fadeout_buffer: LAST MAX_SMART_CROSSFADE_DURATION seconds of fadeout track
        - fadein_buffer: FIRST MAX_SMART_CROSSFADE_DURATION seconds of fadein track
        """
        fadeout_buffer_pos = None
        fadein_buffer_pos = None

        # Translate fadeout position (from end of track to buffer start)
        if fade_out_analysis.duration and fade_out_analysis.duration > MAX_SMART_CROSSFADE_DURATION:
            # Buffer contains seconds [duration-MAX, duration] mapped to [0, MAX]
            buffer_start = fade_out_analysis.duration - MAX_SMART_CROSSFADE_DURATION

            if fadeout_start_pos >= buffer_start:
                fadeout_buffer_pos = fadeout_start_pos - buffer_start
                self.logger.debug(
                    "Fadeout: track_pos=%.2fs -> buffer_pos=%.2fs (track_duration=%.2fs)",
                    fadeout_start_pos,
                    fadeout_buffer_pos,
                    fade_out_analysis.duration,
                )
            else:
                self.logger.debug(
                    "Fadeout beat position %.2fs is outside buffer range (%.2fs-%.2fs)",
                    fadeout_start_pos,
                    buffer_start,
                    fade_out_analysis.duration,
                )
        elif fade_out_analysis.duration:
            # Short track - entire track fits in buffer
            fadeout_buffer_pos = fadeout_start_pos
            self.logger.debug(
                "Fadeout: short track, track_pos=%.2fs = buffer_pos=%.2fs",
                fadeout_start_pos,
                fadeout_buffer_pos,
            )

        # Translate fadein position (first part of track, direct mapping)
        if fadein_start_pos <= MAX_SMART_CROSSFADE_DURATION:
            fadein_buffer_pos = fadein_start_pos
            self.logger.debug(
                "Fadein: track_pos=%.2fs = buffer_pos=%.2fs", fadein_start_pos, fadein_buffer_pos
            )
        else:
            self.logger.debug(
                "Fadein beat position %.2fs is outside buffer range (0-%.2fs)",
                fadein_start_pos,
                MAX_SMART_CROSSFADE_DURATION,
            )

        return fadeout_buffer_pos, fadein_buffer_pos

    # FALLBACK DEFAULT CROSSFADE
    async def default_crossfade(
        self,
        fade_in_part: bytes,
        fade_out_part: bytes,
        pcm_format: AudioFormat,
        crossfade_duration: int = 10,
    ) -> bytes:
        """Apply a standard crossfade without smart analysis."""
        self.logger.debug("Applying default standard crossfade of %ds", crossfade_duration)
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
