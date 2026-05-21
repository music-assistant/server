"""Tests for the sonic similarity plugin API parameter handling."""

from __future__ import annotations

import pytest

from music_assistant.providers.sonic_similarity import (
    SIMILARITY_PRESETS,
    _parse_similar_params,
    _parse_weights,
    apply_filters,
)


class TestParseSimilarParams:
    """Tests for _parse_similar_params validation and normalization."""

    def test_item_id_alias(self) -> None:
        """Single item_id string wraps into item_ids list."""
        params = _parse_similar_params(item_id="abc")
        assert params.item_ids == ["abc"]

    def test_item_ids_list(self) -> None:
        """item_ids list passes through directly."""
        params = _parse_similar_params(item_ids=["a", "b"])
        assert params.item_ids == ["a", "b"]

    def test_item_id_and_item_ids_prefers_ids(self) -> None:
        """When both provided, item_ids takes precedence."""
        params = _parse_similar_params(item_id="old", item_ids=["new"])
        assert params.item_ids == ["new"]

    def test_no_ids_raises(self) -> None:
        """Must provide at least item_id or item_ids."""
        with pytest.raises(ValueError, match="item_id"):
            _parse_similar_params()

    def test_limit_clamped(self) -> None:
        """Limit is clamped to [1, 100]."""
        assert _parse_similar_params(item_id="x", limit=0).limit == 1
        assert _parse_similar_params(item_id="x", limit=200).limit == 100
        assert _parse_similar_params(item_id="x", limit=50).limit == 50

    def test_depth_clamped(self) -> None:
        """Depth is clamped to [1, 5]."""
        assert _parse_similar_params(item_id="x", depth=0).depth == 1
        assert _parse_similar_params(item_id="x", depth=10).depth == 5

    def test_diversity_clamped(self) -> None:
        """Diversity is clamped to [0.0, 1.0]."""
        assert _parse_similar_params(item_id="x", diversity=-1.0).diversity == 0.0
        assert _parse_similar_params(item_id="x", diversity=5.0).diversity == 1.0

    def test_blend_mode_validated(self) -> None:
        """Invalid blend_mode falls back to centroid."""
        assert _parse_similar_params(item_id="x", blend_mode="centroid").blend_mode == "centroid"
        assert _parse_similar_params(item_id="x", blend_mode="union").blend_mode == "union"
        assert _parse_similar_params(item_id="x", blend_mode="invalid").blend_mode == "centroid"

    def test_seed_weights_length_validated(self) -> None:
        """seed_weights length must match item_ids."""
        with pytest.raises(ValueError, match="seed_weights"):
            _parse_similar_params(item_ids=["a", "b"], seed_weights=[1.0])

    def test_candidates_scaled_with_filters(self) -> None:
        """Candidates doubled when filters are active."""
        no_filter = _parse_similar_params(item_id="x", candidates=50)
        with_filter = _parse_similar_params(item_id="x", candidates=50, filter_genres=["jazz"])
        assert with_filter.candidates == no_filter.candidates * 2

    def test_defaults(self) -> None:
        """Verify all default values."""
        params = _parse_similar_params(item_id="x")
        assert params.limit == 25
        assert params.depth == 1
        assert params.branch_factor == 5
        assert params.blend_mode == "centroid"
        assert params.seed_weights is None
        assert params.diversity == 0.0
        assert params.preset == "balanced"
        assert params.resolve is False
        assert params.filter_genres is None
        assert params.filter_providers is None
        assert params.exclude_track_ids is None
        assert params.exclude_artists is None


