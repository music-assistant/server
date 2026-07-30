"""Dataclasses for the Sonic Similarity plugin."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class SimilarParams:
    """Validated parameters for one /similar request."""

    item_ids: list[str]
    limit: int
    depth: int
    branch_factor: int
    blend_mode: str  # "centroid" | "union"
    seed_weights: list[float] | None
    diversity: float
    preset: str
    candidates: int
    filter_genres: list[str] | None
    filter_providers: list[str] | None
    exclude_track_ids: list[str] | None
    exclude_artists: list[str] | None
    resolve: bool
    include_group_distances: bool
    # When set, uses the (item_id, provider) cache instead of the
    # item_id-only fallback — avoids wrong-vector picks under id collisions.
    seed_provider: str | None = None
    weight_overrides: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _SearchContext:
    """Per-request snapshot of the inputs needed by every search-pipeline phase."""

    params: SimilarParams
    weights: dict[str, float]
    seed_sigs: list[list[float]]
    valid_seed_ids: list[str]
    corpus_means: list[float]
    corpus_stds: list[float]
    orig_normalized: list[float]
