"""Unit tests for the vectors module — assemble_vector + corpus stats + group distances."""

from __future__ import annotations

import math

import numpy as np
import pytest

from music_assistant.models.audio_analysis import AudioAnalysisData
from music_assistant.providers.sonic_similarity.vectors import (
    FEATURE_GROUPS,
    VECTOR_DIMENSIONS,
    assemble_vector,
    build_dimension_weights,
    compute_corpus_stats,
    compute_weighted_distance,
    compute_weighted_distance_vec,
    encode_key_mode,
    normalize_features,
)


def _make_analysis(**overrides: object) -> AudioAnalysisData:
    defaults: dict[str, object] = {
        "bpm": 120.0,
        "energy": 0.7,
        "danceability": 0.6,
        "loudness_integrated": -14.0,
        "loudness_range": 8.0,
        "brightness": 0.5,
        "harmonic_complexity": 0.4,
        "roughness": 0.3,
        "rhythmic_regularity": 0.8,
        "key": "C",
        "mode": "major",
        "rms_energy": np.array([0.1, 0.2, 0.15, 0.18], dtype=np.float32),
        "spectral_centroid": np.array([2000.0, 2200.0, 2100.0, 2050.0], dtype=np.float32),
    }
    defaults.update(overrides)
    return AudioAnalysisData(**defaults)  # type: ignore[arg-type]


class TestEncodeKeyMode:
    """Tests for circular key/mode encoding."""

    def test_c_major(self) -> None:
        """C major encodes to (0, 1, 1.0) via sin/cos of 0."""
        key_sin, key_cos, mode_val = encode_key_mode("C", "major")
        assert key_sin == pytest.approx(0.0, abs=1e-9)
        assert key_cos == pytest.approx(1.0, abs=1e-9)
        assert mode_val == pytest.approx(1.0)

    def test_f_sharp_minor(self) -> None:
        """F# minor: pitch class 6, mode = 0.0."""
        key_sin, key_cos, mode_val = encode_key_mode("F#", "minor")
        expected_sin = math.sin(2 * math.pi * 6 / 12)
        expected_cos = math.cos(2 * math.pi * 6 / 12)
        assert key_sin == pytest.approx(expected_sin, abs=1e-9)
        assert key_cos == pytest.approx(expected_cos, abs=1e-9)
        assert mode_val == pytest.approx(0.0)

    def test_b_and_c_are_close_circular(self) -> None:
        """B (pitch class 11) and C (pitch class 0) should be close in circular space."""
        sin_b, cos_b, _ = encode_key_mode("B", "major")
        sin_c, cos_c, _ = encode_key_mode("C", "major")
        # Euclidean distance between B and C in sin/cos space
        dist_bc = math.sqrt((sin_b - sin_c) ** 2 + (cos_b - cos_c) ** 2)
        # Compare to distance between B and F# (6 semitones apart — maximum distance)
        sin_fs, cos_fs, _ = encode_key_mode("F#", "major")
        dist_bfs = math.sqrt((sin_b - sin_fs) ** 2 + (cos_b - cos_fs) ** 2)
        assert dist_bc < dist_bfs

    def test_unknown_key_defaults_to_pitch_class_0(self) -> None:
        """Unknown key should default to pitch class 0 (same as C)."""
        sin_unknown, cos_unknown, _ = encode_key_mode("X", "major")
        sin_c, cos_c, _ = encode_key_mode("C", "major")
        assert sin_unknown == pytest.approx(sin_c, abs=1e-9)
        assert cos_unknown == pytest.approx(cos_c, abs=1e-9)

    def test_major_mode_is_1(self) -> None:
        """Major mode encodes to 1.0."""
        _, _, mode_val = encode_key_mode("A", "major")
        assert mode_val == pytest.approx(1.0)

    def test_minor_mode_is_0(self) -> None:
        """Minor mode encodes to 0.0."""
        _, _, mode_val = encode_key_mode("A", "minor")
        assert mode_val == pytest.approx(0.0)

    def test_all_12_pitch_classes(self) -> None:
        """All 12 standard pitch class names are recognized."""
        pitch_classes = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
        for pc in pitch_classes:
            sin_val, cos_val, _ = encode_key_mode(pc, "major")
            # Each result should be within [-1, 1]
            assert -1.0 <= sin_val <= 1.0
            assert -1.0 <= cos_val <= 1.0


