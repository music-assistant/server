"""Sonic analysis helper — pure feature extraction and similarity functions.

Extracts a 38-dimensional audio signature from raw PCM audio using librosa,
and provides normalization and cosine-distance utilities for similarity search.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import librosa
import numpy as np

SIGNATURE_VERSION: int = 1

FEATURE_NAMES: list[str] = [
    # MFCCs (13)
    "mfcc_1",
    "mfcc_2",
    "mfcc_3",
    "mfcc_4",
    "mfcc_5",
    "mfcc_6",
    "mfcc_7",
    "mfcc_8",
    "mfcc_9",
    "mfcc_10",
    "mfcc_11",
    "mfcc_12",
    "mfcc_13",
    # Chroma (12)
    "chroma_1",
    "chroma_2",
    "chroma_3",
    "chroma_4",
    "chroma_5",
    "chroma_6",
    "chroma_7",
    "chroma_8",
    "chroma_9",
    "chroma_10",
    "chroma_11",
    "chroma_12",
    # Spectral contrast (7)
    "spectral_contrast_1",
    "spectral_contrast_2",
    "spectral_contrast_3",
    "spectral_contrast_4",
    "spectral_contrast_5",
    "spectral_contrast_6",
    "spectral_contrast_7",
    # Scalars (6)
    "tempo",
    "spectral_centroid",
    "spectral_rolloff",
    "spectral_flatness",
    "rms_energy",
    "zcr",
]

SIGNATURE_DIMENSIONS: int = len(FEATURE_NAMES)  # 38


@dataclass
class SonicSignature:
    """A fixed-dimension vector representing the sonic character of a track.

    :param features: List of SIGNATURE_DIMENSIONS float values, one per feature.
    :param version: Schema version; used to detect stale cached signatures.
    :param feature_names: Ordered names corresponding to each feature value.
    """

    features: list[float]
    version: int
    feature_names: list[str] = field(default_factory=lambda: list(FEATURE_NAMES))


def extract_signature(audio: np.ndarray, sample_rate: int) -> SonicSignature:
    """Extract a 38-dimensional sonic signature from raw audio.

    :param audio: Mono float32 audio samples.
    :param sample_rate: Sample rate of the audio in Hz.
    """
    features: list[float] = []

    # MFCCs — 13 values, mean across time frames
    mfccs = librosa.feature.mfcc(y=audio, sr=sample_rate, n_mfcc=13)
    features.extend(float(v) for v in mfccs.mean(axis=1))

    # Chroma — 12 values, mean across time frames
    chroma = librosa.feature.chroma_stft(y=audio, sr=sample_rate)
    features.extend(float(v) for v in chroma.mean(axis=1))

    # Spectral contrast — 7 values (6 bands + 1 vallee), mean across time frames
    spectral_contrast = librosa.feature.spectral_contrast(y=audio, sr=sample_rate, n_bands=6)
    features.extend(float(v) for v in spectral_contrast.mean(axis=1))

    # Tempo — single BPM float
    tempo, _ = librosa.beat.beat_track(y=audio, sr=sample_rate)
    features.append(float(np.asarray(tempo).flat[0]))

    # Spectral centroid — mean across frames
    spectral_centroid = librosa.feature.spectral_centroid(y=audio, sr=sample_rate)
    features.append(float(spectral_centroid.mean()))

    # Spectral rolloff — mean across frames
    spectral_rolloff = librosa.feature.spectral_rolloff(y=audio, sr=sample_rate)
    features.append(float(spectral_rolloff.mean()))

    # Spectral flatness — mean across frames
    spectral_flatness = librosa.feature.spectral_flatness(y=audio)
    features.append(float(spectral_flatness.mean()))

    # RMS energy — mean across frames
    rms = librosa.feature.rms(y=audio)
    features.append(float(rms.mean()))

    # Zero-crossing rate — mean across frames
    zcr = librosa.feature.zero_crossing_rate(y=audio)
    features.append(float(zcr.mean()))

    return SonicSignature(features=features, version=SIGNATURE_VERSION)


def normalize_features(
    raw_features: list[float],
    corpus_means: list[float],
    corpus_stds: list[float],
) -> list[float]:
    """Apply per-feature z-score normalization to a feature vector.

    When the standard deviation for a feature is zero (constant feature across
    the corpus), the normalized value is set to 0.0 to avoid division by zero.

    :param raw_features: Raw feature values to normalize.
    :param corpus_means: Per-feature means computed over the analysis corpus.
    :param corpus_stds: Per-feature standard deviations over the analysis corpus.
    """
    result: list[float] = []
    for value, mean, std in zip(raw_features, corpus_means, corpus_stds, strict=False):
        if std == 0.0:
            result.append(0.0)
        else:
            result.append(float((value - mean) / std))
    return result


def compute_distance(sig_a: SonicSignature, sig_b: SonicSignature) -> float:
    """Compute cosine distance between two sonic signatures.

    Returns a value in [0, 1] where 0 means identical direction and 1 means
    orthogonal. When either vector has zero magnitude the distance is 0.0.

    :param sig_a: First signature.
    :param sig_b: Second signature.
    """
    a = np.array(sig_a.features, dtype=np.float64)
    b = np.array(sig_b.features, dtype=np.float64)
    norm_a = float(np.linalg.norm(a))
    norm_b = float(np.linalg.norm(b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    cosine_similarity = float(np.dot(a, b) / (norm_a * norm_b))
    # Clamp to [-1, 1] to guard against floating-point drift
    cosine_similarity = max(-1.0, min(1.0, cosine_similarity))
    return float(1.0 - cosine_similarity)


def compute_corpus_stats(
    all_features: list[list[float]],
) -> tuple[list[float], list[float]]:
    """Compute per-feature mean and standard deviation across a corpus of signatures.

    :param all_features: List of feature vectors, one per track in the corpus.
    """
    matrix = np.array(all_features, dtype=np.float64)
    means = matrix.mean(axis=0).tolist()
    stds = matrix.std(axis=0).tolist()
    return [float(v) for v in means], [float(v) for v in stds]
