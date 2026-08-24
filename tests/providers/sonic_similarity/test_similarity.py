"""Unit tests for sonic similarity pure functions."""

import pytest

from music_assistant.providers.sonic_similarity.similarity import (
    apply_mmr,
    combine_seeds_centroid,
    expand_recursive,
    merge_union_results,
)


class TestCombineSeedsCentroid:
    """Tests for centroid blending of seed signatures."""

    def test_single_seed_returns_itself(self) -> None:
        """A single seed with no weights returns the same vector."""
        seed = [1.0, 2.0, 3.0]
        result = combine_seeds_centroid([seed])
        assert result == pytest.approx(seed)

    def test_two_seeds_equal_weight(self) -> None:
        """Two seeds with no weights returns their average."""
        a = [0.0, 4.0, 6.0]
        b = [2.0, 0.0, 2.0]
        result = combine_seeds_centroid([a, b])
        assert result == pytest.approx([1.0, 2.0, 4.0])

    def test_two_seeds_weighted(self) -> None:
        """Weighted centroid biases toward the heavier seed."""
        a = [0.0, 0.0, 0.0]
        b = [10.0, 10.0, 10.0]
        result = combine_seeds_centroid([a, b], weights=[1.0, 3.0])
        assert result == pytest.approx([7.5, 7.5, 7.5])

    def test_weights_normalized(self) -> None:
        """Weights [2.0, 2.0] produces same result as [0.5, 0.5]."""
        a = [0.0, 0.0]
        b = [10.0, 10.0]
        r1 = combine_seeds_centroid([a, b], weights=[2.0, 2.0])
        r2 = combine_seeds_centroid([a, b], weights=[0.5, 0.5])
        assert r1 == pytest.approx(r2)

    def test_empty_seeds_raises(self) -> None:
        """Empty seed list raises ValueError."""
        with pytest.raises(ValueError, match="at least one seed"):
            combine_seeds_centroid([])

    def test_mismatched_weights_raises(self) -> None:
        """Weights length must match seeds length."""
        with pytest.raises(ValueError, match="weights length"):
            combine_seeds_centroid([[1.0, 2.0]], weights=[1.0, 2.0])


class TestMergeUnionResults:
    """Tests for union-mode neighborhood merging."""

    def test_single_neighborhood(self) -> None:
        """Single neighborhood passes through unchanged."""
        results = [("track_a", 0.1), ("track_b", 0.5)]
        merged = merge_union_results([results])
        assert merged == [("track_a", 0.1), ("track_b", 0.5)]

    def test_two_neighborhoods_dedup_keeps_best(self) -> None:
        """Duplicate tracks across neighborhoods keep lowest distance."""
        n1 = [("track_a", 0.3), ("track_b", 0.5)]
        n2 = [("track_a", 0.1), ("track_c", 0.4)]
        merged = merge_union_results([n1, n2])
        merged_dict = dict(merged)
        assert merged_dict["track_a"] == pytest.approx(0.1)
        assert "track_b" in merged_dict
        assert "track_c" in merged_dict

    def test_result_sorted_by_distance(self) -> None:
        """Merged results are sorted by distance ascending."""
        n1 = [("a", 0.5)]
        n2 = [("b", 0.1)]
        merged = merge_union_results([n1, n2])
        assert merged[0][0] == "b"
        assert merged[1][0] == "a"

    def test_empty_neighborhoods(self) -> None:
        """Empty input returns empty list."""
        assert merge_union_results([]) == []
        assert merge_union_results([[]]) == []