class TestAssembleVector:
    """Tests for assemble_vector function."""

    def test_correct_dimensions(self) -> None:
        """Assembled vector has exactly VECTOR_DIMENSIONS dimensions."""
        analysis = _make_analysis()
        result = assemble_vector(analysis)
        assert result is not None
        assert len(result) == VECTOR_DIMENSIONS

    def test_returns_none_for_missing_scalar_field(self) -> None:
        """Returns None when any required scalar field is None."""
        for field in [
            "bpm",
            "energy",
            "danceability",
            "loudness_integrated",
            "loudness_range",
            "brightness",
            "harmonic_complexity",
            "roughness",
            "rhythmic_regularity",
        ]:
            analysis = _make_analysis(**{field: None})
            assert assemble_vector(analysis) is None, f"Expected None when {field} is None"

    def test_returns_none_for_missing_key(self) -> None:
        """Returns None when key is None."""
        analysis = _make_analysis(key=None)
        assert assemble_vector(analysis) is None

    def test_returns_none_for_missing_mode(self) -> None:
        """Returns None when mode is None."""
        analysis = _make_analysis(mode=None)
        assert assemble_vector(analysis) is None

    def test_deterministic(self) -> None:
        """Same input always produces the same vector."""
        analysis = _make_analysis()
        r1 = assemble_vector(analysis)
        r2 = assemble_vector(analysis)
        assert r1 == r2

    def test_all_finite(self) -> None:
        """All values in the vector are finite floats."""
        analysis = _make_analysis()
        result = assemble_vector(analysis)
        assert result is not None
        assert all(math.isfinite(v) for v in result)

    def test_different_inputs_produce_different_vectors(self) -> None:
        """Different analysis data produces different vectors."""
        a = _make_analysis(bpm=120.0, key="C")
        b = _make_analysis(bpm=180.0, key="G")
        va = assemble_vector(a)
        vb = assemble_vector(b)
        assert va is not None
        assert vb is not None
        assert va != vb

    def test_scalar_order(self) -> None:
        """First 9 elements match VECTOR_FIELDS order."""
        analysis = _make_analysis()
        result = assemble_vector(analysis)
        assert result is not None
        # bpm is index 0
        assert result[0] == pytest.approx(120.0)
        # energy is index 1
        assert result[1] == pytest.approx(0.7)
        # danceability is index 2
        assert result[2] == pytest.approx(0.6)

    def test_key_mode_at_indices_13_14_15(self) -> None:
        """Indices 13, 14, 15 hold key_sin, key_cos, mode."""
        analysis = _make_analysis(key="C", mode="major")
        result = assemble_vector(analysis)
        assert result is not None
        expected_sin, expected_cos, expected_mode = encode_key_mode("C", "major")
        assert result[13] == pytest.approx(expected_sin, abs=1e-9)
        assert result[14] == pytest.approx(expected_cos, abs=1e-9)
        assert result[15] == pytest.approx(expected_mode)

    def test_rms_variance_at_index_16(self) -> None:
        """Index 16 holds variance of rms_energy."""
        rms = np.array([0.1, 0.2, 0.15, 0.18], dtype=np.float32)
        analysis = _make_analysis(rms_energy=rms)
        result = assemble_vector(analysis)
        assert result is not None
        assert result[16] == pytest.approx(float(np.var(rms)), rel=1e-5)

    def test_centroid_variance_at_index_17(self) -> None:
        """Index 17 holds variance of spectral_centroid."""
        centroid = np.array([2000.0, 2200.0, 2100.0, 2050.0], dtype=np.float32)
        analysis = _make_analysis(spectral_centroid=centroid)
        result = assemble_vector(analysis)
        assert result is not None
        assert result[17] == pytest.approx(float(np.var(centroid)), rel=1e-5)

    def test_rms_variance_defaults_to_zero_when_none(self) -> None:
        """rms_energy=None → variance index 16 is 0.0."""
        analysis = _make_analysis(rms_energy=None)
        result = assemble_vector(analysis)
        assert result is not None
        assert result[16] == pytest.approx(0.0)

    def test_centroid_variance_defaults_to_zero_when_none(self) -> None:
        """spectral_centroid=None → variance index 17 is 0.0."""
        analysis = _make_analysis(spectral_centroid=None)
        result = assemble_vector(analysis)
        assert result is not None
        assert result[17] == pytest.approx(0.0)

    def test_rms_variance_defaults_to_zero_for_single_element(self) -> None:
        """rms_energy with length 1 → variance index 16 is 0.0."""
        analysis = _make_analysis(rms_energy=np.array([0.5], dtype=np.float32))
        result = assemble_vector(analysis)
        assert result is not None
        assert result[16] == pytest.approx(0.0)

    def test_returns_list_of_floats(self) -> None:
        """Return type is a list of Python floats."""
        analysis = _make_analysis()
        result = assemble_vector(analysis)
        assert result is not None
        assert isinstance(result, list)
        assert all(isinstance(v, float) for v in result)