class TestApplyFilters:
    """Tests for post-ANN filter pipeline."""

    def test_no_filters_passes_all(self) -> None:
        """All candidates pass with no active filters."""
        candidates = [("a", "prov1", 0.1), ("b", "prov2", 0.2)]
        result = apply_filters(
            candidates, seed_ids=set(), exclude_track_ids=None, filter_providers=None
        )
        assert len(result) == 2

    def test_exclude_seed_ids(self) -> None:
        """Seed IDs are always excluded."""
        candidates = [("seed", "prov1", 0.1), ("other", "prov1", 0.2)]
        result = apply_filters(
            candidates, seed_ids={"seed"}, exclude_track_ids=None, filter_providers=None
        )
        assert len(result) == 1
        assert result[0][0] == "other"

    def test_exclude_track_ids(self) -> None:
        """Explicitly excluded track IDs are removed."""
        candidates = [("a", "p", 0.1), ("b", "p", 0.2), ("c", "p", 0.3)]
        result = apply_filters(
            candidates, seed_ids=set(), exclude_track_ids={"a", "c"}, filter_providers=None
        )
        assert [r[0] for r in result] == ["b"]

    def test_filter_providers(self) -> None:
        """Only candidates from listed providers are kept."""
        candidates = [("a", "prov1", 0.1), ("b", "prov2", 0.2), ("c", "prov1", 0.3)]
        result = apply_filters(
            candidates, seed_ids=set(), exclude_track_ids=None, filter_providers={"prov1"}
        )
        assert [r[0] for r in result] == ["a", "c"]

    def test_all_filters_combined(self) -> None:
        """Filters stack: seed exclusion + exclude_track_ids + filter_providers."""
        candidates = [
            ("seed", "prov1", 0.0),
            ("a", "prov1", 0.1),
            ("b", "prov2", 0.2),
            ("c", "prov1", 0.3),
        ]
        result = apply_filters(
            candidates, seed_ids={"seed"}, exclude_track_ids={"c"}, filter_providers={"prov1"}
        )
        assert [r[0] for r in result] == ["a"]


class TestParseWeights:
    """Tests for _parse_weights dict-based weight parsing."""

    def test_default_preset(self) -> None:
        """Empty params return balanced preset defaults."""
        result = _parse_weights({})
        balanced = SIMILARITY_PRESETS["balanced"]
        assert result == balanced

    def test_named_preset(self) -> None:
        """Selecting a preset by name uses its values."""
        result = _parse_weights({"preset": "party"})
        party = SIMILARITY_PRESETS["party"]
        assert result["rhythm"] == party["rhythm"]
        assert result["timbre"] == party["timbre"]

    def test_unknown_preset_falls_back(self) -> None:
        """Unknown preset name falls back to balanced."""
        result = _parse_weights({"preset": "nonexistent"})
        assert result == SIMILARITY_PRESETS["balanced"]

    def test_individual_override(self) -> None:
        """Individual weight overrides take precedence over preset."""
        result = _parse_weights({"preset": "balanced", "rhythm_weight": "0.3"})
        assert abs(result["rhythm"] - 0.3) < 0.01
        assert result["timbre"] == SIMILARITY_PRESETS["balanced"]["timbre"]

    def test_clamping(self) -> None:
        """Values outside [0, 1] are clamped."""
        result = _parse_weights({"rhythm_weight": "1.5", "timbre_weight": "-0.3"})
        assert result["rhythm"] == 1.0
        assert result["timbre"] == 0.0

    def test_invalid_string_falls_back(self) -> None:
        """Non-numeric string falls back to preset default."""
        result = _parse_weights({"preset": "vibe", "rhythm_weight": "abc"})
        assert result["rhythm"] == SIMILARITY_PRESETS["vibe"]["rhythm"]

    def test_returns_dict(self) -> None:
        """Result is a plain dict with every preset weight key (7 audio groups + 2 metadata)."""
        result = _parse_weights({})
        assert isinstance(result, dict)
        for key in (
            "rhythm",
            "loudness",
            "timbre",
            "regularity",
            "mood",
            "tonal",
            "dynamics",
            "genre",
            "era",
        ):
            assert key in result


class TestSingleSeedAPI:
    """Cover the single-seed `item_id=` API: parsing, options, and parity with `item_ids=[...]`."""

    def test_single_and_multi_seed_param_parse(self) -> None:
        """item_id='abc' produces same params as item_ids=['abc']."""
        single = _parse_similar_params(item_id="abc")
        multi = _parse_similar_params(item_ids=["abc"])
        assert single.item_ids == multi.item_ids
        assert single.limit == multi.limit
        assert single.depth == multi.depth

    def test_single_seed_with_limit_and_preset(self) -> None:
        """Single-seed call with extra kwargs works."""
        params = _parse_similar_params(item_id="abc", limit=10, preset="vibe")
        assert params.item_ids == ["abc"]
        assert params.limit == 10
        assert params.preset == "vibe"

    def test_single_seed_with_weight_overrides(self) -> None:
        """Weight kwargs pass through."""
        params = _parse_similar_params(item_id="abc", timbre_weight="0.5")
        assert params.weight_overrides["timbre_weight"] == "0.5"
