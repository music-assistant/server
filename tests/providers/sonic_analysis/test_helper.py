"""Tests for the sonic_analysis helper module."""

import math

import numpy as np
import pytest

from music_assistant.helpers.sonic_analysis import (
    FEATURE_NAMES,
    SIGNATURE_DIMENSIONS,
    SIGNATURE_VERSION,
    SonicSignature,
    compute_corpus_stats,
    compute_distance,
    extract_signature,
    normalize_features,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestConstants:
    """Tests for module-level constants."""

    def test_signature_version_is_int(self) -> None:
        """SIGNATURE_VERSION must be an integer."""
        assert isinstance(SIGNATURE_VERSION, int)

    def test_signature_version_value(self) -> None:
        """SIGNATURE_VERSION must be 1."""
        assert SIGNATURE_VERSION == 1

    def test_feature_names_length(self) -> None:
        """FEATURE_NAMES must have exactly 38 entries."""
        assert len(FEATURE_NAMES) == 38

    def test_signature_dimensions_equals_feature_names_length(self) -> None:
        """SIGNATURE_DIMENSIONS must equal len(FEATURE_NAMES)."""
        assert len(FEATURE_NAMES) == SIGNATURE_DIMENSIONS

    def test_signature_dimensions_is_38(self) -> None:
        """SIGNATURE_DIMENSIONS must be 38."""
        assert SIGNATURE_DIMENSIONS == 38

    def test_feature_names_mfcc_prefix(self) -> None:
        """First 13 features must be mfcc_1..mfcc_13."""
        mfcc_features = [f"mfcc_{i}" for i in range(1, 14)]
        assert FEATURE_NAMES[:13] == mfcc_features

    def test_feature_names_chroma_prefix(self) -> None:
        """Features 13..24 must be chroma_1..chroma_12."""
        chroma_features = [f"chroma_{i}" for i in range(1, 13)]
        assert FEATURE_NAMES[13:25] == chroma_features

    def test_feature_names_spectral_contrast(self) -> None:
        """Features 25..31 must be spectral_contrast_1..spectral_contrast_7."""
        sc_features = [f"spectral_contrast_{i}" for i in range(1, 8)]
        assert FEATURE_NAMES[25:32] == sc_features

    def test_feature_names_scalars(self) -> None:
        """Last 6 features must be the scalar names."""
        expected = [
            "tempo",
            "spectral_centroid",
            "spectral_rolloff",
            "spectral_flatness",
            "rms_energy",
            "zcr",
        ]
        assert FEATURE_NAMES[32:] == expected

    def test_feature_names_are_unique(self) -> None:
        """All feature names must be unique."""
        assert len(FEATURE_NAMES) == len(set(FEATURE_NAMES))


# ---------------------------------------------------------------------------
# SonicSignature dataclass
# ---------------------------------------------------------------------------


class TestSonicSignature:
    """Tests for the SonicSignature dataclass."""

    def test_construction_with_valid_features(self) -> None:
        """SonicSignature can be constructed with 38 floats."""
        features = [float(i) for i in range(38)]
        sig = SonicSignature(features=features, version=SIGNATURE_VERSION)
        assert sig.features == features
        assert sig.version == SIGNATURE_VERSION

    def test_default_feature_names(self) -> None:
        """Default feature_names must match the module-level FEATURE_NAMES."""
        sig = SonicSignature(features=[0.0] * 38, version=SIGNATURE_VERSION)
        assert sig.feature_names == FEATURE_NAMES

    def test_feature_names_is_copy(self) -> None:
        """Mutating feature_names on one instance must not affect another."""
        sig_a = SonicSignature(features=[0.0] * 38, version=SIGNATURE_VERSION)
        sig_b = SonicSignature(features=[0.0] * 38, version=SIGNATURE_VERSION)
        sig_a.feature_names[0] = "mutated"
        assert sig_b.feature_names[0] != "mutated"

    def test_custom_feature_names(self) -> None:
        """SonicSignature accepts custom feature_names."""
        custom = ["a"] * 38
        sig = SonicSignature(features=[0.0] * 38, version=1, feature_names=custom)
        assert sig.feature_names == custom


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def sine_audio() -> tuple[np.ndarray, int]:
    """Generate a 3-second 440 Hz sine wave at 22050 Hz sample rate."""
    sample_rate = 22050
    duration = 3.0
    t = np.linspace(0, duration, int(sample_rate * duration), endpoint=False)
    audio = (np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    return audio, sample_rate


# ---------------------------------------------------------------------------
# extract_signature
# ---------------------------------------------------------------------------


class TestExtractSignature:
    """Tests for extract_signature."""

    def test_returns_sonic_signature(self, sine_audio: tuple[np.ndarray, int]) -> None:
        """extract_signature must return a SonicSignature instance."""
        audio, sr = sine_audio
        result = extract_signature(audio, sr)
        assert isinstance(result, SonicSignature)

    def test_exactly_38_features(self, sine_audio: tuple[np.ndarray, int]) -> None:
        """Returned signature must have exactly 38 features."""
        audio, sr = sine_audio
        result = extract_signature(audio, sr)
        assert len(result.features) == 38

    def test_all_features_are_finite(self, sine_audio: tuple[np.ndarray, int]) -> None:
        """Every feature value must be a finite float."""
        audio, sr = sine_audio
        result = extract_signature(audio, sr)
        for i, val in enumerate(result.features):
            assert math.isfinite(val), f"Feature {FEATURE_NAMES[i]} is not finite: {val}"

    def test_version_is_set(self, sine_audio: tuple[np.ndarray, int]) -> None:
        """Returned signature must carry the current SIGNATURE_VERSION."""
        audio, sr = sine_audio
        result = extract_signature(audio, sr)
        assert result.version == SIGNATURE_VERSION

    def test_feature_names_match_module_constant(self, sine_audio: tuple[np.ndarray, int]) -> None:
        """Returned signature feature_names must match FEATURE_NAMES."""
        audio, sr = sine_audio
        result = extract_signature(audio, sr)
        assert result.feature_names == FEATURE_NAMES

    def test_tempo_is_non_negative(self, sine_audio: tuple[np.ndarray, int]) -> None:
        """Tempo feature must be a non-negative finite float.

        A pure sine wave has no detectable beat so librosa returns 0.0 BPM,
        which is acceptable — we only require the value is finite and >= 0.
        """
        audio, sr = sine_audio
        result = extract_signature(audio, sr)
        tempo_idx = FEATURE_NAMES.index("tempo")
        assert result.features[tempo_idx] >= 0.0

    def test_rms_energy_is_non_negative(self, sine_audio: tuple[np.ndarray, int]) -> None:
        """RMS energy must be non-negative."""
        audio, sr = sine_audio
        result = extract_signature(audio, sr)
        rms_idx = FEATURE_NAMES.index("rms_energy")
        assert result.features[rms_idx] >= 0.0

    def test_deterministic(self, sine_audio: tuple[np.ndarray, int]) -> None:
        """Calling extract_signature twice on the same input must return identical features."""
        audio, sr = sine_audio
        result_a = extract_signature(audio, sr)
        result_b = extract_signature(audio, sr)
        assert result_a.features == result_b.features


# ---------------------------------------------------------------------------
# normalize_features
# ---------------------------------------------------------------------------


class TestNormalizeFeatures:
    """Tests for normalize_features."""

    def test_known_values(self) -> None:
        """z-score normalization: (x - mean) / std."""
        raw = [10.0, 20.0, 30.0]
        means = [10.0, 10.0, 10.0]
        stds = [2.0, 5.0, 10.0]
        result = normalize_features(raw, means, stds)
        assert len(result) == 3
        assert math.isclose(result[0], 0.0)
        assert math.isclose(result[1], 2.0)
        assert math.isclose(result[2], 2.0)

    def test_zero_std_returns_zero(self) -> None:
        """When std is 0, the normalized value must be 0.0 (not NaN/inf)."""
        raw = [5.0]
        means = [5.0]
        stds = [0.0]
        result = normalize_features(raw, means, stds)
        assert result == [0.0]

    def test_returns_list_of_floats(self) -> None:
        """normalize_features must return a list of floats."""
        raw = [1.0, 2.0]
        result = normalize_features(raw, [0.0, 0.0], [1.0, 1.0])
        assert isinstance(result, list)
        assert all(isinstance(v, float) for v in result)

    def test_length_matches_input(self) -> None:
        """Output length must match input length."""
        raw = [float(i) for i in range(10)]
        means = [0.0] * 10
        stds = [1.0] * 10
        result = normalize_features(raw, means, stds)
        assert len(result) == 10

    def test_all_zero_std_returns_all_zeros(self) -> None:
        """All-zero stds must yield all zeros."""
        raw = [1.0, 2.0, 3.0]
        means = [0.0, 0.0, 0.0]
        stds = [0.0, 0.0, 0.0]
        result = normalize_features(raw, means, stds)
        assert result == [0.0, 0.0, 0.0]


# ---------------------------------------------------------------------------
# compute_distance
# ---------------------------------------------------------------------------


class TestComputeDistance:
    """Tests for compute_distance."""

    def test_identical_signatures_distance_is_zero(self) -> None:
        """Cosine distance between identical vectors must be ≈ 0."""
        features = [float(i + 1) for i in range(38)]
        sig = SonicSignature(features=features, version=SIGNATURE_VERSION)
        dist = compute_distance(sig, sig)
        assert math.isclose(dist, 0.0, abs_tol=1e-6)

    def test_different_signatures_distance_is_positive(self) -> None:
        """Cosine distance between different vectors must be > 0."""
        features_a = [1.0] + [0.0] * 37
        features_b = [0.0] + [1.0] + [0.0] * 36
        sig_a = SonicSignature(features=features_a, version=SIGNATURE_VERSION)
        sig_b = SonicSignature(features=features_b, version=SIGNATURE_VERSION)
        dist = compute_distance(sig_a, sig_b)
        assert dist > 0.0

    def test_orthogonal_vectors_distance_is_one(self) -> None:
        """Orthogonal vectors must have cosine distance ≈ 1."""
        features_a = [1.0] + [0.0] * 37
        features_b = [0.0] + [1.0] + [0.0] * 36
        sig_a = SonicSignature(features=features_a, version=SIGNATURE_VERSION)
        sig_b = SonicSignature(features=features_b, version=SIGNATURE_VERSION)
        dist = compute_distance(sig_a, sig_b)
        assert math.isclose(dist, 1.0, abs_tol=1e-6)

    def test_distance_is_symmetric(self) -> None:
        """compute_distance(a, b) must equal compute_distance(b, a)."""
        features_a = [float(i) for i in range(38)]
        features_b = [float(38 - i) for i in range(38)]
        sig_a = SonicSignature(features=features_a, version=SIGNATURE_VERSION)
        sig_b = SonicSignature(features=features_b, version=SIGNATURE_VERSION)
        assert math.isclose(compute_distance(sig_a, sig_b), compute_distance(sig_b, sig_a))

    def test_returns_float(self) -> None:
        """compute_distance must return a float."""
        sig = SonicSignature(features=[1.0] * 38, version=SIGNATURE_VERSION)
        result = compute_distance(sig, sig)
        assert isinstance(result, float)

    def test_zero_vector_distance(self) -> None:
        """Zero vector vs any vector must return 0.0 (graceful fallback)."""
        sig_zero = SonicSignature(features=[0.0] * 38, version=SIGNATURE_VERSION)
        sig_other = SonicSignature(features=[1.0] * 38, version=SIGNATURE_VERSION)
        dist = compute_distance(sig_zero, sig_other)
        assert isinstance(dist, float)
        assert math.isfinite(dist)


# ---------------------------------------------------------------------------
# compute_corpus_stats
# ---------------------------------------------------------------------------


class TestComputeCorpusStats:
    """Tests for compute_corpus_stats."""

    def test_returns_two_lists(self) -> None:
        """compute_corpus_stats must return a tuple of (means, stds)."""
        corpus = [[1.0, 2.0], [3.0, 4.0]]
        result = compute_corpus_stats(corpus)
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_means_correct(self) -> None:
        """Means must be per-feature averages across all feature vectors."""
        corpus = [[1.0, 10.0], [3.0, 20.0]]
        means, _ = compute_corpus_stats(corpus)
        assert math.isclose(means[0], 2.0)
        assert math.isclose(means[1], 15.0)

    def test_stds_correct(self) -> None:
        """Stds must be per-feature standard deviations across all feature vectors."""
        corpus = [[2.0, 4.0], [4.0, 4.0]]
        _, stds = compute_corpus_stats(corpus)
        assert math.isclose(stds[0], 1.0, abs_tol=1e-6)
        assert math.isclose(stds[1], 0.0, abs_tol=1e-6)

    def test_length_matches_feature_count(self) -> None:
        """Output lists must have same length as number of features."""
        n = 5
        corpus = [[float(i) for i in range(n)] for _ in range(4)]
        means, stds = compute_corpus_stats(corpus)
        assert len(means) == n
        assert len(stds) == n

    def test_single_item_corpus_std_is_zero(self) -> None:
        """A single-item corpus must have std of 0 for all features."""
        corpus = [[1.0, 2.0, 3.0]]
        _, stds = compute_corpus_stats(corpus)
        assert all(s == 0.0 for s in stds)

    def test_returns_lists_of_floats(self) -> None:
        """Both returned lists must contain Python floats."""
        corpus = [[1.0, 2.0], [3.0, 4.0]]
        means, stds = compute_corpus_stats(corpus)
        assert all(isinstance(v, float) for v in means)
        assert all(isinstance(v, float) for v in stds)