class TestNormalizeFeatures:
    """Tests for z-score + L2 normalization."""

    def test_unit_length_after_normalization(self) -> None:
        """Normalized vector has L2 norm of 1.0."""
        raw = [
            120.0,
            0.7,
            0.6,  # rhythm: bpm, energy, danceability
            -14.0,
            8.0,  # loudness: integrated, range
            0.5,
            0.4,
            0.3,  # timbre: brightness, harmonic, roughness
            0.8,  # regularity
            0.5,
            0.5,
            0.5,
            0.5,  # mood: instrumentalness, valence, arousal, acousticness
            0.0,
            1.0,
            1.0,  # tonal: key_sin, key_cos, mode
            0.01,
            5000.0,  # dynamics: rms_var, centroid_var
        ]
        assert len(raw) == VECTOR_DIMENSIONS
        stds = [1.0] * VECTOR_DIMENSIONS
        # Shift means so z-scores are non-zero
        means_shifted = [v + 1.0 for v in raw]
        result = normalize_features(raw, means_shifted, stds)
        norm = math.sqrt(sum(v**2 for v in result))
        assert norm == pytest.approx(1.0, abs=1e-9)

    def test_zero_std_yields_zero_for_that_feature(self) -> None:
        """Feature with std=0 produces 0.0 for that dimension."""
        raw = [1.0] * VECTOR_DIMENSIONS
        means = [1.0] * VECTOR_DIMENSIONS
        stds = [1.0] * VECTOR_DIMENSIONS
        stds[3] = 0.0  # zero std for dimension 3
        result = normalize_features(raw, means, stds)
        # Dimension 3 z-score is 0 (due to zero std), all others also 0 (raw == mean)
        # Zero vector → return z-scores without L2 normalization
        assert result[3] == pytest.approx(0.0)

    def test_zero_norm_returns_z_scored_without_l2(self) -> None:
        """When z-score vector is all zeros, return it without L2 normalization."""
        raw = [1.0] * VECTOR_DIMENSIONS
        means = [1.0] * VECTOR_DIMENSIONS
        stds = [1.0] * VECTOR_DIMENSIONS
        result = normalize_features(raw, means, stds)
        # All z-scores = 0 → zero norm → return zeros
        assert all(v == pytest.approx(0.0) for v in result)

    def test_output_dimensionality_preserved(self) -> None:
        """Output has the same number of dimensions as input."""
        raw = [float(i) for i in range(VECTOR_DIMENSIONS)]
        means = [0.0] * VECTOR_DIMENSIONS
        stds = [1.0] * VECTOR_DIMENSIONS
        result = normalize_features(raw, means, stds)
        assert len(result) == VECTOR_DIMENSIONS

    def test_returns_list_of_floats(self) -> None:
        """Output is a list of Python floats."""
        raw = [float(i) for i in range(VECTOR_DIMENSIONS)]
        means = [0.0] * VECTOR_DIMENSIONS
        stds = [1.0] * VECTOR_DIMENSIONS
        result = normalize_features(raw, means, stds)
        assert isinstance(result, list)
        assert all(isinstance(v, float) for v in result)


