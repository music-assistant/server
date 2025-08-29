"""Smart Fades - Object-oriented implementation with intelligent fades and adaptive filtering."""

from __future__ import annotations

import asyncio
import logging
import multiprocessing
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import aiofiles
import madmom
import numpy as np
import shortuuid
from music_assistant_models.enums import ContentType
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.smart_fades import SmartFadesAnalysis

from music_assistant.constants import MASS_LOGGER_NAME, VERBOSE_LOG_LEVEL
from music_assistant.helpers.audio import crossfade_pcm_parts
from music_assistant.helpers.process import communicate
from music_assistant.helpers.util import remove_file

if TYPE_CHECKING:
    from music_assistant_models.streamdetails import StreamDetails

    from music_assistant.mass import MusicAssistant

MAX_SMART_CROSSFADE_DURATION = 30

ANALYSIS_FPS = 100


@dataclass
class CrossfadeData:
    """Data class to hold crossfade data."""

    fadeout_part: bytes = b""
    pcm_format: AudioFormat = field(default_factory=AudioFormat)
    queue_item_id: str | None = None
    session_id: str | None = None
    smart_fades_analysis: SmartFadesAnalysis | None = None


class SmartFadesAnalyzer:
    """Smart fades analyzer that performs audio analysis."""

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize smart fades analyzer."""
        self.mass = mass
        self.logger = logging.getLogger(__name__)

    async def analyze(
        self,
        streamdetails: StreamDetails,
        audio_stream: AsyncGenerator[bytes, None],
    ) -> SmartFadesAnalysis | None:
        """
        Analyze a track's beats for BPM matching smart fade (pyCrossfade approach).

        This is the main entry point for beat analysis.

        Args:
            streamdetails: Stream details containing track metadata and audio format
            audio_stream: Audio data stream for analysis
        """
        # PCM format is hardcoded as specified
        pcm_format = AudioFormat(
            content_type=ContentType.PCM_F32LE, sample_rate=44100, bit_depth=32, channels=2
        )

        # Get track name for logging (derive from streamdetails)
        track_name = self._get_track_name_from_streamdetails(streamdetails)

        start_time = time.perf_counter()
        self.logger.info("Starting beat analysis for track : %s", track_name)

        try:
            # Collect audio data from stream
            audio_data = await self._collect_audio_data(audio_stream, track_name)
            if audio_data is None:
                return None

            # Log audio collection details
            self._log_audio_collection_details(streamdetails, audio_data, pcm_format)

            # Perform beat analysis
            analysis = await self._analyze_track_beats(audio_data, pcm_format.sample_rate)

            # Log analysis results and return
            return self._handle_analysis_results(analysis, track_name, start_time)

        except Exception as e:
            total_time = time.perf_counter() - start_time
            self.logger.exception(
                "Beat analysis error for %s: %s (took %.2fs)",
                track_name,
                e,
                total_time,
            )
            return None

    def _get_track_name_from_streamdetails(self, streamdetails: StreamDetails) -> str:
        """Extract track name from streamdetails for logging."""
        if streamdetails.stream_title:
            return streamdetails.stream_title
        # Try to get a more readable name from the URI or fallback to URI
        return streamdetails.uri

    async def _collect_audio_data(
        self, audio_stream: AsyncGenerator[bytes, None], track_name: str
    ) -> bytes | None:
        """Collect audio data from stream for analysis."""
        audio_data = b""
        async for chunk in audio_stream:
            audio_data += chunk

        if len(audio_data) == 0:
            self.logger.warning("No audio data received for analysis: %s", track_name)
            return None

        return audio_data

    def _log_audio_collection_details(
        self, streamdetails: StreamDetails, audio_data: bytes, pcm_format: AudioFormat
    ) -> None:
        """Log details about collected audio data."""
        self.logger.debug(
            "Collected %.2fs of audio data for analysis "
            "(%d bytes, format: %s, %dHz, %d-bit, %d channels)",
            streamdetails.duration or 0,
            len(audio_data),
            pcm_format.content_type,
            pcm_format.sample_rate,
            pcm_format.bit_depth,
            pcm_format.channels,
        )

    def _handle_analysis_results(
        self, analysis: SmartFadesAnalysis | None, track_name: str, start_time: float
    ) -> SmartFadesAnalysis | None:
        """Handle and log analysis results."""
        total_time = time.perf_counter() - start_time

        if analysis:
            if analysis.confidence > 0.3:  # Good confidence threshold
                self.logger.info(
                    "Beat analysis successful for %s: BPM=%.1f, %d beats, "
                    "Confidence=%.2f (took %.2fs)",
                    track_name,
                    analysis.bpm,
                    len(analysis.beats),
                    analysis.confidence,
                    total_time,
                )
            else:
                self.logger.warning(
                    "Beat analysis low confidence for %s: BPM=%.1f, confidence=%.2f (took %.2fs)",
                    track_name,
                    analysis.bpm,
                    analysis.confidence,
                    total_time,
                )
            return analysis
        else:
            self.logger.warning(
                "Beat analysis failed for %s (took %.2fs)",
                track_name,
                total_time,
            )
            return None

    async def _analyze_track_beats(
        self,
        audio_data: bytes,
        sample_rate: int = 44100,
    ) -> SmartFadesAnalysis | None:
        """Analyze track for beat tracking."""
        try:
            audio_array = self._prepare_audio_for_madmom(audio_data, sample_rate)
            return await asyncio.to_thread(self._madmom_beat_analysis, audio_array, sample_rate)
        except Exception as e:
            self.logger.exception("Beat tracking analysis failed: %s", e)
            return None

    def _prepare_audio_for_madmom(self, pcm_data: bytes, sample_rate: int) -> np.ndarray:
        """Convert PCM bytes to numpy array for madmom."""
        # The audio format from streams.py is PCM_F32LE (32-bit float, little endian, 2 channels)
        # Each sample is 4 bytes (32-bit) * 2 channels = 8 bytes per frame

        # Convert from 32-bit float PCM to numpy array
        audio_array = np.frombuffer(pcm_data, dtype=np.float32)

        # Audio is stereo (2 channels), convert to mono by averaging channels
        if len(audio_array) % 2 == 0:  # Ensure even number of samples for stereo
            audio_array = audio_array.reshape(-1, 2)  # Reshape to [samples, channels]
            audio_array = np.mean(audio_array, axis=1)  # Average stereo to mono

        # Calculate actual duration from original bytes and format
        # PCM_F32LE: 4 bytes per sample * 2 channels = 8 bytes per frame
        actual_duration = len(pcm_data) / (sample_rate * 4 * 2)  # 4 bytes * 2 channels
        self.logger.debug(
            "Prepared %.2fs of audio for madmom analysis (%d samples)",
            actual_duration,
            len(audio_array),
        )

        return audio_array

    def _madmom_beat_analysis(
        self, audio_array: np.ndarray, sample_rate: int
    ) -> SmartFadesAnalysis:
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
        beat_activations = beat_processor.process(audio_array, sample_rate=sample_rate)
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

        # Downbeat tracking (pyCrossfade approach)
        try:
            # RNN Downbeat Processing
            start_time = time.perf_counter()
            downbeat_processor = madmom.features.downbeats.RNNDownBeatProcessor()
            downbeat_activations = downbeat_processor.process(audio_array, sample_rate=sample_rate)
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
        crossfade_bars: int = 8,
        fallback_crossfade_duration: int = 10
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
            # Use smart crossfade with BPM matching
            self.logger.debug(
                "Using smart crossfade: fadeout_bpm=%.1f, fadein_bpm=%.1f",
                fade_out_analysis.bpm,
                fade_in_analysis.bpm,
            )
            
            try:
                return await self._apply_smart_crossfade(
                    fade_out_analysis,
                    fade_in_analysis, 
                    fade_out_part,
                    fade_in_part,
                    pcm_format,
                    crossfade_bars
                )
            except Exception as e:
                self.logger.warning(
                    "Smart crossfade failed: %s, falling back to standard crossfade", e
                )
                # Fall through to standard crossfade
        else:
            # Log why we're using standard crossfade
            if not fade_out_analysis:
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
        optimal_duration = self._calculate_optimal_fade_timing(
            fade_out_analysis, fade_in_analysis, crossfade_bars
        )

        self.logger.debug(
            "Smart fade duration: %.2fs (%d bars, based on beat analysis)",
            optimal_duration,
            crossfade_bars,
        )

        # Write the fade_out_part to a temporary file (following crossfade_pcm_parts pattern)
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

        # Build enhanced filter chain with adaptive frequency filtering
        filter_complex, _ = self._create_enhanced_smart_fade_filters(
            fade_out_analysis,
            fade_in_analysis,
            "[0]",
            "[1]",
            optimal_duration,
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

        # Execute the enhanced smart fade
        _, crossfaded_audio, stderr = await communicate(args, fade_in_part)
        await remove_file(fadeout_filename)

        self.logger.debug(
            "FFmpeg smart fade execution: input_size=%d bytes, output_size=%d bytes, stderr=%s",
            len(fade_in_part),
            len(crossfaded_audio) if crossfaded_audio else 0,
            stderr.decode() if stderr else "(none)",
        )

        if crossfaded_audio:
            self.logger.info(
                "Smart fade successful: duration=%.2fs",
                optimal_duration,
            )
            return crossfaded_audio
        else:
            raise RuntimeError(
                f"Smart crossfade failed. FFmpeg stderr: {stderr.decode() if stderr else '(no stderr output)'}"
            )

    # SMART FADE HELPER METHODS

    def _calculate_optimal_fade_timing(
        self,
        fade_out_analysis: SmartFadesAnalysis,
        fade_in_analysis: SmartFadesAnalysis,
        crossfade_bars: int = 4,
        max_fallback_duration: float = 15.0,
    ) -> float:
        """Calculate precise fade timing based on actual beat positions."""
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
                self.logger.debug("Smart timing from downbeats: %.2fs", smart_duration)
                return smart_duration

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
            self.logger.debug("Smart timing from beats: %.2fs", smart_duration)
            return float(smart_duration)

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

        self.logger.debug("Using BPM fallback timing: %.2fs", capped_duration)
        return capped_duration

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

    def _create_enhanced_smart_fade_filters(
        self,
        fade_out_analysis: SmartFadesAnalysis,
        fade_in_analysis: SmartFadesAnalysis,
        fade_out_input: str,
        fade_in_input: str,
        crossfade_duration: float,
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
        self.logger.debug(
            "Smart fade: fadeout=%.1f BPM, fadein=%.1f BPM, difference=%.1f%%",
            fade_out_analysis.bpm,
            fade_in_analysis.bpm,
            bpm_diff_percent * 100,
        )

        # Pass audio through without tempo modification - the "smart" is in timing and filtering
        filters.append(f"{fade_out_input}anull[fadeout_clean]")  # codespell:ignore
        filters.append(f"{fade_in_input}anull[fadein_clean]")  # codespell:ignore
        current_fade_out_label = "[fadeout_clean]"
        current_fade_in_label = "[fadein_clean]"

        # Step 2: Apply adaptive frequency filters
        frequency_filters = self._create_adaptive_frequency_filters(
            fade_out_analysis, fade_in_analysis, current_fade_out_label, current_fade_in_label
        )
        filters.extend(frequency_filters)

        # Step 3: Apply crossfade
        filters.append(f"[fadeout_hp][fadein_lp]acrossfade=d={crossfade_duration}")

        return filters, current_fade_in_label

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