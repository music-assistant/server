"""Smart Fades Analyzer - Performs audio analysis for smart fades."""

from __future__ import annotations

import asyncio
import time
import warnings
from typing import TYPE_CHECKING

import librosa
import numpy as np
import numpy.typing as npt

from music_assistant.constants import VERBOSE_LOG_LEVEL
from music_assistant.helpers.audio import (
    align_audio_to_frame_boundary,
)
from music_assistant.models.smart_fades import (
    SmartFadesAnalysis,
    SmartFadesAnalysisFragment,
)

if TYPE_CHECKING:
    from music_assistant_models.media_items import AudioFormat

    from music_assistant.controllers.streams.streams_controller import StreamsController

ANALYSIS_FPS = 100


class SmartFadesAnalyzer:
    """Smart fades analyzer that performs audio analysis."""

    def __init__(self, streams: StreamsController) -> None:
        """Initialize smart fades analyzer."""
        self.streams = streams
        self.logger = streams.logger.getChild("smart_fades_analyzer")

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
        self.logger.log(
            VERBOSE_LOG_LEVEL,
            "Starting %s beat analysis for track : %s",
            fragment.name,
            stream_details_name,
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
                "Smart fades %s analysis completed for %s: BPM=%.1f, %d beats, "
                "%d downbeats, confidence=%.2f (took %.2fs)",
                fragment.name,
                stream_details_name,
                analysis.bpm,
                len(analysis.beats),
                len(analysis.downbeats),
                analysis.confidence,
                total_time,
            )
            self.streams.mass.create_task(
                self.streams.mass.music.set_smart_fades_analysis(
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

        self.logger.log(
            VERBOSE_LOG_LEVEL,
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
