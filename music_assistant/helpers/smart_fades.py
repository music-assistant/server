"""Smart Fades - Object-oriented implementation with intelligent fades and adaptive filtering."""

# TODO: Figure out if we can achieve shared buffer with StreamController on full
# current and next track for more EQ options.
# TODO: Refactor the Analyzer into a metadata controller after we have split the controllers
# TODO: Refactor the Mixer into a stream controller after we have split the controllers
from __future__ import annotations

import asyncio
import logging
import time
import warnings
from typing import TYPE_CHECKING

import aiofiles
import librosa
import numpy as np
import numpy.typing as npt
import shortuuid

from music_assistant.constants import VERBOSE_LOG_LEVEL
from music_assistant.helpers.audio import crossfade_pcm_parts, strip_silence
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
# Only apply time stretching if BPM difference is < this %
TIME_STRETCH_BPM_PERCENTAGE_THRESHOLD = 5.0


def align_audio_to_frame_boundary(audio_data: bytes, pcm_format: AudioFormat) -> bytes:
    """Align audio data to frame boundaries by truncating incomplete frames."""
    bytes_per_sample = pcm_format.bit_depth // 8
    frame_size = bytes_per_sample * pcm_format.channels
    valid_bytes = (len(audio_data) // frame_size) * frame_size
    if valid_bytes != len(audio_data):
        logging.getLogger(__name__).debug(
            "Truncating %d bytes from audio buffer to align to frame boundary",
            len(audio_data) - valid_bytes,
        )
        return audio_data[:valid_bytes]
    return audio_data


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
            # Perform beat analysis

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
        mode: SmartFadesMode = SmartFadesMode.SMART_FADES,
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
            # crossfade with standard crossfade
            return await self._default_crossfade(
                fade_in_part,
                fade_out_part,
                pcm_format,
                standard_crossfade_duration,
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
            and mode == SmartFadesMode.SMART_FADES
        ):
            try:
                return await self._apply_smart_crossfade(
                    fade_out_analysis,
                    fade_in_analysis,
                    fade_out_part,
                    fade_in_part,
                    pcm_format,
                )
            except Exception as e:
                self.logger.warning(
                    "Smart crossfade failed: %s, falling back to standard crossfade", e
                )

        return await self._default_crossfade(
            fade_in_part,
            fade_out_part,
            pcm_format,
            standard_crossfade_duration,
        )

    async def _apply_smart_crossfade(
        self,
        fade_out_analysis: SmartFadesAnalysis,
        fade_in_analysis: SmartFadesAnalysis,
        fade_out_part: bytes,
        fade_in_part: bytes,
        pcm_format: AudioFormat,
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

        # Calculate initial crossfade duration (may be adjusted later for downbeat alignment)
        initial_crossfade_duration = self._calculate_crossfade_duration(
            crossfade_bars=crossfade_bars,
            fade_in_analysis=fade_in_analysis,
        )

        # Create time stretch filters - needs to know crossfade duration to complete
        # tempo ramping before the crossfade starts
        time_stretch_filters, tempo_factor = self._create_time_stretch_filters(
            fade_out_analysis=fade_out_analysis,
            fade_in_analysis=fade_in_analysis,
            crossfade_bars=crossfade_bars,
            crossfade_duration=initial_crossfade_duration,
        )
        filters.extend(time_stretch_filters)

        crossfade_duration = initial_crossfade_duration

        # Check if we would have enough audio after beat alignment for the crossfade
        if (
            fadein_start_pos is not None
            and fadein_start_pos + crossfade_duration > SMART_CROSSFADE_DURATION
        ):
            self.logger.debug(
                "Skipping beat alignment: not enough audio after trim (%.1fs + %.1fs > %.1fs)",
                fadein_start_pos,
                crossfade_duration,
                SMART_CROSSFADE_DURATION,
            )
            # Skip beat alignment
            fadein_start_pos = None

        # Adjust crossfade duration to align with outgoing track's downbeats
        # This prevents echo-ey sounds when both tracks have kicks during the crossfade
        crossfade_duration = self._adjust_crossfade_to_downbeats(
            fade_out_analysis=fade_out_analysis,
            crossfade_duration=crossfade_duration,
            fadein_start_pos=fadein_start_pos,
            tempo_factor=tempo_factor,
        )

        beat_align_filters = self._trim_incoming_track_to_downbeat(
            fadein_start_pos=fadein_start_pos,
            fadeout_input_label="[fadeout_stretched]",
            fadein_input_label="[1]",
        )
        filters.extend(beat_align_filters)

        self.logger.debug(
            "Smart fade: out_bpm=%.1f, in_bpm=%.1f, %d bars, crossfade: %.2fs%s",
            fade_out_analysis.bpm,
            fade_in_analysis.bpm,
            crossfade_bars,
            crossfade_duration,
            ", beat-aligned" if fadein_start_pos else "",
        )
        frequency_filters = self._apply_eq_filters(
            fade_out_analysis=fade_out_analysis,
            fade_in_analysis=fade_in_analysis,
            fade_out_label="[fadeout_beatalign]",
            fade_in_label="[fadein_beatalign]",
            crossfade_duration=crossfade_duration,
            crossfade_bars=crossfade_bars,
        )
        filters.extend(frequency_filters)

        # Apply linear crossfade for now since we already use EQ sweeps for smoothness
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
        actual_duration = min(musical_duration, SMART_CROSSFADE_DURATION)

        # Log if we had to constrain the duration
        if musical_duration > SMART_CROSSFADE_DURATION:
            self.logger.debug(
                "Constraining crossfade duration from %.1fs to %.1fs (buffer limit)",
                musical_duration,
                actual_duration,
            )

        return actual_duration

    def _extrapolate_downbeats(
        self,
        downbeats: npt.NDArray[np.float64],
        tempo_factor: float,
        buffer_size: float = SMART_CROSSFADE_DURATION,
    ) -> npt.NDArray[np.float64]:
        """Extrapolate downbeats based on actual intervals when detection is incomplete.

        This is needed when we want to perform beat alignment in an 'atmospheric' outro
        that does not have any detected downbeats.
        """
        if len(downbeats) < 3:
            # Need at least 3 downbeats to reliably calculate interval
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
            self.logger.debug(
                "Downbeat intervals too inconsistent (std=%.3fs) for extrapolation",
                std_interval,
            )
            return adjusted_downbeats

        # Adjust the interval for time stretching
        # When slowing down (tempo_factor < 1.0), intervals get longer
        adjusted_interval = median_interval / tempo_factor

        # Extrapolate forward from last adjusted downbeat using adjusted interval
        extrapolated = []
        current_pos = last_downbeat + adjusted_interval
        max_extrapolation_distance = 25.0  # Don't extrapolate more than 25s

        while (
            current_pos < buffer_size
            and (current_pos - last_downbeat) <= max_extrapolation_distance
        ):
            extrapolated.append(current_pos)
            current_pos += adjusted_interval

        if extrapolated:
            self.logger.debug(
                "Extrapolated %d downbeats (adjusted_interval=%.3fs, original=%.3fs) "
                "from %.2fs to %.2fs",
                len(extrapolated),
                adjusted_interval,
                median_interval,
                last_downbeat,
                extrapolated[-1],
            )
            # Combine adjusted detected downbeats and extrapolated downbeats
            return np.concatenate([adjusted_downbeats, np.array(extrapolated)])

        return adjusted_downbeats

    def _adjust_crossfade_to_downbeats(
        self,
        fade_out_analysis: SmartFadesAnalysis,
        crossfade_duration: float,
        fadein_start_pos: float | None,
        tempo_factor: float,
    ) -> float:
        """Adjust crossfade duration to align with outgoing track's downbeats.

        This ensures the crossfade starts on a downbeat of the outgoing track,
        preventing echo-ey sounds when both tracks have kicks during the crossfade.

        The downbeat positions are adjusted for time stretching - when tempo_factor < 1.0
        (slowing down), beats take longer to reach their position in the stretched audio.
        """
        # If we don't have downbeats or beat alignment is disabled, return original duration
        if len(fade_out_analysis.downbeats) == 0 or fadein_start_pos is None:
            return crossfade_duration

        # Extrapolate downbeats if needed (e.g., when beat detection is incomplete)
        # This returns downbeats already adjusted for time stretching
        adjusted_downbeats = self._extrapolate_downbeats(
            fade_out_analysis.downbeats, tempo_factor=tempo_factor
        )

        # Calculate where the crossfade would start in the buffer
        ideal_start_pos = SMART_CROSSFADE_DURATION - crossfade_duration

        # Debug: Show all downbeats and the ideal position
        self.logger.debug(
            "Downbeat adjustment - ideal_start=%.2fs (buffer=%.1fs - crossfade=%.2fs), "
            "fadein_start=%.2fs, tempo_factor=%.4f",
            ideal_start_pos,
            SMART_CROSSFADE_DURATION,
            crossfade_duration,
            fadein_start_pos,
            tempo_factor,
        )

        # Find the closest downbeats (earlier and later)
        earlier_downbeat = None
        later_downbeat = None

        for downbeat in adjusted_downbeats:
            if downbeat <= ideal_start_pos:
                earlier_downbeat = downbeat
            elif downbeat > ideal_start_pos and later_downbeat is None:
                later_downbeat = downbeat
                break

        # Try earlier downbeat first (longer crossfade)
        if earlier_downbeat is not None:
            adjusted_duration = float(SMART_CROSSFADE_DURATION - earlier_downbeat)
            # Check if this fits in the buffer
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
            # Check if this fits in the buffer
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

    def _calculate_optimal_crossfade_bars(
        self, fade_out_analysis: SmartFadesAnalysis, fade_in_analysis: SmartFadesAnalysis
    ) -> int:
        """Calculate optimal crossfade bars that fit in available buffer."""
        bpm_in = fade_in_analysis.bpm
        bpm_out = fade_out_analysis.bpm
        bpm_diff_percent = abs(1.0 - bpm_in / bpm_out) * 100

        # Calculate ideal bars based on BPM compatibility. We link this to time stretching
        # so we avoid extreme tempo changes over short fades.
        ideal_bars = 10 if bpm_diff_percent <= TIME_STRETCH_BPM_PERCENTAGE_THRESHOLD else 6

        # We could encounter songs that have a long athmospheric intro without any downbeats
        # In those cases, we need to reduce the bars until it fits in the fadein buffer.
        for bars in [ideal_bars, 8, 6, 4, 2, 1]:
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
            fade_out_beats: npt.NDArray[np.float64],
            fade_in_beats: npt.NDArray[np.float64],
            num_beats: int,
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

    def _trim_incoming_track_to_downbeat(
        self,
        fadein_start_pos: float | None,
        fadeout_input_label: str = "[0]",
        fadein_input_label: str = "[1]",
    ) -> list[str]:
        """Perform beat alignment preprocessing.

        The incoming track is trimmed to its first downbeat position.
        No adjustment is needed for time stretching since the incoming track
        is not stretched - it's already at the target BPM.
        """
        # Just relabel in case we cannot perform beat alignment
        if fadein_start_pos is None:
            return [
                f"{fadeout_input_label}anull[fadeout_beatalign]",  # codespell:ignore anull
                f"{fadein_input_label}anull[fadein_beatalign]",  # codespell:ignore anull
            ]

        # Trim incoming track to start at first downbeat position
        return [
            f"{fadeout_input_label}anull[fadeout_beatalign]",  # codespell:ignore anull
            f"{fadein_input_label}atrim=start={fadein_start_pos},asetpts=PTS-STARTPTS[fadein_beatalign]",
        ]

    def _create_time_stretch_filters(
        self,
        fade_out_analysis: SmartFadesAnalysis,
        fade_in_analysis: SmartFadesAnalysis,
        crossfade_bars: int,
        crossfade_duration: float,
    ) -> tuple[list[str], float]:
        """Create FFmpeg filters to gradually adjust tempo from original BPM to target BPM.

        The tempo ramping is completed before the crossfade starts to ensure perfect beat alignment
        throughout the entire crossfade region.
        """
        # Check if time stretching should be applied (BPM difference < 3%)
        original_bpm = fade_out_analysis.bpm
        target_bpm = fade_in_analysis.bpm
        bpm_ratio = target_bpm / original_bpm
        bpm_diff_percent = abs(1.0 - bpm_ratio) * 100

        # If no time stretching needed, return passthrough filter and no tempo change
        if not (
            0.1 < bpm_diff_percent <= TIME_STRETCH_BPM_PERCENTAGE_THRESHOLD and crossfade_bars > 4
        ):
            return ["[0]anull[fadeout_stretched]"], 1.0  # codespell:ignore anull

        # Log that we're applying time stretching
        self.logger.debug(
            "Time stretch: %.1f%% BPM diff, adjusting %.1f -> %.1f BPM, crossfade starts at %.1fs",
            bpm_diff_percent,
            original_bpm,
            target_bpm,
            SMART_CROSSFADE_DURATION - crossfade_duration,
        )

        # Use uniform rubberband time stretching for the entire buffer
        # This ensures downbeat adjustment calculations are accurate and beat alignment is perfect
        # Rubberband is a high-quality music-specific algorithm optimized for music
        self.logger.debug(
            "Time stretch (rubberband uniform): %.1f BPM -> %.1f BPM (factor=%.4f)",
            original_bpm,
            target_bpm,
            bpm_ratio,
        )
        return [
            f"[0]rubberband=tempo={bpm_ratio:.6f}:transients=mixed:detector=soft:pitchq=quality"
            "[fadeout_stretched]"
        ], bpm_ratio

    def _apply_eq_filters(
        self,
        fade_out_analysis: SmartFadesAnalysis,
        fade_in_analysis: SmartFadesAnalysis,
        fade_out_label: str,
        fade_in_label: str,
        crossfade_duration: float,
        crossfade_bars: int,
    ) -> list[str]:
        """Create LP / HP complementary filters using frequency sweeps for smooth transitions."""
        # Calculate target frequency based on average BPM
        avg_bpm = (fade_out_analysis.bpm + fade_in_analysis.bpm) / 2
        bpm_ratio = fade_in_analysis.bpm / fade_out_analysis.bpm

        # 90 BPM -> 1500Hz, 140 BPM -> 2500Hz
        crossover_freq = int(np.clip(1500 + (avg_bpm - 90) * 20, 1500, 2500))

        # Adjust for BPM mismatch
        if abs(bpm_ratio - 1.0) > 0.3:
            crossover_freq = int(crossover_freq * 0.85)

        # Extended lowpass effect to gradually remove bass frequencies
        fadeout_eq_duration = min(max(crossfade_duration * 2.5, 8.0), SMART_CROSSFADE_DURATION)

        # Quicker highpass removal to avoid lingering vocals after crossfade
        fadein_eq_duration = crossfade_duration / 1.5

        # Calculate when the EQ sweep should start
        # The crossfade always happens at the END of the buffer, regardless of beat alignment
        fadeout_eq_start = max(0, SMART_CROSSFADE_DURATION - fadeout_eq_duration)

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

        self.logger.debug(
            "EQ: crossover=%dHz, EQ fadeout duration=%.1fs,"
            " EQ fadein duration=%.1fs, BPM=%.1f, BPM ratio=%.2f,"
            " EQ curves: %s/%s",
            crossover_freq,
            fadeout_eq_duration,
            fadein_eq_duration,
            avg_bpm,
            bpm_ratio,
            fadeout_curve,
            fadein_curve,
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
            curve_type=fadeout_curve,
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
            curve_type=fadein_curve,
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
            fade_out_pcm_format=pcm_format,
        )
        # Post-crossfade: incoming track minus the crossfaded portion
        post_crossfade = fade_in_part[crossfade_size:]
        # Full result: everything concatenated
        return pre_crossfade + crossfaded_section + post_crossfade