class TestComputeCorpusStats:
    """Tests for compute_corpus_stats function."""

    def test_basic_stats(self) -> None:
        """Compute means and stds for a simple corpus."""
        features = [
            [0.0, 2.0],
            [2.0, 4.0],
            [4.0, 6.0],
        ]
        means, stds = compute_corpus_stats(features)
        assert means == pytest.approx([2.0, 4.0])
        assert stds == pytest.approx([math.sqrt(8 / 3), math.sqrt(8 / 3)], rel=1e-5)

    def test_single_feature_vector(self) -> None:
        """Single feature vector returns that vector as means and zeros as stds."""
        features = [[1.0, 2.0, 3.0]]
        means, stds = compute_corpus_stats(features)
        assert means == pytest.approx([1.0, 2.0, 3.0])
        assert stds == pytest.approx([0.0, 0.0, 0.0])

    def test_empty_corpus_raises_value_error(self) -> None:
        """Empty corpus raises ValueError."""
        with pytest.raises(ValueError, match=r"[Ee]mpty"):
            compute_corpus_stats([])

    def test_output_lengths_match_feature_dim(self) -> None:
        """Output lists have same length as feature dimension."""
        features = [[1.0, 2.0, 3.0, 4.0], [5.0, 6.0, 7.0, 8.0]]
        means, stds = compute_corpus_stats(features)
        assert len(means) == 4
        assert len(stds) == 4

    def test_returns_lists_of_floats(self) -> None:
        """Output is two lists of Python floats."""
        features = [[1.0, 2.0], [3.0, 4.0]]
        means, stds = compute_corpus_stats(features)
        assert isinstance(means, list)
        assert isinstance(stds, list)
        assert all(isinstance(v, float) for v in means)
        assert all(isinstance(v, float) for v in stds)


