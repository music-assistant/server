"""Pure-function helpers for the Sonic Similarity plugin."""

from __future__ import annotations

import re
from typing import Any

import numpy as np

from music_assistant.providers.sonic_similarity.clap_index import CLAP_EMBEDDING_DIM
from music_assistant.providers.sonic_similarity.constants import SIMILARITY_PRESETS
from music_assistant.providers.sonic_similarity.models import SimilarParams
from music_assistant.providers.sonic_similarity.similarity import ScoredCandidate

_MUSIC_WORD = re.compile(r"\bmusic\b", re.IGNORECASE)


def format_text_query(query: str) -> str:
    """
    Frame a bare query as ``<query> music`` for the CLAP text encoder.

    Skipped when the query already contains the word "music" (case-insensitive);
    an empty or whitespace-only query returns "".

    :param query: Raw user query.
    """
    # CLAP was trained on audio captions, so the " music" suffix matches that form.
    cleaned = query.strip()
    if not cleaned or _MUSIC_WORD.search(cleaned):
        return cleaned
    return f"{cleaned} music"


def _parse_clap_embedding(raw: Any) -> np.ndarray | None:
    """Coerce a stored embedding (list/tuple) into a 1024-dim float32 array, or None."""
    if raw is None:
        return None
    try:
        arr = np.asarray(raw, dtype=np.float32).reshape(-1)
    except TypeError, ValueError:
        return None
    if arr.shape != (CLAP_EMBEDDING_DIM,):
        return None
    return arr


def _parse_weights(params: dict[str, Any]) -> dict[str, float]:
    """Parse similarity weights from API parameters."""
    preset_name = str(params.get("preset", "balanced"))
    preset = SIMILARITY_PRESETS.get(preset_name, SIMILARITY_PRESETS["balanced"])
    result = dict(preset)

    def _clamp(val: str, fallback: float) -> float:
        try:
            return max(0.0, min(1.0, float(val)))
        except ValueError, TypeError:
            return fallback

    for group, default in result.items():
        key = f"{group}_weight"
        if key in params:
            result[group] = _clamp(params[key], default)

    return result


def _parse_similar_params(  # noqa: PLR0913
    item_id: str | None = None,
    item_ids: list[str] | None = None,
    limit: int = 25,
    depth: int = 1,
    branch_factor: int = 5,
    blend_mode: str = "centroid",
    seed_weights: list[float] | None = None,
    diversity: float = 0.0,
    preset: str = "balanced",
    candidates: int = 200,
    filter_genres: list[str] | None = None,
    filter_providers: list[str] | None = None,
    exclude_track_ids: list[str] | None = None,
    exclude_artists: list[str] | None = None,
    resolve: bool = False,
    include_group_distances: bool = False,
    seed_provider: str | None = None,
    **kwargs: Any,
) -> SimilarParams:
    """Validate and normalize parameters for the similar endpoint."""
    if item_ids is None:
        if item_id is None:
            msg = "Either item_id or item_ids must be provided"
            raise ValueError(msg)
        item_ids = [item_id]

    limit = max(1, min(100, limit))
    depth = max(1, min(5, depth))
    diversity = max(0.0, min(1.0, diversity))

    if blend_mode not in ("centroid", "union"):
        blend_mode = "centroid"

    if seed_weights is not None and len(seed_weights) != len(item_ids):
        msg = f"seed_weights length ({len(seed_weights)}) must match item_ids ({len(item_ids)})"
        raise ValueError(msg)

    has_filters = any(
        x is not None for x in (filter_genres, filter_providers, exclude_track_ids, exclude_artists)
    )
    if has_filters:
        candidates = candidates * 2

    return SimilarParams(
        item_ids=item_ids,
        limit=limit,
        depth=depth,
        branch_factor=branch_factor,
        blend_mode=blend_mode,
        seed_weights=seed_weights,
        diversity=diversity,
        preset=preset,
        candidates=candidates,
        filter_genres=filter_genres,
        filter_providers=filter_providers,
        exclude_track_ids=exclude_track_ids,
        exclude_artists=exclude_artists,
        resolve=resolve,
        include_group_distances=include_group_distances,
        seed_provider=seed_provider,
        weight_overrides=kwargs,
    )


def apply_filters(
    candidates: list[ScoredCandidate],
    seed_ids: set[str],
    exclude_track_ids: set[str] | None,
    filter_providers: set[str] | None,
) -> list[ScoredCandidate]:
    """
    Apply cheap post-ANN filters to candidate list.

    :param candidates: ScoredCandidate results from the ANN search.
    :param seed_ids: Seed track IDs to exclude.
    :param exclude_track_ids: Additional track IDs to exclude.
    :param filter_providers: If set, only keep candidates from these providers.
    """
    result: list[ScoredCandidate] = []
    exclude = seed_ids | (exclude_track_ids or set())

    for cand in candidates:
        if cand.item_id in exclude:
            continue
        if filter_providers is not None and cand.provider not in filter_providers:
            continue
        result.append(cand)

    return result
