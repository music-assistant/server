"""Extended Analysis Processor - Final analysis on accumulated streaming features."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import warnings
from typing import TYPE_CHECKING

import librosa
import numpy as np
import numpy.typing as npt
from scipy.signal import find_peaks

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

# Shaath key profiles - Krumhansl-based, tuned for pop/electronic music
# Source: Essentia library (https://essentia.upf.edu/reference/std_Key.html)
MAJOR_PROFILE = np.array(
    [6.6, 2.0, 3.5, 2.3, 4.6, 4.0, 2.5, 5.2, 2.4, 3.7, 2.3, 3.4],
    dtype=np.float64,
)
MINOR_PROFILE = np.array(
    [6.5, 2.7, 3.5, 5.4, 2.6, 3.5, 2.5, 5.2, 4.0, 2.7, 4.3, 3.2],
    dtype=np.float64,
)

# Pitch class names for key root identification
PITCH_CLASSES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

# Number of initial beats to skip when estimating downbeat phase.
# Based on OBTAIN paper's 5-second transient period where tempo estimation
# Don't skip intro beats - the first drop is often the most reliable phase anchor.
# Energy transitions handle intros by detecting where the main pattern kicks in.
SKIP_INITIAL_BEATS = 0


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
            low_freq_rms = accumulator.get_low_freq_rms()
            mid_freq_rms = accumulator.get_mid_freq_rms()

            # Yield to event loop
            await asyncio.sleep(0)

            # Beat tracking (heavy - ~0.75s)
            bpm, beats, downbeats, confidence = await asyncio.to_thread(
                self._beat_tracking,
                onset_envelope,
                low_freq_rms,
                mid_freq_rms,
                chroma,
                spectral_centroid,
                sample_rate,
                hop_length,
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

            # Key estimation (fast) - use bass-weighted chroma for better accuracy
            downbeat_frames = librosa.time_to_frames(
                downbeats, sr=sample_rate, hop_length=hop_length
            )
            musical_key = await asyncio.to_thread(
                self._estimate_key, chroma, low_freq_rms, downbeat_frames
            )

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

            # Log detailed analysis data for debugging/verification against ground truth
            self._log_analysis_debug_data(
                beats=beats,
                downbeats=downbeats,
                bpm=bpm,
                onset_envelope=onset_envelope,
                chroma=chroma,
                low_freq_rms=low_freq_rms,
                sample_rate=sample_rate,
                hop_length=hop_length,
                time_signature=time_signature,
                phrase_boundaries=phrase_boundaries,
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
        low_freq_rms: npt.NDArray[np.float32],
        mid_freq_rms: npt.NDArray[np.float32],
        chroma: npt.NDArray[np.float32],
        spectral_centroid: npt.NDArray[np.float64],
        sample_rate: int,
        hop_length: int,
    ) -> tuple[float | None, npt.NDArray[np.float64] | None, npt.NDArray[np.float64] | None, float]:
        """Perform beat tracking using pre-computed onset envelope.

        :param onset_envelope: Pre-computed onset strength envelope.
        :param low_freq_rms: Low-frequency RMS for kick detection, shape (1, n_frames).
        :param mid_freq_rms: Mid-frequency RMS for snare detection, shape (1, n_frames).
        :param chroma: Chroma features for harmonic analysis, shape (12, n_frames).
        :param spectral_centroid: Spectral centroid for timbre analysis, shape (1, n_frames).
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

            # Standard octave error: BPM > 155 is likely double-time
            if bpm > 155:
                original_bpm = bpm
                bpm = bpm / 2
                beat_frames = beat_frames[::2]
                beat_times = librosa.frames_to_time(
                    beat_frames, sr=sample_rate, hop_length=hop_length
                )
                self.logger.debug(
                    "Applied BPM halving: %.1f -> %.1f BPM (%d beats)",
                    original_bpm,
                    bpm,
                    len(beat_times),
                )

            # Calculate confidence based on beat interval consistency
            intervals = np.diff(beat_times)
            interval_std = np.std(intervals)
            interval_mean = np.mean(intervals)
            cv = interval_std / interval_mean if interval_mean > 0 else 1.0
            confidence = max(0.1, 1.0 - cv)

            # Estimate downbeats using multi-feature scoring
            downbeats = self._estimate_downbeats(
                beat_times,
                onset_envelope,
                low_freq_rms,
                mid_freq_rms,
                chroma,
                spectral_centroid,
                sample_rate,
                hop_length,
            )

            return bpm, beat_times, downbeats, float(confidence)

        except Exception as e:
            self.logger.warning("Beat tracking failed: %s", e)
            return None, None, None, 0.0

    def _estimate_downbeats(
        self,
        beats: npt.NDArray[np.float64],
        onset_envelope: npt.NDArray[np.float32],
        low_freq_rms: npt.NDArray[np.float32],
        mid_freq_rms: npt.NDArray[np.float32],
        chroma: npt.NDArray[np.float32],
        spectral_centroid: npt.NDArray[np.float64],
        sample_rate: int,
        hop_length: int,
    ) -> npt.NDArray[np.float64]:
        """Estimate downbeats using beat-synchronous pattern matching.

        Uses phase-aware features to distinguish beat positions:
        1. Kick-snare pattern: Kick (low freq) on beats 1,3, snare (mid freq) on 2,4
        2. Harmonic novelty: Chord changes happen on beat 1, not beat 3
        3. Bass energy: Kick drums (present on beats 1 and 3)
        4. Phrase alignment: Beat drops anchor to beat 1

        Key insight: Kick-snare pattern distinguishes (1,3) from (2,4),
        harmonic novelty and phrase alignment distinguish 1 from 3.

        :param beats: Array of beat times in seconds.
        :param onset_envelope: Onset strength envelope.
        :param low_freq_rms: Low-frequency RMS energy, shape (1, n_frames).
        :param mid_freq_rms: Mid-frequency RMS energy, shape (1, n_frames).
        :param chroma: Chroma features, shape (12, n_frames).
        :param spectral_centroid: Spectral centroid, shape (1, n_frames).
        :param sample_rate: Audio sample rate in Hz.
        :param hop_length: Hop length used during feature extraction.
        :return: Array of estimated downbeat times.
        """
        if len(beats) < 8:
            return beats[:1] if len(beats) > 0 else np.array([])

        # Flatten arrays if 2D
        low_freq_flat = low_freq_rms.flatten() if low_freq_rms.ndim == 2 else low_freq_rms
        mid_freq_flat = mid_freq_rms.flatten() if mid_freq_rms.ndim == 2 else mid_freq_rms
        centroid_flat = (
            spectral_centroid.flatten() if spectral_centroid.ndim == 2 else spectral_centroid
        )

        if len(low_freq_flat) == 0 or len(onset_envelope) == 0:
            self.logger.warning("Missing feature data, falling back to simple downbeats")
            return beats[::4]

        # Convert beat times to frame indices
        beat_frames = librosa.time_to_frames(beats, sr=sample_rate, hop_length=hop_length)

        # Find valid frame range across all features
        max_frame = min(
            len(onset_envelope),
            len(low_freq_flat),
            len(centroid_flat),
            chroma.shape[1] if chroma.ndim == 2 else len(chroma),
        )
        beat_frames = beat_frames[beat_frames < max_frame]

        if len(beat_frames) < 8:
            return beats[::4]

        # Skip initial beats for phase estimation (intro often has atypical patterns)
        # Based on OBTAIN paper's 5-second transient period where tempo estimation
        # needs time to stabilize. We keep full beat_frames for final output,
        # but use only stable region for phase scoring.
        skip_beats = min(SKIP_INITIAL_BEATS, len(beat_frames) // 2)  # Don't skip more than half
        stable_frames = beat_frames[skip_beats:]

        if len(stable_frames) < 8:
            # Not enough beats after skipping, use all beats
            stable_frames = beat_frames
            skip_beats = 0

        # Extract features at stable beat positions
        beat_bass = low_freq_flat[stable_frames]

        # Compute harmonic novelty: chroma distance between consecutive beats
        # This is the KEY phase-aware feature - chord changes happen on beat 1
        harmonic_novelty = self._compute_harmonic_novelty_at_beats(chroma, stable_frames)

        # Compute chroma flux (frame-level) for comparison
        beat_chroma_flux = self._compute_chroma_flux_at_beats(chroma, stable_frames)

        # Detect major energy transitions (beat drops) for phrase anchoring
        energy_transitions = self._detect_energy_transitions(
            low_freq_rms, beats, sample_rate, hop_length
        )

        # Use beat-synchronous pattern matching for phase detection
        best_offset, all_scores = self._score_bar_patterns(
            beat_bass=beat_bass,
            harmonic_novelty=harmonic_novelty,
            beat_chroma_flux=beat_chroma_flux,
            beats=beats,
            energy_transitions=energy_transitions,
            low_freq_flat=low_freq_flat,
            mid_freq_flat=mid_freq_flat,
            stable_frames=stable_frames,
        )

        # Log detailed scoring
        self.logger.debug(
            "Downbeat phase scoring (beat-synchronous): skipped=%d beats, "
            "using %d stable beats, energy_transitions=%d, scores: %s -> best_offset=%d",
            skip_beats,
            len(stable_frames),
            len(energy_transitions),
            [
                f"off={int(s['offset'])}: harm={s['harmonic']:.2f}, "
                f"bass={s['bass']:.2f}, patt={s['pattern']:.2f}, "
                f"phr={s.get('phrase', 0):.2f}, comb={s['combined']:.2f}"
                for s in all_scores
            ],
            best_offset,
        )

        # Check for early energy transition - if there's a clear drop in the first 10 seconds,
        # use it as a hard anchor since the first drop almost always falls on beat 1
        early_anchor_offset = None
        if energy_transitions:
            first_trans = energy_transitions[0]
            if first_trans < 10.0:  # First transition within 10 seconds
                # Find which beat index this transition is at
                beat_dists = np.abs(beats - first_trans)
                nearest_beat_idx = int(np.argmin(beat_dists))
                early_anchor_offset = nearest_beat_idx % 4
                self.logger.debug(
                    "Early energy transition at %.2fs (beat %d) -> anchor offset=%d",
                    first_trans,
                    nearest_beat_idx,
                    early_anchor_offset,
                )

        # Use early anchor if available, otherwise use scored offset
        if early_anchor_offset is not None:
            corrected_offset = early_anchor_offset
            self.logger.debug(
                "Using early anchor offset=%d (overriding scored offset=%d)",
                corrected_offset,
                best_offset,
            )
        else:
            corrected_offset = best_offset

        # Debug: log energy transition alignment details
        if energy_transitions:
            trans_debug = []
            for trans in energy_transitions[:5]:  # First 5 transitions
                # Find which beat index this transition corresponds to
                beat_dists = np.abs(beats - trans)
                nearest_beat_idx = int(np.argmin(beat_dists))
                phase_in_bar = nearest_beat_idx % 4
                trans_debug.append(f"{trans:.2f}s->beat{nearest_beat_idx}(ph{phase_in_bar})")
            self.logger.debug(
                "Energy transitions (first 5 of %d): %s", len(energy_transitions), trans_debug
            )

        return beats[corrected_offset::4]

    def _detect_energy_transitions(
        self,
        low_freq_rms: npt.NDArray[np.float32],
        beats: npt.NDArray[np.float64],
        sample_rate: int,
        hop_length: int,
    ) -> list[float]:
        """Detect major energy transitions (beat drops) for phrase anchoring.

        Beat drops and phrase boundaries are reliably on beat 1 (downbeat).
        By detecting these, we can anchor our phase selection.

        :param low_freq_rms: Low-frequency RMS energy, shape (1, n_frames).
        :param beats: Beat times in seconds.
        :param sample_rate: Audio sample rate.
        :param hop_length: Hop length used during feature extraction.
        :return: List of transition times (in seconds).
        """
        rms_flat = low_freq_rms.flatten() if low_freq_rms.ndim == 2 else low_freq_rms
        if len(rms_flat) < 100 or len(beats) < 16:
            return []

        frames_per_second = sample_rate / hop_length
        transitions: list[float] = []

        # Check each beat for energy change (compare 2 bars before vs 2 bars after)
        # Use a 4-beat window (roughly 2 seconds at 120 BPM)
        window_beats = 4
        window_frames = int(window_beats * (60.0 / 120.0) * frames_per_second)  # Approximate

        # Debounce: after detecting a transition, skip at least 1 bar (4 beats) to avoid
        # detecting consecutive beats during energy ramp-up. We want the FIRST beat of a
        # drop, not every beat after the drop.
        min_gap_seconds = 1.5  # Minimum gap between transitions (~1 bar at 120 BPM)
        last_transition_time = -min_gap_seconds

        for beat_time in beats:
            # Skip if too close to last transition (debounce)
            if beat_time - last_transition_time < min_gap_seconds:
                continue

            frame_idx = int(beat_time * frames_per_second)

            if frame_idx < window_frames or frame_idx >= len(rms_flat) - window_frames:
                continue

            rms_before = float(np.mean(rms_flat[frame_idx - window_frames : frame_idx]))
            rms_after = float(np.mean(rms_flat[frame_idx : frame_idx + window_frames]))

            # Look for significant upward transitions (beat drops)
            if rms_before > 1e-8:
                change_ratio = (rms_after - rms_before) / rms_before
                # Only major transitions (>50% energy increase)
                if change_ratio > 0.5:
                    transitions.append(beat_time)
                    last_transition_time = beat_time

        return transitions

    def _compute_harmonic_novelty_at_beats(
        self, chroma: npt.NDArray[np.float32], beat_frames: npt.NDArray[np.int64]
    ) -> npt.NDArray[np.float32]:
        """Compute harmonic novelty (chroma distance) between consecutive beats.

        This is a key phase-aware feature: chord changes cluster on beat 1,
        not beat 3. Beat 3 typically has the same chord as beat 1.

        :param chroma: Chroma features, shape (12, n_frames).
        :param beat_frames: Frame indices of beats.
        :return: Harmonic novelty at each beat position.
        """
        novelty = np.zeros(len(beat_frames), dtype=np.float32)

        for i in range(1, len(beat_frames)):
            curr_frame = beat_frames[i]
            prev_frame = beat_frames[i - 1]

            if curr_frame < chroma.shape[1] and prev_frame < chroma.shape[1]:
                # L2 distance between chroma vectors
                diff = chroma[:, curr_frame] - chroma[:, prev_frame]
                novelty[i] = float(np.linalg.norm(diff))

        return novelty

    def _score_bar_patterns(
        self,
        beat_bass: npt.NDArray[np.float32],
        harmonic_novelty: npt.NDArray[np.float32],
        beat_chroma_flux: npt.NDArray[np.float32],
        beats: npt.NDArray[np.float64],
        energy_transitions: list[float],
        low_freq_flat: npt.NDArray[np.float32],
        mid_freq_flat: npt.NDArray[np.float32],
        stable_frames: npt.NDArray[np.int64],
    ) -> tuple[int, list[dict[str, float | int]]]:
        """Score each phase offset using beat-synchronous pattern matching.

        Uses kick-snare pattern as primary feature: kick (low freq) on beats 1,3,
        snare (mid freq) on beats 2,4. Harmonic novelty distinguishes 1 from 3.

        :param beat_bass: Bass energy at each beat.
        :param harmonic_novelty: Harmonic novelty at each beat.
        :param beat_chroma_flux: Chroma flux at each beat.
        :param beats: All beat times in seconds.
        :param energy_transitions: Times of major energy transitions (beat drops).
        :param low_freq_flat: Flattened low-frequency RMS array.
        :param mid_freq_flat: Flattened mid-frequency RMS array.
        :param stable_frames: Frame indices of stable beats (after intro skip).
        :return: Tuple of (best_offset, all_scores).
        """
        all_scores: list[dict[str, float | int]] = []

        for offset in range(4):
            # For this offset, calculate scores using bar-level patterns

            # 1. Harmonic novelty score: beat 1 should have MORE harmonic change than beat 3
            harmonic_score = self._compute_beat1_vs_beat3_ratio(harmonic_novelty, offset)

            # 2. Bass score: ratio of downbeat bass to other beats
            bass_score = self._compute_feature_ratio(beat_bass, offset)

            # 3. Pattern score: analyze the 4-beat pattern shape
            # Expected: beat 1 high novelty, beat 2 low, beat 3 moderate, beat 4 low
            pattern_score = self._score_pattern_shape(harmonic_novelty, beat_bass, offset)

            # 4. Phrase alignment: how well do candidate downbeats align with energy transitions?
            # This is a strong anchor - beat drops should be on beat 1
            phrase_score = self._score_phrase_alignment(beats, energy_transitions, offset)

            # Weighted combination - balanced approach
            # Phrase alignment is most reliable when transitions exist
            if energy_transitions:
                combined = (
                    0.35 * phrase_score  # Phrase boundary alignment (primary anchor)
                    + 0.25 * pattern_score  # Bar-level pattern shape
                    + 0.20 * harmonic_score  # Chord changes (supporting)
                    + 0.20 * bass_score  # Bass energy
                )
            else:
                # Without phrase markers, rely more evenly on pattern and harmonic
                combined = (
                    0.30 * pattern_score  # Bar-level pattern shape
                    + 0.30 * harmonic_score  # Chord changes
                    + 0.25 * bass_score  # Bass energy
                    + 0.15 * phrase_score  # Phrase alignment (less reliable here)
                )

            all_scores.append(
                {
                    "offset": offset,
                    "harmonic": harmonic_score,
                    "bass": bass_score,
                    "pattern": pattern_score,
                    "phrase": phrase_score,
                    "combined": combined,
                }
            )

        # Sort by combined score
        all_scores.sort(key=lambda x: x["combined"], reverse=True)

        best = all_scores[0]
        second_best = all_scores[1] if len(all_scores) > 1 else all_scores[0]

        # Calculate confidence margin
        margin = (best["combined"] - second_best["combined"]) / (best["combined"] + 1e-8)

        # If margin is very low, use multi-bar confirmation
        # Note: Set to 0.05 (5%) - stricter threshold since combined score is usually reliable
        if margin < 0.05 and len(all_scores) >= 2:
            best_offset = self._multi_bar_vote(harmonic_novelty, beat_bass, all_scores)
            self.logger.debug(
                "Low confidence (margin=%.1f%%), using multi-bar vote -> offset=%d",
                margin * 100,
                best_offset,
            )
        else:
            best_offset = int(best["offset"])

        return best_offset, all_scores

    def _compute_beat1_vs_beat3_ratio(self, feature: npt.NDArray[np.float32], offset: int) -> float:
        """Compute ratio of feature at beat 1 vs beat 3 positions.

        This is the key metric for distinguishing downbeats from beat 3.
        Chord changes (harmonic novelty) should be higher on beat 1.

        :param feature: Feature values at each beat position.
        :param offset: Phase offset (0-3) to test as downbeat.
        :return: Ratio score (higher = beat 1 has more of this feature than beat 3).
        """
        n_beats = len(feature)
        if n_beats < 8:
            return 1.0

        # Beat 1 positions (candidate downbeats)
        beat1_indices = list(range(offset, n_beats, 4))
        # Beat 3 positions (2 beats after each beat 1)
        beat3_offset = (offset + 2) % 4
        beat3_indices = list(range(beat3_offset, n_beats, 4))

        if len(beat1_indices) < 2 or len(beat3_indices) < 2:
            return 1.0

        # Use median for robustness
        beat1_median = float(np.median(feature[beat1_indices]))
        beat3_median = float(np.median(feature[beat3_indices]))

        # Ratio: higher means beat 1 has more of this feature
        return beat1_median / (beat3_median + 1e-8)

    def _score_pattern_shape(
        self,
        harmonic_novelty: npt.NDArray[np.float32],
        beat_bass: npt.NDArray[np.float32],
        offset: int,
    ) -> float:
        """Score how well the 4-beat pattern matches expected downbeat pattern.

        Expected pattern for 4/4 with offset being beat 1:
        - Beat 1: High harmonic novelty (chord change), high bass (kick)
        - Beat 2: Low novelty, moderate bass (snare)
        - Beat 3: Low novelty (same chord), high bass (kick)
        - Beat 4: Low novelty, moderate bass (snare)

        :param harmonic_novelty: Harmonic novelty at each beat.
        :param beat_bass: Bass energy at each beat.
        :param offset: Phase offset to test.
        :return: Pattern match score (higher = better match).
        """
        n_beats = len(harmonic_novelty)
        n_complete_bars = (n_beats - offset) // 4

        if n_complete_bars < 2:
            return 1.0

        # Aggregate features by beat position within bar
        pos_harmonic: dict[int, list[float]] = {0: [], 1: [], 2: [], 3: []}
        pos_bass: dict[int, list[float]] = {0: [], 1: [], 2: [], 3: []}

        for bar_idx in range(n_complete_bars):
            for beat_in_bar in range(4):
                global_idx = offset + bar_idx * 4 + beat_in_bar
                if global_idx < n_beats:
                    pos_harmonic[beat_in_bar].append(harmonic_novelty[global_idx])
                    pos_bass[beat_in_bar].append(beat_bass[global_idx])

        # Calculate mean for each position
        mean_harmonic = {pos: np.mean(vals) if vals else 0.0 for pos, vals in pos_harmonic.items()}
        mean_bass = {pos: np.mean(vals) if vals else 0.0 for pos, vals in pos_bass.items()}

        # Score: beat 0 (our candidate beat 1) should have highest harmonic novelty
        # And beat 0 + beat 2 should have higher bass than beat 1 + beat 3 (kick pattern)

        # Harmonic pattern score: beat 0 > beat 2
        harmonic_pattern = mean_harmonic[0] / (mean_harmonic[2] + 1e-8)

        # Bass pattern score: (beat 0 + beat 2) > (beat 1 + beat 3) - kick on 1 and 3
        kick_bass = (mean_bass[0] + mean_bass[2]) / 2
        snare_bass = (mean_bass[1] + mean_bass[3]) / 2
        bass_pattern = kick_bass / (snare_bass + 1e-8)

        # Combined pattern score
        return float(0.7 * harmonic_pattern + 0.3 * bass_pattern)

    def _score_phrase_alignment(
        self,
        beats: npt.NDArray[np.float64],
        energy_transitions: list[float],
        offset: int,
        tolerance: float = 0.1,
    ) -> float:
        """Score how well candidate downbeats align with energy transitions.

        Energy transitions (beat drops) should align with beat 1 (downbeat).
        Early transitions (first drop/intro) are weighted more heavily as they're
        the most reliable anchor for phase detection.

        :param beats: All beat times in seconds.
        :param energy_transitions: Times of major energy transitions.
        :param offset: Phase offset (0-3) to test as downbeat.
        :param tolerance: Time tolerance in seconds for matching.
        :return: Alignment score (higher = better alignment).
        """
        if not energy_transitions or len(beats) < 8:
            return 1.0  # Neutral score when no transitions

        # Get candidate downbeats for this offset
        candidate_downbeats = beats[offset::4]

        if len(candidate_downbeats) == 0:
            return 1.0

        # Weight transitions by position - early transitions are more reliable anchors
        # The first drop/intro almost always falls on beat 1 (downbeat)
        total_weight = 0.0
        weighted_alignments = 0.0

        for trans_time in energy_transitions:
            # Early transitions get higher weight (exponential decay)
            # First 10 seconds: weight ~3-5x, after 30 seconds: weight ~1x
            position_weight = 1.0 + 4.0 * np.exp(-trans_time / 10.0)

            # Find closest candidate downbeat
            distances = np.abs(candidate_downbeats - trans_time)
            min_dist = float(np.min(distances))
            if min_dist < tolerance:
                weighted_alignments += position_weight

            total_weight += position_weight

        # Score: weighted proportion of transitions that align
        if total_weight > 0:
            alignment_rate = weighted_alignments / total_weight
            # Scale so that good alignment gives scores > 1.0
            return 0.5 + alignment_rate  # Range: 0.5 to 1.5
        return 1.0

    def _multi_bar_vote(
        self,
        harmonic_novelty: npt.NDArray[np.float32],
        beat_bass: npt.NDArray[np.float32],
        all_scores: list[dict[str, float | int]],
    ) -> int:
        """Use multi-bar voting when phase scores are too close.

        Score each bar independently, then use majority vote.
        This reduces the impact of anomalous bars (fills, breaks).

        :param harmonic_novelty: Harmonic novelty at each beat.
        :param beat_bass: Bass energy at each beat.
        :param all_scores: Pre-computed offset scores.
        :return: Best offset according to multi-bar vote.
        """
        n_beats = len(harmonic_novelty)
        n_complete_bars = n_beats // 4

        if n_complete_bars < 4:
            # Not enough bars for voting, use combined score
            return int(all_scores[0]["offset"])

        votes: dict[int, int] = {0: 0, 1: 0, 2: 0, 3: 0}

        # Score each bar independently
        for bar_start in range(0, n_complete_bars * 4 - 3, 4):
            bar_harmonic = harmonic_novelty[bar_start : bar_start + 4]
            bar_bass = beat_bass[bar_start : bar_start + 4]

            # For this bar, which position has highest harmonic novelty?
            # That's likely beat 1
            if len(bar_harmonic) == 4:
                # Weight by both harmonic novelty and bass energy
                combined_score = bar_harmonic + 0.3 * bar_bass
                best_pos = int(np.argmax(combined_score))
                # This position should be beat 1, so offset = (4 - best_pos) % 4
                # Actually, if best_pos is the local winner, then offset that makes
                # best_pos align with position 0 is: offset = best_pos (mod 4 arithmetic)
                winning_offset = best_pos % 4
                votes[winning_offset] += 1

        # Find offset with most votes
        max_votes = max(votes.values())
        top_offsets = [off for off, v in votes.items() if v == max_votes]

        self.logger.debug(
            "Multi-bar vote: votes=%s, max_votes=%d, top_offsets=%s",
            dict(votes),
            max_votes,
            top_offsets,
        )

        if len(top_offsets) == 1:
            return top_offsets[0]

        # Tiebreak: use combined score from pre-computed scores
        for score_dict in all_scores:
            if int(score_dict["offset"]) in top_offsets:
                return int(score_dict["offset"])

        return int(all_scores[0]["offset"])

    def _compute_feature_ratio(self, beat_feature: npt.NDArray[np.float32], offset: int) -> float:
        """Compute ratio of feature at downbeat positions vs other beats.

        :param beat_feature: Feature values at each beat position.
        :param offset: Phase offset (0-3) to test as downbeat.
        :return: Ratio score (higher = stronger at this offset).
        """
        downbeat_indices = list(range(offset, len(beat_feature), 4))
        other_indices = [i for i in range(len(beat_feature)) if i % 4 != offset]

        if len(downbeat_indices) < 2 or not other_indices:
            return 1.0

        # Use median for robust estimation (resistant to outliers)
        db_median = float(np.median(beat_feature[downbeat_indices]))
        other_median = float(np.median(beat_feature[other_indices]))

        return db_median / (other_median + 1e-8)

    def _compute_chroma_flux_at_beats(
        self, chroma: npt.NDArray[np.float32], beat_frames: npt.NDArray[np.int64]
    ) -> npt.NDArray[np.float32]:
        """Compute chroma flux (harmonic change) at each beat position.

        Chroma flux is the L2 norm of the difference between consecutive
        chroma vectors. Higher flux indicates harmonic changes, which often
        occur at downbeats (chord changes at bar boundaries).

        :param chroma: Chroma features, shape (12, n_frames).
        :param beat_frames: Frame indices of beats.
        :return: Chroma flux at each beat position.
        """
        chroma_flux = np.zeros(len(beat_frames), dtype=np.float32)

        for i, frame in enumerate(beat_frames):
            if frame > 0 and frame < chroma.shape[1]:
                # Compare current frame to a few frames before the beat
                # to capture the harmonic change leading into the beat
                lookback = min(3, frame)
                diff = chroma[:, frame] - chroma[:, frame - lookback]
                chroma_flux[i] = float(np.linalg.norm(diff))

        return chroma_flux

    def _compute_spectral_contrast_score(
        self,
        beat_bass: npt.NDArray[np.float32],
        beat_centroid: npt.NDArray[np.float64],
        offset: int,
    ) -> float:
        """Compute spectral contrast score to distinguish kick vs snare.

        Kick drums are "dark" (high bass, low centroid) while snares are
        "bright" (low bass, high centroid). This ratio helps distinguish
        beat 1 (kick) from beat 2/4 (snare).

        :param beat_bass: Bass energy at each beat.
        :param beat_centroid: Spectral centroid at each beat.
        :param offset: Phase offset (0-3) to test as downbeat.
        :return: Spectral contrast score.
        """
        # Compute bass-to-brightness ratio (higher = more kick-like)
        bass_brightness_ratio = beat_bass / (beat_centroid + 1e-8)

        downbeat_indices = list(range(offset, len(bass_brightness_ratio), 4))
        # Compare against beat 2 position (where snare typically is)
        beat2_offset = (offset + 1) % 4
        beat2_indices = list(range(beat2_offset, len(bass_brightness_ratio), 4))

        if len(downbeat_indices) < 2 or len(beat2_indices) < 2:
            return 1.0

        # Use median for robust estimation (resistant to outliers)
        db_ratio_median = float(np.median(bass_brightness_ratio[downbeat_indices]))
        beat2_ratio_median = float(np.median(bass_brightness_ratio[beat2_indices]))

        # Higher score = this offset has more kick-like beats relative to beat 2
        return db_ratio_median / (beat2_ratio_median + 1e-8)

    def _consensus_vote(self, all_scores: list[dict[str, float | int]]) -> int:
        """Use consensus voting when feature scores are too close.

        Each feature votes for the offset with its highest score.
        Bass gets double weight as the most reliable feature.
        Ties are broken by combined score (our best overall estimate).

        :param all_scores: List of score dicts with offset and feature scores.
        :return: Best offset according to consensus.
        """
        votes: dict[int, int] = {0: 0, 1: 0, 2: 0, 3: 0}
        feature_weights = {"bass": 2, "onset": 1, "chroma": 1, "spectral": 1}

        for feature in ["bass", "onset", "chroma", "spectral"]:
            best_for_feature = max(all_scores, key=lambda x: x[feature])
            offset = int(best_for_feature["offset"])
            votes[offset] += feature_weights[feature]

        # Find offset with most votes
        max_votes = max(votes.values())
        top_offsets = [off for off, v in votes.items() if v == max_votes]

        if len(top_offsets) == 1:
            return top_offsets[0]

        # Tiebreak: use combined score (our best overall estimate)
        # Filter to only tied offsets, then pick highest combined
        tied_scores = [s for s in all_scores if int(s["offset"]) in top_offsets]
        best_combined = max(tied_scores, key=lambda x: x["combined"])
        return int(best_combined["offset"])

    def _log_analysis_debug_data(
        self,
        beats: npt.NDArray[np.float64],
        downbeats: npt.NDArray[np.float64],
        bpm: float,
        onset_envelope: npt.NDArray[np.float32],
        chroma: npt.NDArray[np.float32],
        low_freq_rms: npt.NDArray[np.float32],
        sample_rate: int,
        hop_length: int,
        time_signature: TimeSignature,
        phrase_boundaries: list[PhraseBoundary],
    ) -> None:
        """Log detailed analysis data for debugging and ground truth verification.

        Outputs JSON-formatted data that can be compared against datasets like Harmonix.

        :param beats: Detected beat times in seconds.
        :param downbeats: Detected downbeat times in seconds.
        :param bpm: Detected tempo in BPM.
        :param onset_envelope: Onset strength envelope.
        :param chroma: Chroma features, shape (12, n_frames).
        :param low_freq_rms: Low-frequency RMS energy, shape (1, n_frames).
        :param sample_rate: Audio sample rate in Hz.
        :param hop_length: Hop length used during feature extraction.
        :param time_signature: Detected time signature.
        :param phrase_boundaries: Detected phrase boundaries.
        """
        # Get onset strength at each beat position
        beat_frames = librosa.time_to_frames(beats, sr=sample_rate, hop_length=hop_length)
        beat_frames = beat_frames[beat_frames < len(onset_envelope)]
        beat_onset_strengths = onset_envelope[beat_frames].tolist()

        # Get low-frequency energy at each beat position
        low_freq_flat = low_freq_rms.flatten() if low_freq_rms.ndim == 2 else low_freq_rms
        valid_lf_frames = beat_frames[beat_frames < len(low_freq_flat)]
        beat_bass_energy = (
            low_freq_flat[valid_lf_frames].tolist() if len(valid_lf_frames) > 0 else []
        )

        # Compute chroma flux at each beat position
        chroma_flux: list[float] = []
        valid_chroma_frames = beat_frames[beat_frames < chroma.shape[1]]
        for frame in valid_chroma_frames:
            if frame > 0:
                diff = chroma[:, frame] - chroma[:, frame - 1]
                chroma_flux.append(float(np.linalg.norm(diff)))
            else:
                chroma_flux.append(0.0)

        # Determine which beat index corresponds to each downbeat
        downbeat_indices = []
        for db_time in downbeats:
            closest_beat_idx = int(np.argmin(np.abs(beats - db_time)))
            downbeat_indices.append(closest_beat_idx)

        # Calculate the detected phase offset
        detected_offset = downbeat_indices[0] % 4 if downbeat_indices else 0

        # Get chroma values at each beat position (12 pitch classes per beat)
        beat_chroma: list[list[float]] = []
        for frame in valid_chroma_frames:
            beat_chroma.append([round(float(v), 4) for v in chroma[:, frame]])

        debug_data = {
            "bpm": round(bpm, 2),
            "time_signature": f"{time_signature.beats_per_bar}/4",
            "time_signature_confidence": round(time_signature.confidence, 3),
            "detected_downbeat_offset": detected_offset,
            "num_beats": len(beats),
            "num_downbeats": len(downbeats),
            "beats": [round(b, 6) for b in beats.tolist()],
            "downbeats": [round(d, 6) for d in downbeats.tolist()],
            "beat_onset_strengths": [round(s, 4) for s in beat_onset_strengths],
            "beat_bass_energy": [round(s, 6) for s in beat_bass_energy],
            "beat_chroma_flux": [round(c, 4) for c in chroma_flux],
            "beat_chroma": beat_chroma,  # Shape: (n_beats, 12) - pitch classes C to B
            "phrase_boundaries": [
                {
                    "time": round(pb.time, 3),
                    "confidence": round(pb.confidence, 2),
                    "type": pb.boundary_type,
                }
                for pb in phrase_boundaries
            ],
        }

        # Log as JSON for easy parsing
        self.logger.info(
            "ANALYSIS_DEBUG_DATA: %s",
            json.dumps(debug_data, separators=(",", ":")),
        )

        # Also log a human-readable summary
        self.logger.info(
            "Analysis summary: BPM=%.1f, beats=%d, downbeats=%d (offset=%d), "
            "time_sig=%s, phrases=%d",
            bpm,
            len(beats),
            len(downbeats),
            detected_offset,
            f"{time_signature.beats_per_bar}/4",
            len(phrase_boundaries),
        )

        # Log bass energy by beat position for quick inspection
        if len(beat_bass_energy) >= 8:
            bass_by_phase: dict[int, list[float]] = {0: [], 1: [], 2: [], 3: []}
            for i in range(min(32, len(beat_bass_energy))):
                bass_by_phase[i % 4].append(beat_bass_energy[i])

            self.logger.info(
                "Bass energy by beat position (first 8 bars): "
                "beat1=%.6f, beat2=%.6f, beat3=%.6f, beat4=%.6f",
                float(np.mean(bass_by_phase[detected_offset])),
                float(np.mean(bass_by_phase[(detected_offset + 1) % 4])),
                float(np.mean(bass_by_phase[(detected_offset + 2) % 4])),
                float(np.mean(bass_by_phase[(detected_offset + 3) % 4])),
            )

    def _estimate_key(
        self,
        chroma: npt.NDArray[np.float32],
        low_freq_rms: npt.NDArray[np.float32] | None = None,
        downbeat_frames: npt.NDArray[np.int64] | None = None,
    ) -> MusicalKey | None:
        """Estimate musical key using Shaath profiles with bass-weighted chroma.

        Uses Shaath key profiles (tuned for pop/electronic music) and optionally
        weights chroma frames by low-frequency energy to emphasize bass notes,
        which are strong indicators of chord roots.

        :param chroma: Chroma features, shape (12, n_frames).
        :param low_freq_rms: Low-frequency RMS energy, shape (1, n_frames).
        :param downbeat_frames: Frame indices of downbeats for fallback weighting.
        :return: Estimated key with confidence, or None if estimation fails.
        """
        if chroma.shape[1] < 10:
            return None

        try:
            # Weight chroma by bass energy if available (bass notes indicate chord roots)
            if low_freq_rms is not None and low_freq_rms.shape[1] == chroma.shape[1]:
                weights = low_freq_rms.flatten()
                weights = weights / (np.sum(weights) + 1e-8)  # Normalize to sum to 1
                chroma_avg = np.sum(chroma * weights, axis=1)
            elif downbeat_frames is not None and len(downbeat_frames) > 4:
                # Fallback: use downbeat-only chroma (tonic chord on beat 1)
                valid_frames = downbeat_frames[downbeat_frames < chroma.shape[1]]
                chroma_avg = np.mean(chroma[:, valid_frames], axis=1)
            else:
                # Simple average across time
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
        def normalize(
            arr: npt.NDArray[np.floating[npt.Any]],
        ) -> npt.NDArray[np.float32]:
            arr_f32: npt.NDArray[np.float32] = arr.astype(np.float32)
            min_val, max_val = float(arr_f32.min()), float(arr_f32.max())
            if max_val - min_val > 1e-8:
                result: npt.NDArray[np.float32] = (arr_f32 - min_val) / (max_val - min_val)
                return result
            return np.zeros_like(arr_f32)

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
        for peak_frame, prominence in zip(peaks, prominences, strict=True):
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
