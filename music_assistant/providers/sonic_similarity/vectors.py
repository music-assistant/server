"""
18-dimensional semantic vector schema for sonic similarity search.

Maps AudioAnalysisData to a fixed-size float vector for USearch ANN indexing.
Dimensions: [0-8] required scalars, [9-12] optional ML scalars, [13-15] key
sin/cos + mode, [16-17] RMS and spectral-centroid time-series variance.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from music_assistant.models.audio_analysis import AudioAnalysisData

PITCH_CLASS_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
PITCH_CLASS_INDEX = {name: idx for idx, name in enumerate(PITCH_CLASS_NAMES)}

# Required fields — must be non-None for a valid vector
VECTOR_FIELDS = [
    "bpm",
    "energy",
    "danceability",
    "loudness_integrated",
    "loudness_range",
    "brightness",
    "harmonic_complexity",
    "roughness",
    "rhythmic_regularity",
]

# Optional ML fields — use neutral default (0.5) when absent
OPTIONAL_FIELDS = [
    "instrumentalness",
    "valence",
    "arousal",
    "acousticness",
]
OPTIONAL_DEFAULT = 0.5

# 9 required + 4 optional ML + 2 key encoding + 1 mode + 2 time-series variance = 18
VECTOR_DIMENSIONS = 18

FEATURE_GROUPS = {
    "rhythm": (0, 3),  # bpm, energy, danceability
    "loudness": (3, 5),  # loudness_integrated, loudness_range
    "timbre": (5, 8),  # brightness, harmonic_complexity, roughness
    "regularity": (8, 9),  # rhythmic_regularity
    "mood": (9, 13),  # instrumentalness, valence, arousal, acousticness
    "tonal": (13, 16),  # key_sin, key_cos, mode
    "dynamics": (16, 18),  # rms_variance, centroid_variance
}


def encode_key_mode(key: str, mode: str) -> tuple[float, float, float]:
    """
    Encode musical key and mode as three floats for circular and binary representation.

    :param key: Pitch class name (e.g. "C", "F#"). Unknown keys default to pitch class 0.
    :param mode: Tonality string — "major" encodes to 1.0, anything else to 0.0.
    :returns: Tuple of (key_sin, key_cos, mode_float).
    """
    pitch_class = PITCH_CLASS_INDEX.get(key, 0)
    angle = 2.0 * math.pi * pitch_class / 12
    key_sin = math.sin(angle)
    key_cos = math.cos(angle)
    mode_float = 1.0 if mode == "major" else 0.0
    return key_sin, key_cos, mode_float


def assemble_vector(analysis: AudioAnalysisData) -> list[float] | None:
    """
    Assemble the VECTOR_DIMENSIONS-element feature vector for an analysis row.

    :param analysis: Source audio analysis data.
    :returns: List of floats sized to VECTOR_DIMENSIONS, or None when any required
        field (VECTOR_FIELDS, key, mode) is missing or NaN.
    """
    # The isinstance guard is load-bearing: getattr can return non-numeric junk
    # (e.g. a list serialized into the wrong column) which would crash math.isnan.
    for field in VECTOR_FIELDS:
        val = getattr(analysis, field)
        if not isinstance(val, (int, float)) or math.isnan(float(val)):
            return None
    if analysis.key is None or analysis.mode is None:
        return None

    vec: list[float] = [float(getattr(analysis, field)) for field in VECTOR_FIELDS]

    # Optional ML scalars default to OPTIONAL_DEFAULT (= 0.5, the neutral midpoint
    # of the 0..1 calibrated probability range) when absent or NaN.
    for field in OPTIONAL_FIELDS:
        val = getattr(analysis, field, None)
        vec.append(
            float(val)
            if isinstance(val, (int, float)) and not math.isnan(float(val))
            else OPTIONAL_DEFAULT
        )

    key_sin, key_cos, mode_float = encode_key_mode(analysis.key, analysis.mode)
    vec.extend([key_sin, key_cos, mode_float])

    # 2 time-series variance (NaN-safe: np.var of NaN-containing arrays yields NaN)
    rms = analysis.rms_energy
    rms_var = float(np.var(rms)) if rms is not None and len(rms) > 1 else 0.0
    vec.append(rms_var if not math.isnan(rms_var) else 0.0)

    centroid = analysis.spectral_centroid
    centroid_var = float(np.var(centroid)) if centroid is not None and len(centroid) > 1 else 0.0
    vec.append(centroid_var if not math.isnan(centroid_var) else 0.0)

    return vec


def normalize_features(
    raw_features: list[float],
    corpus_means: list[float],
    corpus_stds: list[float],
) -> list[float]:
    """
    Apply z-score then L2 normalization to a raw feature vector.

    :param raw_features: Raw feature vector to normalize.
    :param corpus_means: Per-feature means from the corpus.
    :param corpus_stds: Per-feature standard deviations from the corpus.
    :returns: Normalized feature vector as a list of floats.
    """
    # Zero std → 0.0 for that dimension; zero L2 norm → return z-scores as-is.
    z_scored = [
        (v - m) / s if s != 0.0 else 0.0
        for v, m, s in zip(raw_features, corpus_means, corpus_stds, strict=True)
    ]

    norm = math.sqrt(sum(v * v for v in z_scored))
    if norm == 0.0:
        return [float(v) for v in z_scored]

    return [float(v / norm) for v in z_scored]


def compute_corpus_stats(
    all_features: list[list[float]],
) -> tuple[list[float], list[float]]:
    """
    Compute per-feature means and standard deviations across a corpus.

    :param all_features: List of feature vectors (all same dimensionality).
    :returns: Tuple of (means, stds) as lists of floats.
    :raises ValueError: If all_features is empty.
    """
    if not all_features:
        msg = "Empty corpus: cannot compute stats from zero feature vectors"
        raise ValueError(msg)

    arr = np.array(all_features, dtype=np.float64)
    means = arr.mean(axis=0)
    stds = arr.std(axis=0)
    return [float(v) for v in means], [float(v) for v in stds]


def compute_group_distances(
    sig_a: list[float],
    sig_b: list[float],
) -> dict[str, float]:
    """
    Compute per-group raw (unweighted) Euclidean distance between two feature vectors.

    :param sig_a: First feature vector.
    :param sig_b: Second feature vector.
    """
    a = np.array(sig_a, dtype=np.float64)
    b = np.array(sig_b, dtype=np.float64)
    result: dict[str, float] = {}
    for group, (start, end) in FEATURE_GROUPS.items():
        diff = a[start:end] - b[start:end]
        dim_count = end - start
        result[group] = math.sqrt(float(np.dot(diff, diff)) / dim_count)
    return result


def build_dimension_weights(weights: dict[str, float]) -> np.ndarray:
    """
    Expand per-group weights into a per-dimension weight vector.

    Callers in a hot loop (e.g. MMR re-ranking) can build this once and reuse it
    across many compute_weighted_distance_vec calls instead of rebuilding it per call.

    :param weights: Per-group weight overrides keyed by FEATURE_GROUPS name. Groups
        absent from the dict default to weight 1.0.
    :returns: Float64 weight array of length VECTOR_DIMENSIONS.
    """
    dim_weights = np.ones(VECTOR_DIMENSIONS, dtype=np.float64)
    for group, (start, end) in FEATURE_GROUPS.items():
        if group in weights:
            dim_weights[start:end] = weights[group]
    return dim_weights


def compute_weighted_distance(
    sig_a: list[float] | np.ndarray,
    sig_b: list[float] | np.ndarray,
    weights: dict[str, float],
) -> float:
    """
    Compute per-group weighted Euclidean distance between two feature vectors.

    :param sig_a: First feature vector (list or numpy array).
    :param sig_b: Second feature vector (list or numpy array).
    :param weights: Per-group weight overrides keyed by FEATURE_GROUPS name.
    :returns: Weighted normalized distance as a float.
    """
    return compute_weighted_distance_vec(sig_a, sig_b, build_dimension_weights(weights))


def compute_weighted_distance_vec(
    sig_a: list[float] | np.ndarray,
    sig_b: list[float] | np.ndarray,
    dim_weights: np.ndarray,
) -> float:
    """
    Compute weighted Euclidean distance from a precomputed per-dimension weight vector.

    :param sig_a: First feature vector (list or numpy array).
    :param sig_b: Second feature vector (list or numpy array).
    :param dim_weights: Per-dimension weights as built by build_dimension_weights.
    :returns: Weighted normalized distance as a float.
    """
    total_weighted_dims = float(dim_weights.sum())
    if total_weighted_dims == 0.0:
        return 0.0
    # np.asarray is a no-op when the caller already holds a float64 array (the
    # MMR hot path), avoiding the list round-trip the previous version forced.
    a = np.asarray(sig_a, dtype=np.float64)
    b = np.asarray(sig_b, dtype=np.float64)
    diff = a - b
    weighted_sq_sum = float(np.dot(dim_weights, diff * diff))
    return math.sqrt(weighted_sq_sum / total_weighted_dims)


def build_debug_breakdown(
    seed_normalized: list[float],
    cand_normalized: list[float],
    weights: dict[str, float],
    displayed_dist: float,
) -> dict[str, Any]:
    """
    Build a per-track diagnostic dict (weighted_distance, metadata_bonus, group_distances).

    :param seed_normalized: Normalized seed feature vector.
    :param cand_normalized: Normalized candidate feature vector.
    :param weights: Per-feature-group weights (preset-derived).
    :param displayed_dist: The distance value shown to the user (post any
        metadata reranking + MMR pass-through).
    """
    weighted = compute_weighted_distance(seed_normalized, cand_normalized, weights)
    return {
        "weighted_distance": round(weighted, 4),
        "metadata_bonus": round(displayed_dist - weighted, 4),
        "group_distances": {
            k: round(v, 4)
            for k, v in compute_group_distances(seed_normalized, cand_normalized).items()
        },
    }