class TestComputeWeightedDistance:
    """Tests for compute_weighted_distance function."""

    def _make_vector(self, val: float = 0.0) -> list[float]:
        """Build a uniform vector at VECTOR_DIMENSIONS-length."""
        return [val] * VECTOR_DIMENSIONS

    def test_identical_vectors_give_zero_distance(self) -> None:
        """Distance between identical vectors is 0."""
        v = self._make_vector(1.0)
        assert compute_weighted_distance(v, v, {}) == pytest.approx(0.0)

    def test_symmetric(self) -> None:
        """Distance is symmetric: d(a,b) == d(b,a)."""
        a = assemble_vector(_make_analysis(bpm=120.0))
        b = assemble_vector(_make_analysis(bpm=180.0))
        assert a is not None
        assert b is not None
        d_ab = compute_weighted_distance(a, b, {})
        d_ba = compute_weighted_distance(b, a, {})
        assert d_ab == pytest.approx(d_ba)

    def test_zeroed_group_weight_changes_result(self) -> None:
        """Setting a group's weight to 0 reduces total distance."""
        a = assemble_vector(_make_analysis(bpm=120.0, energy=0.2))
        b = assemble_vector(_make_analysis(bpm=180.0, energy=0.9))
        assert a is not None
        assert b is not None
        d_no_weight = compute_weighted_distance(a, b, {})
        d_rhythm_zeroed = compute_weighted_distance(a, b, {"rhythm": 0.0})
        # Zeroing the rhythm group (which contains bpm) should reduce distance
        assert d_rhythm_zeroed < d_no_weight

    def test_missing_group_weight_defaults_to_1(self) -> None:
        """Missing group weight defaults to 1.0."""
        a = self._make_vector(0.0)
        b = self._make_vector(1.0)
        # All groups use default weight 1.0
        d_no_weights = compute_weighted_distance(a, b, {})
        d_explicit_ones = compute_weighted_distance(a, b, dict.fromkeys(FEATURE_GROUPS, 1.0))
        assert d_no_weights == pytest.approx(d_explicit_ones)

    def test_positive_distance_for_different_vectors(self) -> None:
        """Distance is positive for non-identical vectors."""
        a = self._make_vector(0.0)
        b = self._make_vector(1.0)
        assert compute_weighted_distance(a, b, {}) > 0.0

    def test_normalization_by_dimension_count(self) -> None:
        """Distance is normalized so it does not grow arbitrarily with dimensions."""
        a = self._make_vector(0.0)
        b = self._make_vector(1.0)
        d = compute_weighted_distance(a, b, {})
        # With equal weights (1.0) and uniform diff of 1.0, result should be
        # normalized so large-dim vectors don't dominate
        assert d < 10.0  # Sanity: not an unnormalized sum

    @staticmethod
    def _reference_distance(
        sig_a: list[float], sig_b: list[float], weights: dict[str, float]
    ) -> float:
        """
        Replicate the pre-vectorization per-group sqrt-then-square formula.

        Mirrors the original compute_weighted_distance implementation so the
        refactored vectorized version can be asserted numerically equivalent.
        """
        a = np.array(sig_a, dtype=np.float64)
        b = np.array(sig_b, dtype=np.float64)
        weighted_sq_sum = 0.0
        total_weighted_dims = 0.0
        for group, (start, end) in FEATURE_GROUPS.items():
            w = weights.get(group, 1.0)
            dim_count = end - start
            diff = a[start:end] - b[start:end]
            group_dist = math.sqrt(float(np.dot(diff, diff)) / dim_count)
            weighted_sq_sum += w * (group_dist**2) * dim_count
            total_weighted_dims += w * dim_count
        if total_weighted_dims == 0.0:
            return 0.0
        return math.sqrt(weighted_sq_sum / total_weighted_dims)

    def test_matches_reference_per_group_formula(self) -> None:
        """Vectorized result equals the original per-group formula across random inputs."""
        rng = np.random.default_rng(1234)
        weight_sets = [
            {},
            {"rhythm": 0.0},
            {"mood": 2.5, "tonal": 0.3},
            dict.fromkeys(FEATURE_GROUPS, 0.7),
        ]
        for _ in range(20):
            a = rng.standard_normal(VECTOR_DIMENSIONS).tolist()
            b = rng.standard_normal(VECTOR_DIMENSIONS).tolist()
            for weights in weight_sets:
                assert compute_weighted_distance(a, b, weights) == pytest.approx(
                    self._reference_distance(a, b, weights)
                )

    def test_numpy_array_input_matches_list_input(self) -> None:
        """Passing numpy arrays yields the same float as passing lists (the MMR hot path)."""
        rng = np.random.default_rng(99)
        a = rng.standard_normal(VECTOR_DIMENSIONS)
        b = rng.standard_normal(VECTOR_DIMENSIONS)
        weights = {"timbre": 1.5, "loudness": 0.2}
        from_arrays = compute_weighted_distance(a, b, weights)
        from_lists = compute_weighted_distance(a.tolist(), b.tolist(), weights)
        assert isinstance(from_arrays, float)
        assert from_arrays == pytest.approx(from_lists)

    def test_all_zero_weights_returns_zero(self) -> None:
        """When every group weight is 0, the distance is defined as 0.0."""
        a = self._make_vector(0.0)
        b = self._make_vector(1.0)
        all_zero = dict.fromkeys(FEATURE_GROUPS, 0.0)
        assert compute_weighted_distance(a, b, all_zero) == 0.0

    def test_precomputed_weights_match_dict_path(self) -> None:
        """compute_weighted_distance_vec with a prebuilt vector equals the dict-based path."""
        rng = np.random.default_rng(7)
        a = rng.standard_normal(VECTOR_DIMENSIONS)
        b = rng.standard_normal(VECTOR_DIMENSIONS)
        weights = {"rhythm": 2.0, "dynamics": 0.0}
        dim_weights = build_dimension_weights(weights)
        assert compute_weighted_distance_vec(a, b, dim_weights) == pytest.approx(
            compute_weighted_distance(a, b, weights)
        )

    def test_build_dimension_weights_expands_groups(self) -> None:
        """Each group's weight lands on its dimensions; absent groups default to 1.0."""
        dim_weights = build_dimension_weights({"timbre": 3.0})
        assert len(dim_weights) == VECTOR_DIMENSIONS
        start, end = FEATURE_GROUPS["timbre"]
        assert list(dim_weights[start:end]) == [3.0] * (end - start)
        # rhythm was not overridden, so it keeps the 1.0 default
        r_start, r_end = FEATURE_GROUPS["rhythm"]
        assert list(dim_weights[r_start:r_end]) == [1.0] * (r_end - r_start)
