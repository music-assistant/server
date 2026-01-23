"""Extended Analysis Processor - Final analysis on accumulated streaming features."""

from __future__ import annotations

import asyncio
import logging
import time
import warnings
from typing import TYPE_CHECKING

import librosa
import numpy as np
import numpy.typing as npt

from music_assistant.models.smart_fades import (
    ExtendedSmartFadesAnalysis,
    MusicalKey,
    PhraseBoundary,
    TimeSignature,
)

if TYPE_CHECKING:
    from music_assistant.controllers.streams.smart_fades.feature_accumulator import (
        FeatureAccumulator,
    )

# Krumhansl-Schmuckler key profiles for key estimation
# These represent the expected distribution of pitch classes in major/minor keys
# Values from Krumhansl & Kessler (1982)
MAJOR_PROFILE = np.array(
    [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88],
    dtype=np.float64,
)
MINOR_PROFILE = np.array(
    [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17],
    dtype=np.float64,
)

# Pitch class names for key root identification
PITCH_CLASSES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


class ExtendedAnalysisProcessor:
    """Processes accumulated features to produce extended smart fades analysis.

    This class performs final analysis on the lightweight features accumulated
    during streaming:
    - Beat tracking using pre-computed onset envelope
    - Key estimation using Krumhansl-Schmuckler profiles on averaged chroma
    - Phrase boundary detection using onset density changes
    - Energy/brightness curve downsampling to per-second resolution
    """

    def __init__(self, logger: logging.Logger) -> None:
        """Initialize the extended analysis processor.

        :param logger: Logger instance for debug output.
        """
        self.logger = logger

    async def analyze(
        self,
        accumulator: FeatureAccumulator,
        sample_rate: int,
        hop_length: int,
    ) -> ExtendedSmartFadesAnalysis | None:
        """Perform extended analysis on accumulated features.

        This runs CPU-intensive analysis steps in separate thread pool calls
        with event loop yields between them to prevent audio dropout.

        :param accumulator: Feature accumulator with streaming data.
        :param sample_rate: Audio sample rate in Hz.
        :param hop_length: Hop length used during feature extraction.
        :return: Extended analysis result, or None if analysis fails.
        """
        if not accumulator.has_sufficient_data():
            self.logger.warning("Insufficient data for extended analysis")
            return None

        start_time = time.perf_counter()
        try:
            duration = accumulator.get_duration()

            # Get accumulated features (fast, but do in thread to be safe)
            onset_envelope = accumulator.get_onset_envelope()
            chroma = accumulator.get_chroma()
            rms = accumulator.get_rms()
            spectral_centroid = accumulator.get_spectral_centroid()

            # Yield to event loop
            await asyncio.sleep(0)

            # Beat tracking (heavy - ~0.75s)
            bpm, beats, downbeats, confidence = await asyncio.to_thread(
                self._beat_tracking, onset_envelope, sample_rate, hop_length
            )

            if bpm is None or beats is None or downbeats is None:
                self.logger.warning("Beat tracking failed")
                return None

            # Yield to event loop
            await asyncio.sleep(0)

            # Time signature estimation (fast)
            time_signature = await asyncio.to_thread(
                self._estimate_time_signature, beats, onset_envelope, sample_rate, hop_length
            )

            # Yield to event loop
            await asyncio.sleep(0)

            # Key estimation (fast)
            musical_key = await asyncio.to_thread(self._estimate_key, chroma)

            # Yield to event loop
            await asyncio.sleep(0)

            # Phrase boundary detection (heavy - ~0.4s)
            phrase_boundaries = await asyncio.to_thread(
                self._detect_phrase_boundaries,
                onset_envelope,
                chroma,
                rms,
                spectral_centroid,
                beats,
                bpm,
                time_signature,
                sample_rate,
                hop_length,
            )

            # Yield to event loop
            await asyncio.sleep(0)

            # Downsample curves (fast)
            energy_curve = self._downsample_to_per_second(rms, duration, sample_rate, hop_length)
            spectral_curve = self._downsample_to_per_second(
                spectral_centroid, duration, sample_rate, hop_length
            )

            elapsed = time.perf_counter() - start_time
            self.logger.debug(
                "Extended analysis completed in %.3fs (duration: %.1fs)",
                elapsed,
                duration,
            )

            return ExtendedSmartFadesAnalysis(
                bpm=bpm,
                beats=beats,
                downbeats=downbeats,
                confidence=confidence,
                duration=duration,
                musical_key=musical_key,
                time_signature=time_signature,
                phrase_boundaries=phrase_boundaries,
                energy_curve=energy_curve,
                spectral_centroid_curve=(
                    spectral_curve.astype(np.float32) if spectral_curve is not None else None
                ),
                full_song_analysis=True,
                analysis_version=3,
            )
        except Exception as e:
            self.logger.exception("Extended analysis failed: %s", e)
            return None

    def _beat_tracking(
        self,
        onset_envelope: npt.NDArray[np.float32],
        sample_rate: int,
        hop_length: int,
    ) -> tuple[float | None, npt.NDArray[np.float64] | None, npt.NDArray[np.float64] | None, float]:
        """Perform beat tracking using pre-computed onset envelope.

        :param onset_envelope: Pre-computed onset strength envelope.
        :param sample_rate: Audio sample rate in Hz.
        :param hop_length: Hop length used during feature extraction.
        :return: Tuple of (bpm, beat_times, downbeat_times, confidence).
        """
        if len(onset_envelope) < 10:
            return None, None, None, 0.0

        try:
            # Suppress librosa UserWarnings about empty mel filters
            with warnings.catch_warnings():
                warnings.filterwarnings(
                    "ignore",
                    message="Empty filters detected in mel frequency basis",
                    category=UserWarning,
                )
                tempo, beat_frames = librosa.beat.beat_track(
                    onset_envelope=onset_envelope,
                    sr=sample_rate,
                    hop_length=hop_length,
                )

            if len(beat_frames) < 2:
                return None, None, None, 0.0

            # Convert frames to times
            beat_times = librosa.frames_to_time(beat_frames, sr=sample_rate, hop_length=hop_length)

            # Extract BPM value
            bpm = float(tempo.item()) if hasattr(tempo, "item") else float(tempo)

            # Calculate confidence based on beat interval consistency
            intervals = np.diff(beat_times)
            interval_std = np.std(intervals)
            interval_mean = np.mean(intervals)
            cv = interval_std / interval_mean if interval_mean > 0 else 1.0
            confidence = max(0.1, 1.0 - cv)

            # Estimate downbeats
            downbeats = self._estimate_downbeats(beat_times, bpm)

            return bpm, beat_times, downbeats, float(confidence)

        except Exception as e:
            self.logger.warning("Beat tracking failed: %s", e)
            return None, None, None, 0.0

    def _estimate_downbeats(
        self,
        beats: npt.NDArray[np.float64],
        bpm: float,
    ) -> npt.NDArray[np.float64]:
        """Estimate downbeats using musical logic and beat consistency.

        :param beats: Array of beat times in seconds.
        :param bpm: Detected BPM.
        :return: Array of estimated downbeat times.
        """
        if len(beats) < 4:
            return beats[:1] if len(beats) > 0 else np.array([])

        expected_beat_interval = 60.0 / bpm
        best_offset = 0
        best_consistency = 0.0

        # Try different starting offsets to find most consistent downbeat pattern
        for offset in range(min(4, len(beats))):
            downbeat_candidates = beats[offset::4]

            if len(downbeat_candidates) < 2:
                continue

            intervals = np.diff(downbeat_candidates)
            expected_downbeat_interval = 4 * expected_beat_interval

            interval_errors = (
                np.abs(intervals - expected_downbeat_interval) / expected_downbeat_interval
            )
            consistency = 1.0 - np.mean(interval_errors)

            if consistency > best_consistency:
                best_consistency = float(consistency)
                best_offset = offset

        return beats[best_offset::4]

    def _estimate_key(
        self,
        chroma: npt.NDArray[np.float32],
    ) -> MusicalKey | None:
        """Estimate musical key using Krumhansl-Schmuckler profiles.

        :param chroma: Chroma features, shape (12, n_frames).
        :return: Estimated key with confidence, or None if estimation fails.
        """
        if chroma.shape[1] < 10:
            return None

        try:
            # Average chroma across time
            chroma_avg = np.mean(chroma, axis=1)

            # Normalize
            chroma_norm = chroma_avg / (np.linalg.norm(chroma_avg) + 1e-8)

            best_correlation = -1.0
            best_root = 0
            best_mode = "major"

            # Correlate with all 12 major and minor key profiles
            for root in range(12):
                # Rotate profile to match key root
                major_rotated = np.roll(MAJOR_PROFILE, root)
                minor_rotated = np.roll(MINOR_PROFILE, root)

                # Normalize profiles
                major_norm = major_rotated / np.linalg.norm(major_rotated)
                minor_norm = minor_rotated / np.linalg.norm(minor_rotated)

                # Compute correlations
                major_corr = float(np.corrcoef(chroma_norm, major_norm)[0, 1])
                minor_corr = float(np.corrcoef(chroma_norm, minor_norm)[0, 1])

                if major_corr > best_correlation:
                    best_correlation = major_corr
                    best_root = root
                    best_mode = "major"

                if minor_corr > best_correlation:
                    best_correlation = minor_corr
                    best_root = root
                    best_mode = "minor"

            # Convert correlation to confidence [0, 1]
            # Correlation can be negative, so we scale from [-1, 1] to [0, 1]
            confidence = (best_correlation + 1.0) / 2.0

            return MusicalKey(
                root=PITCH_CLASSES[best_root],
                mode=best_mode,
                confidence=float(confidence),
            )

        except Exception as e:
            self.logger.warning("Key estimation failed: %s", e)
            return None

    def _detect_phrase_boundaries(
        self,
        _onset_envelope: npt.NDArray[np.float32],
        _chroma: npt.NDArray[np.float32],
        rms: npt.NDArray[np.float32],
        _spectral_centroid: npt.NDArray[np.float64],
        beats: npt.NDArray[np.float64],
        bpm: float,
        time_signature: TimeSignature,
        sample_rate: int,
        hop_length: int,
    ) -> list[PhraseBoundary]:
        """Detect phrase boundaries using hybrid energy-anchored approach.

        Algorithm:
        1. Detect major energy transitions (>30% RMS change)
        2. Snap each transition to nearest downbeat
        3. Use the biggest transition as anchor point
        4. Count 8-bar phrases from anchor in both directions
        5. All boundaries are "phrase" type

        This approach finds where the music actually changes (e.g., beat drop)
        and uses that as the reference point for counting phrases.

        :param _onset_envelope: Pre-computed onset strength envelope (unused, kept for API).
        :param _chroma: Chroma features (unused, kept for API).
        :param rms: RMS energy, shape (1, n_frames).
        :param _spectral_centroid: Spectral centroid (unused, kept for API).
        :param beats: Beat times in seconds.
        :param bpm: Detected BPM.
        :param time_signature: Estimated time signature.
        :param sample_rate: Audio sample rate in Hz.
        :param hop_length: Hop length used during feature extraction.
        :return: List of detected phrase boundaries.
        """
        beats_per_bar = time_signature.beats_per_bar
        bars_per_phrase = 8  # Standard phrase length

        # Calculate timing constants
        seconds_per_beat = 60.0 / bpm
        seconds_per_bar = seconds_per_beat * beats_per_bar
        seconds_per_phrase = seconds_per_bar * bars_per_phrase

        if len(beats) < beats_per_bar * 2:
            return []

        total_duration = float(beats[-1])

        # Calculate downbeats (first beat of each bar)
        downbeats = beats[::beats_per_bar]

        self.logger.debug(
            "Phrase detection: bpm=%.1f, time_sig=%d/4, phrase_duration=%.1fs, total=%.1fs",
            bpm,
            beats_per_bar,
            seconds_per_phrase,
            total_duration,
        )

        # Prepare RMS for energy analysis
        rms_flat = rms.flatten() if rms.ndim > 1 else rms
        frames_per_second = sample_rate / hop_length

        try:
            # Step 1: Find major UPWARD energy transitions (beat drops = energy increases)
            # Use 4-bar window for smoother comparison (less susceptible to local fluctuations)
            transitions: list[tuple[float, float]] = []  # (time, rms_increase)
            window_seconds = seconds_per_bar * 4  # 4-bar window

            for downbeat_time in downbeats:
                if (
                    downbeat_time < window_seconds
                    or downbeat_time > total_duration - window_seconds
                ):
                    continue

                frame_idx = int(downbeat_time * frames_per_second)
                window_frames = int(window_seconds * frames_per_second)

                start_before = max(0, frame_idx - window_frames)
                end_before = frame_idx
                start_after = frame_idx
                end_after = min(len(rms_flat), frame_idx + window_frames)

                if end_before <= start_before or end_after <= start_after:
                    continue

                rms_before = float(np.mean(rms_flat[start_before:end_before]))
                rms_after = float(np.mean(rms_flat[start_after:end_after]))

                # Only consider UPWARD transitions (energy increase = beat drop)
                if rms_after > rms_before:
                    rms_increase = (rms_after - rms_before) / max(rms_before, 1e-8)
                    # Higher threshold (50%) for more confident detection
                    if rms_increase > 0.5:
                        transitions.append((downbeat_time, rms_increase))

            self.logger.debug(
                "Phrase detection: found %d major upward transitions (>50%% energy increase)",
                len(transitions),
            )

            # Step 2: Use FIRST major upward transition as anchor (intro → drop)
            if transitions:
                # Sort by time, pick the first one (the intro→drop)
                transitions.sort(key=lambda x: x[0])
                anchor_time = transitions[0][0]
                anchor_change = transitions[0][1]
                self.logger.debug(
                    "Phrase detection: anchor at %.1fs (%.0f%% energy increase)",
                    anchor_time,
                    anchor_change * 100,
                )
            else:
                # No major transitions found - fall back to first downbeat after 8 bars
                anchor_time = (
                    seconds_per_phrase if len(downbeats) > bars_per_phrase else downbeats[0]
                )
                self.logger.debug(
                    "Phrase detection: no major transitions, using default anchor at %.1fs",
                    anchor_time,
                )

            # Step 3: Count phrases from anchor in both directions
            boundaries: list[PhraseBoundary] = []

            # Forward from anchor
            phrase_time = anchor_time
            while phrase_time < total_duration - seconds_per_bar:
                # Snap to nearest downbeat
                downbeat_idx = int(np.argmin(np.abs(downbeats - phrase_time)))
                snapped_time = float(downbeats[downbeat_idx])

                # Skip if too close to start or end
                if (
                    snapped_time > seconds_per_bar
                    and snapped_time < total_duration - seconds_per_bar
                ):
                    boundaries.append(
                        PhraseBoundary(
                            time=snapped_time,
                            confidence=0.7,  # Base confidence for structure-based
                            boundary_type="phrase",
                        )
                    )

                phrase_time += seconds_per_phrase

            # Backward from anchor
            phrase_time = anchor_time - seconds_per_phrase
            while phrase_time > seconds_per_bar:
                # Snap to nearest downbeat
                downbeat_idx = int(np.argmin(np.abs(downbeats - phrase_time)))
                snapped_time = float(downbeats[downbeat_idx])

                # Skip if too close to start
                if snapped_time > seconds_per_bar:
                    boundaries.append(
                        PhraseBoundary(
                            time=snapped_time,
                            confidence=0.7,
                            boundary_type="phrase",
                        )
                    )

                phrase_time -= seconds_per_phrase

            # Sort by time and remove duplicates
            boundaries.sort(key=lambda b: b.time)
            unique_boundaries: list[PhraseBoundary] = []
            last_time = -seconds_per_bar  # Allow first boundary
            for boundary in boundaries:
                if boundary.time - last_time >= seconds_per_bar:  # At least 1 bar apart
                    unique_boundaries.append(boundary)
                    last_time = boundary.time

            self.logger.debug(
                "Phrase detection: placed %d boundaries at %d-bar intervals from anchor",
                len(unique_boundaries),
                bars_per_phrase,
            )

            return unique_boundaries

        except Exception as e:
            self.logger.warning("Phrase boundary detection failed: %s", e)
            return []

    def _downsample_to_per_second(
        self,
        features: npt.NDArray[np.floating],
        duration: float,
        sample_rate: int,
        hop_length: int,
    ) -> npt.NDArray[np.float32] | None:
        """Downsample per-frame features to per-second resolution.

        :param features: Feature array, shape (1, n_frames) or (n_frames,).
        :param duration: Total duration in seconds.
        :param sample_rate: Audio sample rate in Hz.
        :param hop_length: Hop length used during feature extraction.
        :return: Per-second feature array, or None if input is empty.
        """
        if features.size == 0:
            return None

        # Flatten if 2D with first dim = 1
        if features.ndim == 2 and features.shape[0] == 1:
            features = features.flatten()

        if len(features) == 0:
            return None

        # Calculate frames per second
        frames_per_second = sample_rate / hop_length
        num_seconds = int(np.ceil(duration))

        if num_seconds == 0:
            return None

        result = np.zeros(num_seconds, dtype=np.float32)

        for sec in range(num_seconds):
            start_frame = int(sec * frames_per_second)
            end_frame = int((sec + 1) * frames_per_second)
            end_frame = min(end_frame, len(features))

            if start_frame < len(features) and end_frame > start_frame:
                result[sec] = np.mean(features[start_frame:end_frame])

        return result

    # ==================================================================================
    # PHRASE BOUNDARY DETECTION
    # ==================================================================================
    #
    # The following functions implement memory-efficient phrase boundary detection using
    # multi-feature novelty analysis. Instead of computing a full O(n²) self-similarity
    # matrix, we use sliding window comparisons for O(n) memory complexity.
    #
    # Pipeline:
    #   1. _estimate_time_signature()      - Detect beats per bar from accent patterns
    #   2. _build_combined_features()      - Normalize and stack all feature types
    #   3. _compute_feature_novelty()      - Sliding window novelty (main detection)
    #   4. _compute_rms_novelty()          - Energy-based boundary detection
    #   5. _fuse_novelty_signals()         - Weighted combination of novelty curves
    #   6. _find_peaks_at_bar_boundaries() - Constrain to musically valid positions
    #   7. _detect_phrase_boundaries()     - Orchestrates the full pipeline
    #
    # ==================================================================================

    def _estimate_time_signature(
        self,
        beats: npt.NDArray[np.float64],
        onset_envelope: npt.NDArray[np.float32],
        sample_rate: int,
        hop_length: int,
    ) -> TimeSignature:
        """Estimate time signature by analyzing accent patterns on beat positions.

        Tests common time signatures (3/4, 4/4, 6/8) and finds which grouping
        best aligns with onset strength accents.

        :param beats: Beat times in seconds.
        :param onset_envelope: Onset strength envelope.
        :param sample_rate: Audio sample rate in Hz.
        :param hop_length: Hop length used during feature extraction.
        :return: Estimated time signature with confidence.
        """
        if len(beats) < 12:
            # Not enough beats to estimate, default to 4/4
            return TimeSignature(beats_per_bar=4, confidence=0.5)

        # Convert beat times to frames
        beat_frames = librosa.time_to_frames(beats, sr=sample_rate, hop_length=hop_length)
        beat_frames = beat_frames[beat_frames < len(onset_envelope)]

        if len(beat_frames) < 12:
            return TimeSignature(beats_per_bar=4, confidence=0.5)

        # Get onset strength at each beat
        beat_strengths = onset_envelope[beat_frames]

        best_beats_per_bar = 4
        best_score = -1.0
        scores: dict[int, float] = {}  # Store all scores for comparison

        # Test common time signatures
        for beats_per_bar in [3, 4, 6]:
            if len(beat_strengths) < beats_per_bar * 3:
                continue

            # Group beats into bars and check if first beat of each bar is accented
            num_complete_bars = len(beat_strengths) // beats_per_bar
            if num_complete_bars < 2:
                continue

            # Reshape into bars
            truncated = beat_strengths[: num_complete_bars * beats_per_bar]
            bars = truncated.reshape(num_complete_bars, beats_per_bar)

            # Calculate how much stronger the downbeat is compared to other beats
            downbeat_strengths = bars[:, 0]
            other_strengths = bars[:, 1:].mean(axis=1)

            # Score = ratio of downbeat strength to other beats
            # Higher score means clearer accent pattern for this time signature
            with np.errstate(divide="ignore", invalid="ignore"):
                ratios = downbeat_strengths / (other_strengths + 1e-8)
                score = float(np.median(ratios))

            scores[beats_per_bar] = score

            if score > best_score:
                best_score = score
                best_beats_per_bar = beats_per_bar

        # BIAS TOWARD 4/4: It's ~95% of popular music
        # Only deviate from 4/4 if alternative is significantly better (>20%)
        score_4_4 = scores.get(4, 0.0)
        if best_beats_per_bar != 4 and score_4_4 > 0:
            if best_score < score_4_4 * 1.2:
                self.logger.debug(
                    "Time signature: preferring 4/4 (score=%.2f) over %d/4 (score=%.2f)",
                    score_4_4,
                    best_beats_per_bar,
                    best_score,
                )
                best_beats_per_bar = 4
                best_score = score_4_4

        # Convert score to confidence (ratio of 1.5+ is good, 2.0+ is excellent)
        confidence = min(1.0, max(0.3, (best_score - 1.0) / 1.0))

        self.logger.debug(
            "Time signature estimation: %d/4 with confidence %.2f (score=%.2f)",
            best_beats_per_bar,
            confidence,
            best_score,
        )

        return TimeSignature(beats_per_bar=best_beats_per_bar, confidence=float(confidence))

    def _build_combined_features(
        self,
        onset_envelope: npt.NDArray[np.float32],
        chroma: npt.NDArray[np.float32],
        rms: npt.NDArray[np.float32],
        spectral_centroid: npt.NDArray[np.float64],
    ) -> npt.NDArray[np.float32]:
        """Build normalized combined feature matrix for novelty detection.

        Stacks and normalizes all available features into a single matrix
        for multi-feature novelty computation.

        :param onset_envelope: Onset strength envelope, shape (n_frames,).
        :param chroma: Chroma features, shape (12, n_frames).
        :param rms: RMS energy, shape (1, n_frames).
        :param spectral_centroid: Spectral centroid, shape (1, n_frames).
        :return: Combined features, shape (n_features, n_frames).
        """
        # Flatten 2D arrays
        rms_flat = rms.flatten() if rms.ndim == 2 else rms
        centroid_flat = (
            spectral_centroid.flatten() if spectral_centroid.ndim == 2 else spectral_centroid
        )

        # Find minimum length across all features
        min_len = min(
            len(onset_envelope),
            chroma.shape[1] if chroma.ndim == 2 else len(chroma),
            len(rms_flat),
            len(centroid_flat),
        )

        if min_len == 0:
            return np.zeros((15, 0), dtype=np.float32)

        # Truncate all to same length
        onset_trunc = onset_envelope[:min_len]
        chroma_trunc = chroma[:, :min_len] if chroma.ndim == 2 else chroma[:min_len]
        rms_trunc = rms_flat[:min_len]
        centroid_trunc = centroid_flat[:min_len]

        # Normalize each feature to [0, 1] range
        def normalize(arr: npt.NDArray) -> npt.NDArray[np.float32]:
            arr = arr.astype(np.float32)
            min_val, max_val = arr.min(), arr.max()
            if max_val - min_val > 1e-8:
                return (arr - min_val) / (max_val - min_val)
            return np.zeros_like(arr)

        onset_norm = normalize(onset_trunc).reshape(1, -1)
        rms_norm = normalize(rms_trunc).reshape(1, -1)
        centroid_norm = normalize(centroid_trunc).reshape(1, -1)

        # Normalize chroma per-bin
        chroma_norm = np.zeros_like(chroma_trunc, dtype=np.float32)
        for i in range(12):
            chroma_norm[i] = normalize(chroma_trunc[i])

        # Stack all features: onset(1) + rms(1) + centroid(1) + chroma(12) = 15 features
        combined = np.vstack([onset_norm, rms_norm, centroid_norm, chroma_norm])

        return combined.astype(np.float32)

    def _compute_feature_novelty(
        self,
        features: npt.NDArray[np.float32],
        kernel_size: int,
    ) -> npt.NDArray[np.float32]:
        """Compute novelty function using sliding window comparison.

        Memory-efficient alternative to full self-similarity matrix.
        Computes cosine distance between mean feature vectors before and after
        each time point.

        :param features: Combined features, shape (n_features, n_frames).
        :param kernel_size: Window size in frames for before/after comparison.
        :return: Novelty function, shape (n_frames,).
        """
        n_frames = features.shape[1]
        novelty = np.zeros(n_frames, dtype=np.float32)

        if n_frames < kernel_size:
            return novelty

        half_k = kernel_size // 2

        for i in range(half_k, n_frames - half_k):
            # Get windows before and after current position
            before = features[:, i - half_k : i]
            after = features[:, i : i + half_k]

            # Compute mean feature vectors
            mean_before = np.mean(before, axis=1)
            mean_after = np.mean(after, axis=1)

            # Cosine distance: 1 - cosine_similarity
            dot_product = np.dot(mean_before, mean_after)
            norm_before = np.linalg.norm(mean_before)
            norm_after = np.linalg.norm(mean_after)

            if norm_before > 1e-8 and norm_after > 1e-8:
                cosine_sim = dot_product / (norm_before * norm_after)
                novelty[i] = 1.0 - cosine_sim

        return novelty

    def _compute_rms_novelty(
        self,
        rms: npt.NDArray[np.float32],
        kernel_size: int,
    ) -> npt.NDArray[np.float32]:
        """Compute energy-based novelty from RMS envelope.

        Detects significant energy changes (drops/rises) that often indicate
        phrase or section boundaries.

        :param rms: RMS energy, shape (1, n_frames) or (n_frames,).
        :param kernel_size: Window size in frames.
        :return: RMS novelty function, shape (n_frames,).
        """
        rms_flat = rms.flatten() if rms.ndim == 2 else rms
        n_frames = len(rms_flat)
        novelty = np.zeros(n_frames, dtype=np.float32)

        if n_frames < kernel_size:
            return novelty

        # Smooth RMS first to reduce noise
        smooth_size = max(1, kernel_size // 4)
        rms_smooth = np.convolve(rms_flat, np.ones(smooth_size) / smooth_size, mode="same")

        half_k = kernel_size // 2

        for i in range(half_k, n_frames - half_k):
            before = np.mean(rms_smooth[i - half_k : i])
            after = np.mean(rms_smooth[i : i + half_k])

            # Relative change in energy
            if before > 1e-8:
                novelty[i] = abs(after - before) / before
            elif after > 1e-8:
                novelty[i] = 1.0  # Silence to sound is maximum novelty

        return novelty

    def _fuse_novelty_signals(
        self,
        feature_novelty: npt.NDArray[np.float32],
        rms_novelty: npt.NDArray[np.float32],
        feature_weight: float = 0.7,
        rms_weight: float = 0.3,
    ) -> npt.NDArray[np.float32]:
        """Fuse multiple novelty signals with weighted combination.

        :param feature_novelty: Multi-feature novelty function.
        :param rms_novelty: RMS-based novelty function.
        :param feature_weight: Weight for feature novelty (default 0.7).
        :param rms_weight: Weight for RMS novelty (default 0.3).
        :return: Fused novelty function.
        """
        # Ensure same length
        min_len = min(len(feature_novelty), len(rms_novelty))
        if min_len == 0:
            return np.zeros(0, dtype=np.float32)

        feat = feature_novelty[:min_len]
        rms = rms_novelty[:min_len]

        # Normalize each to [0, 1]
        feat_max = np.max(feat)
        rms_max = np.max(rms)

        feat_norm = feat / feat_max if feat_max > 1e-8 else feat
        rms_norm = rms / rms_max if rms_max > 1e-8 else rms

        # Weighted combination
        fused = feature_weight * feat_norm + rms_weight * rms_norm

        return fused.astype(np.float32)

    def _find_peaks_at_bar_boundaries(
        self,
        novelty: npt.NDArray[np.float32],
        beats: npt.NDArray[np.float64],
        beats_per_bar: int,
        sample_rate: int,
        hop_length: int,
        min_prominence: float = 0.1,
        bars_between_phrases: int = 4,
    ) -> list[tuple[float, float, str]]:
        """Find novelty peaks constrained to bar boundaries.

        :param novelty: Novelty function.
        :param beats: Beat times in seconds.
        :param beats_per_bar: Beats per bar from time signature.
        :param sample_rate: Audio sample rate in Hz.
        :param hop_length: Hop length used during feature extraction.
        :param min_prominence: Minimum peak prominence to consider.
        :param bars_between_phrases: Minimum bars between phrase boundaries.
        :return: List of (time, confidence, boundary_type) tuples.
        """
        from scipy.signal import find_peaks

        if len(novelty) < 10 or len(beats) < beats_per_bar * 2:
            return []

        # Find all peaks in novelty function
        peaks, properties = find_peaks(novelty, prominence=min_prominence, distance=10)

        if len(peaks) == 0:
            return []

        prominences = properties["prominences"]

        # Convert beats to downbeat frames (first beat of each bar)
        downbeat_times = beats[::beats_per_bar]
        downbeat_frames = librosa.time_to_frames(
            downbeat_times, sr=sample_rate, hop_length=hop_length
        )

        # Calculate minimum frames between phrase boundaries
        if len(beats) > beats_per_bar:
            avg_bar_duration = (
                (beats[beats_per_bar] - beats[0]) if len(beats) > beats_per_bar else 2.0
            )
        else:
            avg_bar_duration = 2.0
        min_phrase_frames = int(bars_between_phrases * avg_bar_duration * sample_rate / hop_length)

        boundaries: list[tuple[float, float, str]] = []
        last_boundary_frame = -min_phrase_frames  # Allow first boundary

        # For each peak, find nearest downbeat
        for peak_frame, prominence in zip(peaks, prominences):
            # Find nearest downbeat frame
            distances = np.abs(downbeat_frames - peak_frame)
            nearest_idx = np.argmin(distances)
            nearest_downbeat_frame = downbeat_frames[nearest_idx]

            # Only accept if peak is close to a downbeat (within 1 bar)
            frames_per_bar = int(avg_bar_duration * sample_rate / hop_length)
            if distances[nearest_idx] > frames_per_bar:
                continue

            # Enforce minimum distance between boundaries
            if nearest_downbeat_frame - last_boundary_frame < min_phrase_frames:
                continue

            # Convert frame to time
            boundary_time = float(downbeat_times[nearest_idx])

            # Calculate confidence from prominence
            confidence = min(1.0, float(prominence) / 0.3)

            # Classify as phrase or section based on prominence
            boundary_type = "section" if prominence > 0.4 else "phrase"

            boundaries.append((boundary_time, confidence, boundary_type))
            last_boundary_frame = nearest_downbeat_frame

        return boundaries