class TestApplyMMR:
    """Tests for Maximal Marginal Relevance diversity re-ranking."""

    def test_diversity_zero_preserves_order(self) -> None:
        """With diversity=0, result order matches pure relevance."""
        candidates = [
            ("a", [1.0, 0.0], 0.1),
            ("b", [0.9, 0.1], 0.2),
            ("c", [0.5, 0.5], 0.5),
        ]
        result = apply_mmr(candidates, [1.0, 0.0], diversity=0.0, limit=3)
        assert [r[0] for r in result] == ["a", "b", "c"]

    def test_diversity_one_spreads_results(self) -> None:
        """With diversity=1.0, picks should maximize spread."""
        candidates = [
            ("close_1", [1.0, 0.0], 0.1),
            ("close_2", [0.99, 0.01], 0.11),
            ("far", [0.0, 1.0], 0.9),
        ]
        result = apply_mmr(candidates, [1.0, 0.0], diversity=1.0, limit=3)
        assert result[0][0] == "close_1"
        assert result[1][0] == "far"

    def test_limit_respected(self) -> None:
        """Only limit items are returned."""
        candidates = [
            ("a", [1.0], 0.1),
            ("b", [0.5], 0.2),
            ("c", [0.0], 0.3),
        ]
        result = apply_mmr(candidates, [1.0], diversity=0.0, limit=2)
        assert len(result) == 2

    def test_empty_candidates(self) -> None:
        """Empty candidates returns empty."""
        assert apply_mmr([], [1.0], diversity=0.5, limit=10) == []


class TestApplyMMRWeights:
    """Tests for the preset-weight-aware path of apply_mmr (weights=... shapes the picked set)."""

    @staticmethod
    def _zero18() -> list[float]:
        return [0.0] * 18

    @staticmethod
    def _rhythm_only_weights() -> dict[str, float]:
        return {
            "rhythm": 1.0,
            "loudness": 0.0,
            "timbre": 0.0,
            "regularity": 0.0,
            "mood": 0.0,
            "tonal": 0.0,
            "dynamics": 0.0,
        }

    @staticmethod
    def _mood_only_weights() -> dict[str, float]:
        return {
            "rhythm": 0.0,
            "loudness": 0.0,
            "timbre": 0.0,
            "regularity": 0.0,
            "mood": 1.0,
            "tonal": 0.0,
            "dynamics": 0.0,
        }

    def test_weights_change_top_pick_diversity_zero(self) -> None:
        """Switching weight emphasis flips which candidate ranks first."""
        seed = self._zero18()
        # FEATURE_GROUPS: rhythm=[0,3), mood=[9,13)
        # rhythm_match: zero on rhythm, ones elsewhere -> rhythm dist 0, mood dist 1
        rhythm_match = [0, 0, 0] + [1] * 6 + [1] * 4 + [1] * 5
        # mood_match: zero on mood, ones elsewhere -> rhythm dist 1, mood dist 0
        mood_match = [1] * 9 + [0, 0, 0, 0] + [1] * 5
        candidates = [
            ("rhythm-cand", [float(v) for v in rhythm_match], 0.5),
            ("mood-cand", [float(v) for v in mood_match], 0.5),
        ]

        r1 = apply_mmr(
            candidates, seed, diversity=0.0, limit=2, weights=self._rhythm_only_weights()
        )
        r2 = apply_mmr(candidates, seed, diversity=0.0, limit=2, weights=self._mood_only_weights())

        assert r1[0][0] == "rhythm-cand"
        assert r2[0][0] == "mood-cand"

    def test_weights_none_matches_default(self) -> None:
        """weights=None and an explicit None argument produce identical results."""
        candidates = [
            ("a", [1.0, 0.0], 0.1),
            ("b", [0.9, 0.1], 0.2),
            ("c", [0.5, 0.5], 0.5),
        ]
        default = apply_mmr(candidates, [1.0, 0.0], diversity=0.0, limit=3)
        explicit_none = apply_mmr(candidates, [1.0, 0.0], diversity=0.0, limit=3, weights=None)
        assert default == explicit_none

    def test_weights_affect_redundancy_when_diversity_positive(self) -> None:
        """
        With diversity > 0, weights also affect which 2nd pick MMR diversifies to.

        Setup: rhythm_match_a and rhythm_match_b are both rhythm-close to seed
        but identical to each other on rhythm (would look 'redundant' under
        rhythm-only weights). mood_match is rhythm-far but mood-close.
        Under rhythm-only weights with high diversity, MMR should prefer to
        avoid picking two rhythm-similar candidates, so the second pick is
        not the duplicate.
        """
        seed = self._zero18()
        rhythm_a = [0, 0, 0] + [1] * 15  # rhythm dist 0
        rhythm_b = [0, 0, 0] + [1] * 15  # rhythm dist 0 (duplicate of a)
        mood_match = [1] * 9 + [0, 0, 0, 0] + [1] * 5  # mood dist 0
        candidates = [
            ("rhythm-a", [float(v) for v in rhythm_a], 0.0),
            ("rhythm-b", [float(v) for v in rhythm_b], 0.0),
            ("mood-cand", [float(v) for v in mood_match], 1.0),
        ]
        result = apply_mmr(
            candidates,
            seed,
            diversity=0.9,
            limit=2,
            weights=self._rhythm_only_weights(),
        )
        picked = [r[0] for r in result]
        # First pick: rhythm-a (closest to seed under rhythm-only weights)
        assert picked[0] in ("rhythm-a", "rhythm-b")
        # Second pick: not the duplicate -- but under rhythm-only weights,
        # mood-cand has the same rhythm distance as rhythm-a, so it could be
        # picked. The duplicate should NOT be picked because it has zero
        # rhythm-distance from rhythm-a (max redundancy).
        assert picked[1] != ("rhythm-b" if picked[0] == "rhythm-a" else "rhythm-a")


