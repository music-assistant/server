"""Sonic analysis helper — feature extraction and semantic audio analysis.

Extracts per-block spectral/timbral features from raw PCM audio using librosa,
then collapses accumulated blocks into a populated AudioAnalysisData with
human-readable semantic descriptors (BPM, key, mode, energy, danceability, etc.).
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field

import librosa
import numpy as np
import numpy.typing as npt

from music_assistant.models.audio_analysis import AudioAnalysisData

# Krumhansl-Kessler key-finding profiles (major and minor)
_KK_MAJOR = np.array(
    [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88],
    dtype=np.float64,
)
_KK_MINOR = np.array(
    [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17],
    dtype=np.float64,
)
_PITCH_CLASSES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

# Tempo range used to score danceability: tempos in 90-130 BPM are most danceable
_DANCE_TEMPO_MIN = 90.0
_DANCE_TEMPO_MAX = 130.0

# Fixed resolution for time-series fields (rms_energy, spectral_centroid) on
# AudioAnalysisData — matches the upstream contract shared with other analysis
# providers. Produces a consistent x-axis resolution regardless of track length.
_TIME_SERIES_BINS = 1800

# Energy threshold below which spectral centroid becomes noise-dominated; centroid
# bins with RMS below this are zeroed to keep the signal musically meaningful.
_SILENCE_THRESHOLD = 0.01


@dataclass
class BlockFeatures:
    """Per-block feature arrays accumulated across 10-second blocks.

    Each list holds per-frame (or per-second) values from one block.
    After all blocks are processed, collapse_to_analysis() aggregates
    these into a populated AudioAnalysisData.
    """

    mfcc_frames: list[np.ndarray] = field(default_factory=list)
    chroma_frames: list[np.ndarray] = field(default_factory=list)
    tonnetz_frames: list[np.ndarray] = field(default_factory=list)
    contrast_frames: list[np.ndarray] = field(default_factory=list)
    centroid_frames: list[np.ndarray] = field(default_factory=list)
    rolloff_frames: list[np.ndarray] = field(default_factory=list)
    flatness_frames: list[np.ndarray] = field(default_factory=list)
    rms_frames: list[np.ndarray] = field(default_factory=list)
    zcr_frames: list[np.ndarray] = field(default_factory=list)
    onset_env_frames: list[np.ndarray] = field(default_factory=list)


MIN_BLOCK_SAMPLES: int = 4096


def extract_block_features(audio: np.ndarray, sample_rate: int) -> BlockFeatures | None:
    """Extract per-frame features from a single audio block (~10 seconds).

    Returns None if the audio is too short for STFT processing.

    :param audio: Mono float32 audio samples for this block.
    :param sample_rate: Sample rate in Hz.
    """
    if len(audio) < MIN_BLOCK_SAMPLES:
        return None
    bf = BlockFeatures()

    # Suppress librosa's n_fft warnings from internal sub-calls (harmonic/percussive
    # separation in chroma/tonnetz can produce sub-signals shorter than n_fft)
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="n_fft=", category=UserWarning)
        bf.mfcc_frames.append(librosa.feature.mfcc(y=audio, sr=sample_rate, n_mfcc=13))
        bf.chroma_frames.append(librosa.feature.chroma_stft(y=audio, sr=sample_rate))
        bf.tonnetz_frames.append(librosa.feature.tonnetz(y=audio, sr=sample_rate))
        bf.contrast_frames.append(
            librosa.feature.spectral_contrast(y=audio, sr=sample_rate, n_bands=6)
        )
        bf.centroid_frames.append(librosa.feature.spectral_centroid(y=audio, sr=sample_rate))
        bf.rolloff_frames.append(librosa.feature.spectral_rolloff(y=audio, sr=sample_rate))
        bf.flatness_frames.append(librosa.feature.spectral_flatness(y=audio))
        bf.rms_frames.append(librosa.feature.rms(y=audio))
        bf.zcr_frames.append(librosa.feature.zero_crossing_rate(y=audio))
        bf.onset_env_frames.append(librosa.onset.onset_strength(y=audio, sr=sample_rate))

    return bf


def merge_block_features(target: BlockFeatures, source: BlockFeatures) -> None:
    """Merge source block features into target (in place).

    :param target: Accumulator to merge into.
    :param source: New block features to add.
    """
    target.mfcc_frames.extend(source.mfcc_frames)
    target.chroma_frames.extend(source.chroma_frames)
    target.tonnetz_frames.extend(source.tonnetz_frames)
    target.contrast_frames.extend(source.contrast_frames)
    target.centroid_frames.extend(source.centroid_frames)
    target.rolloff_frames.extend(source.rolloff_frames)
    target.flatness_frames.extend(source.flatness_frames)
    target.rms_frames.extend(source.rms_frames)
    target.zcr_frames.extend(source.zcr_frames)
    target.onset_env_frames.extend(source.onset_env_frames)


def collapse_to_analysis(accumulated: BlockFeatures, sample_rate: int) -> AudioAnalysisData:
    """Collapse accumulated per-block features into a populated AudioAnalysisData.

    Derives all computable semantic fields. Fields that require external data
    (valence, speechiness, instrumentalness, acousticness) are left as None.

    :param accumulated: All block features accumulated during streaming.
    :param sample_rate: Sample rate used during extraction.
    """
    onset_env = np.concatenate(accumulated.onset_env_frames)
    chroma = np.concatenate(accumulated.chroma_frames, axis=1)
    rms = np.concatenate(accumulated.rms_frames, axis=1).squeeze()
    centroid = np.concatenate(accumulated.centroid_frames, axis=1).squeeze()
    contrast = np.concatenate(accumulated.contrast_frames, axis=1)
    flatness = np.concatenate(accumulated.flatness_frames, axis=1).squeeze()

    bpm = _derive_bpm(onset_env, sample_rate)
    key, mode = _derive_key_and_mode(chroma)
    energy = _derive_energy(rms)
    danceability = _derive_danceability(onset_env, bpm, sample_rate)
    loudness_integrated, loudness_range = _derive_loudness(rms)
    brightness = _derive_brightness(centroid, sample_rate)
    harmonic_complexity = _derive_harmonic_complexity(chroma)
    roughness = _derive_roughness(contrast, flatness)
    rhythmic_regularity = _derive_rhythmic_regularity(onset_env, sample_rate)
    rms_energy_series = _derive_rms_energy_series(rms)
    spectral_centroid_series = _derive_spectral_centroid_series(centroid, rms_energy_series)

    return AudioAnalysisData(
        bpm=bpm,
        key=key,
        mode=mode,
        energy=energy,
        danceability=danceability,
        loudness_integrated=loudness_integrated,
        loudness_range=loudness_range,
        brightness=brightness,
        harmonic_complexity=harmonic_complexity,
        roughness=roughness,
        rhythmic_regularity=rhythmic_regularity,
        rms_energy=rms_energy_series,
        spectral_centroid=spectral_centroid_series,
    )


def _clamp(value: float) -> float:
    """Clamp a float to [0.0, 1.0]."""
    return float(max(0.0, min(1.0, value)))


def _derive_bpm(onset_env: np.ndarray, sample_rate: int) -> float:
    """Estimate BPM from the accumulated onset envelope using librosa.

    :param onset_env: Concatenated onset strength envelope.
    :param sample_rate: Sample rate in Hz.
    """
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=FutureWarning)
        tempo = librosa.beat.tempo(onset_envelope=onset_env, sr=sample_rate)
    return float(np.asarray(tempo).flat[0])


def _derive_key_and_mode(chroma: np.ndarray) -> tuple[str, str]:
    """Detect musical key and mode using Krumhansl-Kessler profile correlation.

    :param chroma: Concatenated chroma feature matrix (12 x N_frames).
    """
    mean_chroma = chroma.mean(axis=1).astype(np.float64)

    # Correlate mean chroma profile with all 24 KK templates (12 major + 12 minor)
    # by cyclically rotating the profiles to each pitch class
    best_score = -np.inf
    best_pitch = 0
    best_mode = "major"

    for pitch in range(12):
        rolled_major = np.roll(_KK_MAJOR, pitch)
        rolled_minor = np.roll(_KK_MINOR, pitch)

        corr_major = float(np.corrcoef(mean_chroma, rolled_major)[0, 1])
        corr_minor = float(np.corrcoef(mean_chroma, rolled_minor)[0, 1])

        if corr_major > best_score:
            best_score = corr_major
            best_pitch = pitch
            best_mode = "major"
        if corr_minor > best_score:
            best_score = corr_minor
            best_pitch = pitch
            best_mode = "minor"

    return _PITCH_CLASSES[best_pitch], best_mode


def _derive_energy(rms: np.ndarray) -> float:
    """Compute normalized mean RMS energy in [0, 1].

    :param rms: Per-frame RMS values (1D after squeeze).
    """
    # RMS values are typically in [0, 1] for float32 audio; take mean and clamp
    return _clamp(float(rms.mean()))


def _derive_danceability(onset_env: np.ndarray, bpm: float, sample_rate: int) -> float:
    """Estimate danceability from onset regularity, tempo suitability, and onset strength.

    :param onset_env: Concatenated onset strength envelope.
    :param bpm: Estimated tempo in BPM.
    :param sample_rate: Sample rate in Hz.
    """
    hop_length = 512
    fps = sample_rate / hop_length

    # Onset regularity: low coefficient of variation of inter-onset intervals
    onset_frames = librosa.onset.onset_detect(onset_envelope=onset_env, sr=sample_rate)
    if len(onset_frames) >= 2:
        ioi = np.diff(onset_frames).astype(np.float64) / fps
        cv = float(ioi.std() / (ioi.mean() + 1e-8))
        regularity = _clamp(1.0 - cv)
    else:
        regularity = 0.0

    # Tempo suitability: triangle peak at 90-130 BPM
    if _DANCE_TEMPO_MIN <= bpm <= _DANCE_TEMPO_MAX:
        tempo_score = 1.0
    elif bpm < _DANCE_TEMPO_MIN:
        tempo_score = _clamp(bpm / _DANCE_TEMPO_MIN)
    else:
        tempo_score = _clamp(2.0 - bpm / _DANCE_TEMPO_MAX)

    # Normalised mean onset strength
    onset_strength = _clamp(float(onset_env.mean()) / (float(onset_env.max()) + 1e-8))

    return _clamp(0.4 * regularity + 0.4 * tempo_score + 0.2 * onset_strength)


def _derive_loudness(rms: np.ndarray) -> tuple[float, float]:
    """Compute RMS-derived dB approximations for integrated loudness and loudness range.

    :param rms: Per-frame RMS values (1D after squeeze).
    """
    rms_clipped = np.clip(rms, 1e-8, None)
    rms_db = 20.0 * np.log10(rms_clipped)
    loudness_integrated = float(rms_db.mean())
    loudness_range = float(rms_db.std())
    return loudness_integrated, loudness_range


def _derive_brightness(centroid: np.ndarray, sample_rate: int) -> float:
    """Compute mean spectral centroid normalized against the Nyquist frequency.

    :param centroid: Per-frame spectral centroid values in Hz (1D after squeeze).
    :param sample_rate: Sample rate in Hz.
    """
    nyquist = sample_rate / 2.0
    return _clamp(float(centroid.mean()) / nyquist)


def _derive_harmonic_complexity(chroma: np.ndarray) -> float:
    """Compute normalized Shannon entropy of the mean chroma vector.

    :param chroma: Concatenated chroma feature matrix (12 x N_frames).
    """
    mean_chroma = chroma.mean(axis=1).astype(np.float64)
    # Normalize to a probability distribution
    chroma_sum = mean_chroma.sum()
    if chroma_sum <= 0:
        return 0.0
    p = mean_chroma / chroma_sum
    p = np.clip(p, 1e-10, None)
    entropy = float(-np.sum(p * np.log(p)))
    # Max entropy for 12 bins is ln(12)
    max_entropy = float(np.log(12))
    return _clamp(entropy / max_entropy)


def _derive_roughness(contrast: np.ndarray, flatness: np.ndarray) -> float:
    """Combine spectral contrast range and spectral flatness into a roughness measure.

    :param contrast: Spectral contrast matrix (7 x N_frames).
    :param flatness: Per-frame spectral flatness values (1D after squeeze).
    """
    # High contrast range → more tonal variation → rougher texture
    contrast_range = float(contrast.max() - contrast.min())
    # Normalize against a reasonable max contrast range (~80 dB)
    contrast_score = _clamp(contrast_range / 80.0)

    # High flatness (noise-like) → rougher; low flatness (tonal) → smoother
    flatness_score = _clamp(float(flatness.mean()))

    return _clamp(0.6 * contrast_score + 0.4 * flatness_score)


def _derive_rhythmic_regularity(onset_env: np.ndarray, sample_rate: int) -> float:
    """Estimate rhythmic regularity as 1 minus the normalized CV of inter-onset intervals.

    :param onset_env: Concatenated onset strength envelope.
    :param sample_rate: Sample rate in Hz.
    """
    onset_frames = librosa.onset.onset_detect(onset_envelope=onset_env, sr=sample_rate)
    if len(onset_frames) < 2:
        return 0.0
    ioi = np.diff(onset_frames).astype(np.float64)
    cv = float(ioi.std() / (ioi.mean() + 1e-8))
    return _clamp(1.0 - cv)


def _derive_rms_energy_series(rms: np.ndarray) -> npt.NDArray[np.float32]:
    """Interpolate per-frame RMS onto fixed 1800 bins and peak-normalize.

    :param rms: Per-frame RMS values (1D after squeeze).
    """
    if len(rms) == 0:
        return np.zeros(_TIME_SERIES_BINS, dtype=np.float32)
    src_x = np.linspace(0.0, 1.0, num=len(rms))
    dst_x = np.linspace(0.0, 1.0, num=_TIME_SERIES_BINS)
    result = np.interp(dst_x, src_x, rms).astype(np.float32)
    peak = result.max()
    if peak > 0:
        result = result / peak
    return result


def _derive_spectral_centroid_series(
    centroid: np.ndarray, rms_energy: npt.NDArray[np.float32]
) -> npt.NDArray[np.float32]:
    """Interpolate per-frame centroid onto fixed 1800 bins, zeroing silent regions.

    :param centroid: Per-frame spectral centroid values in Hz (1D after squeeze).
    :param rms_energy: Normalized RMS energy series (1800 bins) used to mask silence.
    """
    if len(centroid) == 0:
        return np.zeros(_TIME_SERIES_BINS, dtype=np.float32)
    src_x = np.linspace(0.0, 1.0, num=len(centroid))
    dst_x = np.linspace(0.0, 1.0, num=_TIME_SERIES_BINS)
    result = np.interp(dst_x, src_x, centroid).astype(np.float32)
    result[rms_energy < _SILENCE_THRESHOLD] = 0.0
    return result
