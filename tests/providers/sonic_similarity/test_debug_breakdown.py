"""Tests for build_debug_breakdown — per-track diagnostic via include_group_distances=True."""

from __future__ import annotations

from music_assistant.providers.sonic_similarity.vectors import (
    build_debug_breakdown,
    compute_weighted_distance,
)


def _balanced_weights() -> dict[str, float]:
    return {
        "rhythm": 1.0,
        "loudness": 1.0,
        "timbre": 1.0,
        "regularity": 1.0,
        "mood": 1.0,
        "tonal": 1.0,
        "dynamics": 1.0,
    }


def test_breakdown_keys_present() -> None:
    """Result has weighted_distance, metadata_bonus, group_distances."""
    seed = [0.1] * 18
    cand = [0.2] * 18
    out = build_debug_breakdown(seed, cand, _balanced_weights(), displayed_dist=0.42)
    assert "weighted_distance" in out
    assert "metadata_bonus" in out
    assert "group_distances" in out


def test_weighted_distance_matches_compute_weighted_distance() -> None:
    """Reported weighted_distance equals compute_weighted_distance directly."""
    seed = [0.1] * 18
    cand = [0.5] * 18
    weights = _balanced_weights()
    out = build_debug_breakdown(seed, cand, weights, displayed_dist=999.0)
    expected = compute_weighted_distance(seed, cand, weights)
    assert out["weighted_distance"] == round(expected, 4)


def test_metadata_bonus_is_displayed_minus_weighted() -> None:
    """metadata_bonus = displayed_dist - weighted_distance (the rerank delta)."""
    seed = [0.1] * 18
    cand = [0.5] * 18
    weights = _balanced_weights()
    weighted = compute_weighted_distance(seed, cand, weights)
    out = build_debug_breakdown(seed, cand, weights, displayed_dist=weighted - 0.7)
    assert out["metadata_bonus"] == round(-0.7, 4)


def test_group_distances_has_all_seven_groups() -> None:
    """All FEATURE_GROUPS keys are present in group_distances."""
    seed = [0.1] * 18
    cand = [0.5] * 18
    out = build_debug_breakdown(seed, cand, _balanced_weights(), displayed_dist=0.0)
    expected_keys = {"rhythm", "loudness", "timbre", "regularity", "mood", "tonal", "dynamics"}
    assert set(out["group_distances"].keys()) == expected_keys