class TestExpandRecursive:
    """Tests for recursive depth expansion."""

    def test_depth_1_returns_single_generation(self) -> None:
        """Depth=1 runs the searcher once, all results are generation 0."""

        def searcher(
            seeds: list[list[float]],  # noqa: ARG001
            seen: set[str],  # noqa: ARG001
        ) -> list[tuple[str, str, list[float], float]]:
            return [
                ("a", "prov", [1.0, 0.0], 0.1),
                ("b", "prov", [0.9, 0.1], 0.2),
            ]

        results = expand_recursive(
            initial_seeds=[[1.0, 0.0]], searcher=searcher, depth=1, branch_factor=5
        )
        assert len(results) == 2
        assert all(gen == 0 for _, _, _, _, gen in results)

    def test_depth_2_expands(self) -> None:
        """Depth=2 uses top branch_factor results from gen 0 as seeds for gen 1."""
        call_count = 0

        def searcher(
            seeds: list[list[float]],  # noqa: ARG001
            seen: set[str],  # noqa: ARG001
        ) -> list[tuple[str, str, list[float], float]]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return [
                    ("a", "p", [1.0, 0.0], 0.1),
                    ("b", "p", [0.8, 0.2], 0.2),
                ]
            return [("c", "p", [0.5, 0.5], 0.4)]

        results = expand_recursive(
            initial_seeds=[[1.0, 0.0]], searcher=searcher, depth=2, branch_factor=2
        )
        assert call_count == 2
        ids = [r[0] for r in results]
        assert "a" in ids
        assert "b" in ids
        assert "c" in ids
        gen_map = {r[0]: r[4] for r in results}
        assert gen_map["a"] == 0
        assert gen_map["b"] == 0
        assert gen_map["c"] == 1

    def test_deduplication_across_generations(self) -> None:
        """A track found in gen 0 is not returned again in gen 1."""
        call_count = 0

        def searcher(
            seeds: list[list[float]],  # noqa: ARG001
            seen: set[str],  # noqa: ARG001
        ) -> list[tuple[str, str, list[float], float]]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return [("a", "p", [1.0], 0.1)]
            return [("b", "p", [0.5], 0.3)]

        results = expand_recursive(
            initial_seeds=[[1.0]], searcher=searcher, depth=2, branch_factor=1
        )
        ids = [r[0] for r in results]
        assert ids.count("a") == 1
        assert "b" in ids
