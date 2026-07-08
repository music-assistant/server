"""
Pure similarity functions — no MA dependencies.

Centroid blending, union merging, and MMR diversity re-ranking.
All functions operate on plain lists of floats and numpy arrays.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import NamedTuple

import numpy as np

from .vectors import build_dimension_weights, compute_weighted_distance_vec


class Candidate(NamedTuple):
    """A search-result candidate carrying its raw feature vector, distance, and generation index."""

    item_id: str
    provider: str
    features: list[float]
    distance: float
    generation: int


class ScoredCandidate(NamedTuple):
    """
    An ANN-search result without features/generation — the cheap variant of Candidate.

    Field order matches Candidate and the project's tracks.get(item_id, provider)
    convention, so the CLAP and 18-dim paths can share post-search helpers
    without slot-order hazards.
    """

    item_id: str
    provider: str
    distance: float


def combine_seeds_centroid(
    seeds: list[list[float]],
    weights: list[float] | None = None,
) -> list[float]:
    """
    Compute weighted average of seed signature vectors.

    :param seeds: List of signature vectors (all same dimensionality).
    :param weights: Per-seed weights. If None, equal weighting.
    :raises ValueError: If seeds is empty or weights length mismatches.
    """
    if not seeds:
        msg = "Cannot compute centroid: at least one seed is required"
        raise ValueError(msg)
    if weights is not None and len(weights) != len(seeds):
        msg = f"weights length ({len(weights)}) must match seeds length ({len(seeds)})"
        raise ValueError(msg)

    arr = np.array(seeds, dtype=np.float64)
    if weights is None:
        centroid = arr.mean(axis=0)
    else:
        w = np.array(weights, dtype=np.float64)
        w = w / w.sum()
        centroid = (arr * w[:, np.newaxis]).sum(axis=0)

    return [float(v) for v in centroid]


def merge_union_results(
    neighborhoods: list[list[tuple[str, float]]],
) -> list[tuple[str, float]]:
    """
    Merge per-seed ANN results, keeping the best distance per track.

    :param neighborhoods: List of result lists, each containing (item_id, distance) pairs.
    """
    if not neighborhoods:
        return []

    best: dict[str, float] = {}
    for neighborhood in neighborhoods:
        for item_id, dist in neighborhood:
            if item_id not in best or dist < best[item_id]:
                best[item_id] = dist

    merged = list(best.items())
    merged.sort(key=lambda x: x[1])
    return merged


def apply_mmr(
    candidates: list[tuple[str, list[float], float]],
    seed_vec: list[float],
    diversity: float,
    limit: int,
    weights: dict[str, float] | None = None,
) -> list[tuple[str, float]]:
    """
    Apply Maximal Marginal Relevance to re-rank candidates for diversity.

    :param candidates: List of (item_id, normalized_features, distance) tuples.
    :param seed_vec: The seed signature vector (normalized).
    :param diversity: MMR lambda, 0.0 = pure relevance, 1.0 = max diversity.
    :param limit: Maximum number of results to return.
    :param weights: Optional per-feature-group weights. When provided, MMR
        scores relevance and redundancy via compute_weighted_distance so
        preset selection shapes the picked set. When None, uses plain L2
        cosine similarity.
    """
    if not candidates:
        return []

    cand_vecs = {cid: np.array(vec, dtype=np.float64) for cid, vec, _d in candidates}
    seed_arr = np.array(seed_vec, dtype=np.float64)

    if weights is not None:
        # Build the per-dimension weight vector once; _similarity runs O(n²) times.
        dim_weights = build_dimension_weights(weights)

        def _similarity(a: np.ndarray, b: np.ndarray) -> float:
            d = compute_weighted_distance_vec(a, b, dim_weights)
            return 1.0 / (1.0 + d)

        relevance: dict[str, float] = {
            cid: _similarity(cand_vecs[cid], seed_arr) for cid, _vec, _d in candidates
        }
    else:
        seed_norm = float(np.linalg.norm(seed_arr))

        def _similarity(a: np.ndarray, b: np.ndarray) -> float:
            na, nb = float(np.linalg.norm(a)), float(np.linalg.norm(b))
            if na == 0 or nb == 0:
                return 0.0
            return float(np.dot(a, b) / (na * nb))

        relevance = {
            cid: (_similarity(cand_vecs[cid], seed_arr) if seed_norm > 0 else 0.0)
            for cid, _vec, _d in candidates
        }

    selected: list[tuple[str, float]] = []
    remaining: set[str] = {cid for cid, _, _ in candidates}
    dist_lookup = {cid: d for cid, _, d in candidates}
    # Input-order list for deterministic tie-breaking, set for O(1) membership.
    ordered_ids = [cid for cid, _, _ in candidates]

    for _ in range(min(limit, len(candidates))):
        best_id: str | None = None
        best_score = -float("inf")

        for cid in ordered_ids:
            if cid not in remaining:
                continue
            rel = relevance[cid]
            if not selected:
                redundancy = 0.0
            else:
                redundancy = max(_similarity(cand_vecs[cid], cand_vecs[sid]) for sid, _ in selected)
            score = (1.0 - diversity) * rel - diversity * redundancy
            if score > best_score:
                best_score = score
                best_id = cid

        if best_id is None:
            break
        remaining.discard(best_id)
        selected.append((best_id, dist_lookup[best_id]))

    return selected


def expand_recursive(
    initial_seeds: list[list[float]],
    searcher: Callable[
        [list[list[float]], set[str]],
        list[tuple[str, str, list[float], float]],
    ],
    depth: int,
    branch_factor: int,
) -> list[Candidate]:
    """
    Expand similarity search across multiple generations.

    :param initial_seeds: Seed signature vectors for generation 0.
    :param searcher: Callback that takes (seed_vectors, seen_ids) and returns
        list of (item_id, provider, features, distance) — generation index is
        added by this function.
    :param depth: Number of generations to run.
    :param branch_factor: How many top results from each generation become seeds.
    """
    all_results: list[Candidate] = []
    seen: set[str] = set()
    current_seeds = initial_seeds

    for gen in range(depth):
        gen_results = searcher(current_seeds, seen)
        gen_candidates: list[Candidate] = []
        for item_id, provider, features, dist in gen_results:
            if item_id not in seen:
                seen.add(item_id)
                cand = Candidate(item_id, provider, features, dist, gen)
                gen_candidates.append(cand)
                all_results.append(cand)

        if not gen_candidates or gen == depth - 1:
            break

        gen_candidates.sort(key=lambda c: c.distance)
        current_seeds = [c.features for c in gen_candidates[:branch_factor]]

    return all_results
