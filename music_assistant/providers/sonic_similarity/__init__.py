"""Sonic Similarity plugin: weighted-Euclidean similarity over audio_analysis signatures.

CLAP-embedding similarity (1024-dim cosine) lives in the separate sonic_clap plugin.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
from music_assistant_models.errors import MusicAssistantError
from music_assistant_models.media_items import Album

from music_assistant.models.plugin import PluginProvider
from music_assistant.providers.sonic_similarity.similarity import (
    Candidate,
    apply_mmr,
    combine_seeds_centroid,
    expand_recursive,
    merge_union_results,
)
from music_assistant.providers.sonic_similarity.vectors import (
    VECTOR_DIMENSIONS,
    assemble_vector,
    build_debug_breakdown,
    compute_corpus_stats,
    compute_weighted_distance,
    normalize_features,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry, ConfigValueType, ProviderConfig
    from music_assistant_models.media_items import Track
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

USEARCH_INDEX_FILENAME_TPL = "sonic_signatures_{domain}_v{version}.usearch"
USEARCH_INDEX_FILENAME_GLOB = "sonic_signatures_{domain}_v*.usearch"
CONF_AA_PROVIDER = "aa_provider_domain"

# Genre/year reranking bonus scale. Raw genre Jaccard + year-proximity terms reach
# magnitudes ~10-20x the audio-distance dynamic range; without scaling they dominate
# ranking and make preset weight changes invisible. 0.1 caps combined bonus at ~0.2,
# comparable to the audio-distance range — categorical context nudges, not overrides.
METADATA_BONUS_SCALE: float = 0.1

# Audio-vector group weights match FEATURE_GROUPS in vectors.py. `genre` and `era`
# are metadata-rerank-only knobs that don't enter the vector distance.
SIMILARITY_PRESETS: dict[str, dict[str, float]] = {
    "balanced": {
        "rhythm": 1.0,
        "loudness": 1.0,
        "timbre": 1.0,
        "regularity": 1.0,
        "mood": 1.0,
        "tonal": 1.0,
        "dynamics": 1.0,
        "genre": 1.0,
        "era": 1.0,
    },
    "vibe": {
        "rhythm": 0.3,
        "loudness": 0.5,
        "timbre": 1.0,
        "regularity": 0.3,
        "mood": 1.0,
        "tonal": 0.5,
        "dynamics": 0.8,
        "genre": 0.5,
        "era": 0.5,
    },
    "party": {
        "rhythm": 1.0,
        "loudness": 0.5,
        "timbre": 0.3,
        "regularity": 0.8,
        "mood": 0.5,
        "tonal": 0.2,
        "dynamics": 0.3,
        "genre": 0.3,
        "era": 0.3,
    },
    "genre_era": {
        "rhythm": 0.5,
        "loudness": 0.5,
        "timbre": 0.5,
        "regularity": 0.5,
        "mood": 0.8,
        "tonal": 0.8,
        "dynamics": 0.5,
        "genre": 1.0,
        "era": 1.0,
    },
    "discover": {
        "rhythm": 0.5,
        "loudness": 0.7,
        "timbre": 1.0,
        "regularity": 0.5,
        "mood": 0.8,
        "tonal": 0.8,
        "dynamics": 0.7,
        "genre": 0.3,
        "era": 0.3,
    },
}


def _parse_weights(params: dict[str, Any]) -> dict[str, float]:
    """Parse similarity weights from API parameters."""
    preset_name = str(params.get("preset", "balanced"))
    preset = SIMILARITY_PRESETS.get(preset_name, SIMILARITY_PRESETS["balanced"])
    result = dict(preset)

    def _clamp(val: str, fallback: float) -> float:
        try:
            return max(0.0, min(1.0, float(val)))
        except (ValueError, TypeError):
            return fallback

    for group, default in result.items():
        key = f"{group}_weight"
        if key in params:
            result[group] = _clamp(params[key], default)

    return result


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
    candidates: int = 50,
    filter_genres: list[str] | None = None,
    filter_providers: list[str] | None = None,
    exclude_track_ids: list[str] | None = None,
    exclude_artists: list[str] | None = None,
    resolve: bool = False,
    include_group_distances: bool = False,
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
        weight_overrides=kwargs,
    )


def apply_filters(
    candidates: list[tuple[str, str, float]],
    seed_ids: set[str],
    exclude_track_ids: set[str] | None,
    filter_providers: set[str] | None,
) -> list[tuple[str, str, float]]:
    """Apply cheap post-ANN filters to candidate list.

    :param candidates: List of (item_id, provider, distance) tuples.
    :param seed_ids: Seed track IDs to exclude.
    :param exclude_track_ids: Additional track IDs to exclude.
    :param filter_providers: If set, only keep candidates from these providers.
    """
    result: list[tuple[str, str, float]] = []
    exclude = seed_ids | (exclude_track_ids or set())

    for item_id, provider, dist in candidates:
        if item_id in exclude:
            continue
        if filter_providers is not None and provider not in filter_providers:
            continue
        result.append((item_id, provider, dist))

    return result


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider instance with given configuration."""
    return SonicSimilarityPlugin(mass, manifest, config)


async def get_config_entries(
    mass: MusicAssistant,  # noqa: ARG001
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,  # noqa: ARG001
    values: dict[str, ConfigValueType] | None = None,  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider.

    :param mass: MusicAssistant instance.
    :param instance_id: id of an existing provider instance (None if new instance setup).
    :param action: action key called from config entries UI.
    :param values: the (intermediate) raw values for config entries sent with the action.
    """
    from music_assistant_models.config_entries import ConfigEntry  # noqa: PLC0415
    from music_assistant_models.enums import ConfigEntryType  # noqa: PLC0415

    return (
        ConfigEntry(
            key=CONF_AA_PROVIDER,
            type=ConfigEntryType.STRING,
            default_value="sonic_analysis",
            label="Analysis Provider",
            description="Which audio analysis provider's data to use for similarity vectors. "
            "Default: sonic_analysis (librosa + CLAP, on-device).",
        ),
    )


class SonicSimilarityPlugin(PluginProvider):
    """Plugin that provides similarity search over sonic analysis signatures."""

    def __init__(
        self,
        mass: MusicAssistant,
        manifest: ProviderManifest,
        config: ProviderConfig,
    ) -> None:
        """Initialize the Sonic Similarity plugin."""
        super().__init__(mass, manifest, config)
        self._aa_domain: str = "sonic_analysis"
        self._label_map: dict[int, tuple[str, str]] = {}
        # _signature_cache is keyed on (item_id, provider) so a track that
        # exists in two providers under the same item_id doesn't overwrite
        # itself. _signatures_by_id is the fallback for the seed-lookup path,
        # where the API caller only supplies item_id; matches the previous
        # last-write-wins behavior for cross-provider collisions.
        self._signature_cache: dict[tuple[str, str], list[float]] = {}
        self._signatures_by_id: dict[str, list[float]] = {}
        self._provider_by_item_id: dict[str, str] = {}
        self.corpus_means: list[float] | None = None
        self.corpus_stds: list[float] | None = None
        self._search_index: Any = None
        self._unregister_handles: list[Callable[[], None]] = []
        self._rebuild_lock = asyncio.Lock()

    async def loaded_in_mass(self) -> None:
        """Register similarity API commands and build the search index."""
        self._unregister_handles.append(
            self.mass.register_api_command("sonic_similarity/similar", self._handle_similar)
        )
        self._unregister_handles.append(
            self.mass.register_api_command("sonic_similarity/status", self._handle_status)
        )
        self._unregister_handles.append(
            self.mass.register_api_command(
                "sonic_similarity/rebuild_index", self._handle_rebuild_index
            )
        )
        self._aa_domain = str(self.config.get_value(CONF_AA_PROVIDER) or "sonic_analysis")
        self.logger.info(
            "Sonic Similarity loaded (aa_provider=%s), rebuilding search index...",
            self._aa_domain,
        )
        await self._rebuild_search_index()
        self.logger.info(
            "Search index ready: %d signatures cached, corpus_stats=%s",
            len(self._signature_cache),
            self.corpus_means is not None,
        )

    async def unload(self, is_removed: bool = False) -> None:
        """Unregister API commands; delete on-disk indexes when the provider is uninstalled."""
        for unregister in self._unregister_handles:
            unregister()
        self._unregister_handles.clear()
        if is_removed:
            self._search_index = None
            await asyncio.to_thread(self._delete_all_index_files)
        await super().unload(is_removed)

    def _delete_all_index_files(self) -> None:
        """Best-effort removal of every versioned index file for the active domain."""
        for path in self._existing_index_files():
            try:
                path.unlink()
            except OSError as err:
                self.logger.debug("Could not unlink %s during uninstall: %s", path, err)

    async def _handle_similar(  # noqa: PLR0913
        self,
        item_id: str | None = None,
        item_ids: list[str] | None = None,
        limit: int = 25,
        depth: int = 1,
        branch_factor: int = 5,
        blend_mode: str = "centroid",
        seed_weights: list[float] | None = None,
        diversity: float = 0.0,
        preset: str = "balanced",
        candidates: int = 50,
        filter_genres: list[str] | None = None,
        filter_providers: list[str] | None = None,
        exclude_track_ids: list[str] | None = None,
        exclude_artists: list[str] | None = None,
        resolve: bool = False,
        include_group_distances: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Find tracks similar to the given track(s)."""
        params = _parse_similar_params(
            item_id=item_id,
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
            **kwargs,
        )
        weights = _parse_weights({**params.weight_overrides, "preset": params.preset})

        seed_sigs, valid_seed_ids = self._lookup_seed_signatures(params.item_ids)
        if not seed_sigs or self.corpus_means is None or self.corpus_stds is None:
            return {
                "analyzed": False,
                "seed_track_ids": params.item_ids,
                "blend_mode": params.blend_mode,
                "depth": params.depth,
                "items": [],
            }

        # Centroid of seed_sigs is invariant across search/MMR/debug paths;
        # compute once and pass through the context.
        orig_normalized = normalize_features(
            combine_seeds_centroid(seed_sigs), self.corpus_means, self.corpus_stds
        )
        ctx = _SearchContext(
            params=params,
            weights=weights,
            seed_sigs=seed_sigs,
            valid_seed_ids=valid_seed_ids,
            corpus_means=self.corpus_means,
            corpus_stds=self.corpus_stds,
            orig_normalized=orig_normalized,
        )

        raw_results = self._run_ann_search(ctx)
        raw_results = await self._post_process_candidates(ctx, raw_results)
        final_items = self._diversify_and_limit(ctx, raw_results)
        debug_breakdown_map = self._build_debug_breakdowns(ctx, final_items)
        items = await self._format_response_items(ctx, final_items, debug_breakdown_map)

        return {
            "analyzed": True,
            "seed_track_ids": valid_seed_ids,
            "blend_mode": params.blend_mode,
            "depth": params.depth,
            "items": items,
        }

    def _lookup_seed_signatures(self, item_ids: list[str]) -> tuple[list[list[float]], list[str]]:
        """Look up signatures by item_id; warn on misses; return (sigs, valid_ids) in input order."""
        seed_sigs: list[list[float]] = []
        valid_seed_ids: list[str] = []
        for sid in item_ids:
            sig = self._signatures_by_id.get(sid)
            if sig is not None:
                seed_sigs.append(sig)
                valid_seed_ids.append(sid)
            else:
                self.logger.warning("Seed %s not in signature cache, skipping", sid)
        return seed_sigs, valid_seed_ids

    def _run_ann_search(self, ctx: _SearchContext) -> list[Candidate]:
        """Run the recursive ANN search for `params.depth` generations and return all hits."""

        def _search_generation(
            seeds: list[list[float]],
            seen: set[str],
        ) -> list[tuple[str, str, list[float], float]]:
            # `_label_map[lbl]` already returns (item_id, provider); cache the
            # provider per cand_id so the raw_tuples build below stays O(1) per
            # candidate.
            id_to_prov: dict[str, str] = {}
            if ctx.params.blend_mode == "union":
                all_neighborhoods: list[list[tuple[str, float]]] = []
                for seed_vec in seeds:
                    normalized = normalize_features(seed_vec, ctx.corpus_means, ctx.corpus_stds)
                    raw = self._query_index(normalized, ctx.params.candidates)
                    neighborhood: list[tuple[str, float]] = []
                    for lbl, cos_dist in raw:
                        if lbl not in self._label_map:
                            continue
                        cand_id, cand_provider = self._label_map[lbl]
                        id_to_prov[cand_id] = cand_provider
                        if cand_id not in seen:
                            neighborhood.append((cand_id, cos_dist))
                    all_neighborhoods.append(neighborhood)
                candidate_ids = merge_union_results(all_neighborhoods)
            else:
                centroid = combine_seeds_centroid(seeds, ctx.params.seed_weights)
                normalized = normalize_features(centroid, ctx.corpus_means, ctx.corpus_stds)
                raw = self._query_index(normalized, ctx.params.candidates)
                candidate_ids = []
                for lbl, cos_dist in raw:
                    if lbl not in self._label_map:
                        continue
                    cand_id, cand_provider = self._label_map[lbl]
                    id_to_prov[cand_id] = cand_provider
                    if cand_id not in seen:
                        candidate_ids.append((cand_id, cos_dist))

            raw_tuples: list[tuple[str, str, float]] = [
                (cand_id, id_to_prov.get(cand_id, "library"), cos_dist)
                for cand_id, cos_dist in candidate_ids
            ]

            seed_id_set = set(ctx.valid_seed_ids)
            exclude_set = (
                set(ctx.params.exclude_track_ids) if ctx.params.exclude_track_ids else None
            )
            filter_prov_set = (
                set(ctx.params.filter_providers) if ctx.params.filter_providers else None
            )
            filtered = apply_filters(raw_tuples, seed_id_set | seen, exclude_set, filter_prov_set)

            results: list[tuple[str, str, list[float], float]] = []
            for cand_id, cand_provider, _cos_dist in filtered:
                cand_features = self._signature_cache.get((cand_id, cand_provider))
                if cand_features is None:
                    continue
                cand_normalized = normalize_features(
                    cand_features, ctx.corpus_means, ctx.corpus_stds
                )
                dist = compute_weighted_distance(ctx.orig_normalized, cand_normalized, ctx.weights)
                results.append((cand_id, cand_provider, cand_features, dist))

            results.sort(key=lambda x: x[3])
            return results

        return expand_recursive(
            initial_seeds=ctx.seed_sigs,
            searcher=_search_generation,
            depth=ctx.params.depth,
            branch_factor=ctx.params.branch_factor,
        )

    async def _post_process_candidates(
        self, ctx: _SearchContext, candidates: list[Candidate]
    ) -> list[Candidate]:
        """Apply genre/artist filters and metadata reranking when configured."""
        if ctx.params.filter_genres or ctx.params.exclude_artists:
            candidates = await self._apply_metadata_filters(
                candidates,
                filter_genres=ctx.params.filter_genres,
                exclude_artists=ctx.params.exclude_artists,
            )
        if ctx.weights.get("genre", 0.0) > 0 or ctx.weights.get("era", 0.0) > 0:
            candidates = await self._apply_metadata_reranking(
                ctx.valid_seed_ids, candidates, ctx.weights
            )
        return candidates

    def _diversify_and_limit(
        self, ctx: _SearchContext, candidates: list[Candidate]
    ) -> list[tuple[str, str, float, int]]:
        """Apply MMR diversity OR distance sort, then trim to `limit`."""
        if ctx.params.diversity > 0:
            mmr_candidates = [
                (
                    c.item_id,
                    normalize_features(c.features, ctx.corpus_means, ctx.corpus_stds),
                    c.distance,
                )
                for c in candidates
            ]
            mmr_result = apply_mmr(
                mmr_candidates,
                ctx.orig_normalized,
                ctx.params.diversity,
                ctx.params.limit,
                weights=ctx.weights,
            )
            result_lookup = {c.item_id: c for c in candidates}
            final_items: list[tuple[str, str, float, int]] = [
                (cid, result_lookup[cid].provider, dist, result_lookup[cid].generation)
                for cid, dist in mmr_result
            ]
        else:
            candidates.sort(key=lambda c: c.distance)
            final_items = [(c.item_id, c.provider, c.distance, c.generation) for c in candidates]
        return final_items[: ctx.params.limit]

    def _build_debug_breakdowns(
        self,
        ctx: _SearchContext,
        final_items: list[tuple[str, str, float, int]],
    ) -> dict[str, dict[str, Any]]:
        """Build the per-track debug breakdown when include_group_distances is set."""
        if not ctx.params.include_group_distances:
            return {}
        breakdown: dict[str, dict[str, Any]] = {}
        for cid, prov, displayed_dist, _gen in final_items:
            cand_features = self._signature_cache.get((cid, prov))
            if cand_features is None:
                continue
            cand_normalized = normalize_features(cand_features, ctx.corpus_means, ctx.corpus_stds)
            breakdown[cid] = build_debug_breakdown(
                ctx.orig_normalized, cand_normalized, ctx.weights, displayed_dist
            )
        return breakdown

    async def _format_response_items(
        self,
        ctx: _SearchContext,
        final_items: list[tuple[str, str, float, int]],
        debug_breakdown_map: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Format final_items into the response shape, optionally resolving track metadata."""
        if ctx.params.resolve:
            return await self._resolve_results(
                final_items,
                debug_breakdown_map if ctx.params.include_group_distances else None,
            )
        items: list[dict[str, Any]] = []
        for cid, prov, dist, gen in final_items:
            entry: dict[str, Any] = {
                "item_id": cid,
                "provider": prov,
                "distance": round(dist, 4),
                "generation": gen,
            }
            if ctx.params.include_group_distances and cid in debug_breakdown_map:
                entry.update(debug_breakdown_map[cid])
            items.append(entry)
        return items

    async def _handle_status(self) -> dict[str, Any]:
        """Return current analysis status."""
        index_size = len(self._search_index) if self._search_index is not None else 0
        return {
            "index_size": index_size,
            "has_corpus_stats": self.corpus_means is not None,
            "cached_signatures": len(self._signature_cache),
            "aa_provider_domain": self._aa_domain,
        }

    async def _handle_rebuild_index(self) -> dict[str, Any]:
        """Rebuild the USearch index from stored analysis data."""
        await self._rebuild_search_index()
        index_size = len(self._search_index) if self._search_index is not None else 0
        return {"status": "rebuilt", "index_size": index_size}

    def _existing_index_files(self) -> list[Path]:
        """List on-disk index files for the active aa_domain, oldest first."""
        storage = Path(self.mass.storage_path)
        pattern = USEARCH_INDEX_FILENAME_GLOB.format(domain=self._aa_domain)
        return sorted(storage.glob(pattern), key=lambda p: p.stat().st_mtime)

    @staticmethod
    def _make_empty_index() -> Any:
        """Return a fresh, empty USearch HNSW index sized for our 18-dim cosine space."""
        from usearch.index import (  # type: ignore[attr-defined]  # noqa: PLC0415
            Index,
            MetricKind,
            ScalarKind,
        )

        return Index(ndim=VECTOR_DIMENSIONS, metric=MetricKind.Cos, dtype=ScalarKind.F32)

    def _query_index(self, normalized_features: list[float], k: int) -> list[tuple[int, float]]:
        """Search the index for the k nearest neighbors.

        :param normalized_features: Z-score normalized query vector.
        :param k: Number of neighbors to return.
        """
        if self._search_index is None or len(self._search_index) == 0:
            return []
        vec = np.array(normalized_features, dtype=np.float32)
        results = self._search_index.search(vec, k)
        return [
            (int(lbl), float(dist))
            for lbl, dist in zip(results.keys, results.distances, strict=False)
        ]

    async def _rebuild_search_index(self) -> None:
        """Rebuild the search index from all stored analysis rows."""
        async with self._rebuild_lock:
            await self._rebuild_search_index_locked()

    async def _rebuild_search_index_locked(self) -> None:
        """Rebuild body — assumes self._rebuild_lock is held."""
        # Cross-AA-provider merge runs in the controller (see get_merged_audio_analysis_rows).
        # Conflict resolution is timestamp-order (latest non-None write wins per field), which
        # means a re-run of the primary analyzer can override fields a secondary analyzer
        # populated earlier — accept that for symmetry with the rest of MA's analysis stack.
        # Rows from currently-unavailable AA providers are skipped by the helper.
        merged_entries = await self.mass.streams.audio_analysis.get_merged_audio_analysis_rows(
            self._aa_domain
        )
        if not merged_entries:
            self.logger.info("No analysis rows found in database, skipping index rebuild")
            return

        # Build new state in LOCALS — old self.* state continues to serve queries
        # until we atomically swap at the end.
        new_label_map: dict[int, tuple[str, str]] = {}
        new_signature_cache: dict[tuple[str, str], list[float]] = {}
        new_signatures_by_id: dict[str, list[float]] = {}
        new_provider_by_item_id: dict[str, str] = {}
        next_label = 1

        all_features: list[list[float]] = []
        row_entries: list[tuple[int, list[float]]] = []  # (label, raw vec)
        for item_id, provider, data in merged_entries:
            vec = assemble_vector(data)
            if vec is None or len(vec) != VECTOR_DIMENSIONS:
                continue
            label = next_label
            next_label += 1
            key = (item_id, provider)
            new_label_map[label] = key
            all_features.append(vec)
            row_entries.append((label, vec))
            new_signature_cache[key] = vec
            new_signatures_by_id[item_id] = vec
            new_provider_by_item_id[item_id] = provider

        if not all_features:
            # Help the user diagnose the "250 rows, 0 signatures" case. Peek at
            # up to 3 merged entries and report which required fields are missing.
            missing_report: list[str] = []
            for item_id, _provider, data in merged_entries[:3]:
                missing = [
                    f
                    for f in (
                        "bpm",
                        "energy",
                        "danceability",
                        "loudness_integrated",
                        "loudness_range",
                        "brightness",
                        "harmonic_complexity",
                        "roughness",
                        "rhythmic_regularity",
                        "key",
                        "mode",
                    )
                    if getattr(data, f, None) is None
                ]
                missing_report.append(f"{item_id}: missing {missing}")
            self.logger.info(
                "No valid signatures assembled from %d merged tracks in domain=%s, "
                "skipping index rebuild. Sample diagnostics: %s. "
                "Common cause: current aa_provider_domain lacks required scalar fields — "
                "switch Similarity Source to sonic_analysis (which populates all "
                "required hard scalars).",
                len(merged_entries),
                self._aa_domain,
                "; ".join(missing_report),
            )
            return

        new_corpus_means, new_corpus_stds = compute_corpus_stats(all_features)

        # Each rebuild writes a NEW versioned file so the previous viewer's mmap
        # is never disturbed. After the atomic swap the old file gets cleaned up.
        new_index_path = Path(self.mass.storage_path) / USEARCH_INDEX_FILENAME_TPL.format(
            domain=self._aa_domain, version=int(time.time() * 1000)
        )

        def _build_save_and_view() -> Any:
            builder = self._make_empty_index()
            for label, features in row_entries:
                normalized = normalize_features(features, new_corpus_means, new_corpus_stds)
                builder.add(label, np.array(normalized, dtype=np.float32))
            builder.save(str(new_index_path))
            viewer = self._make_empty_index()
            viewer.view(str(new_index_path))
            return viewer

        new_viewer = await asyncio.to_thread(_build_save_and_view)

        # Atomic swap: queries that yielded before this point either resume seeing
        # fully old state (if scheduled before this block) or fully new state
        # (after). No `await` between writes, so other tasks cannot observe a
        # half-rotated state.
        old_search_index = self._search_index
        self._search_index = new_viewer
        self._label_map = new_label_map
        self._signature_cache = new_signature_cache
        self._signatures_by_id = new_signatures_by_id
        self._provider_by_item_id = new_provider_by_item_id
        self.corpus_means = new_corpus_means
        self.corpus_stds = new_corpus_stds

        # Drop the old viewer's reference; CPython refcounting closes the mmap
        # synchronously, releasing the previous on-disk file for unlink below.
        del old_search_index

        # Best-effort cleanup of the previous versioned files (off the event loop).
        await asyncio.to_thread(self._cleanup_stale_index_files, new_index_path)

        self.logger.info("Rebuilt search index with %d signatures", len(row_entries))

    def _cleanup_stale_index_files(self, keep: Path) -> None:
        """Remove old versioned index files for the active domain except `keep`."""
        for path in self._existing_index_files():
            if path == keep:
                continue
            try:
                path.unlink()
            except OSError as err:
                self.logger.debug("Could not unlink stale index file %s: %s", path, err)

    @staticmethod
    def _track_genres(track: Track) -> set[str]:
        """Lower-cased genre set for a track; empty when metadata is absent."""
        if not track.metadata or not track.metadata.genres:
            return set()
        return {g.lower() for g in track.metadata.genres}

    async def _resolve_candidate_tracks(
        self, candidates: list[Candidate], log_context: str
    ) -> list[tuple[Candidate, Track | None]]:
        """Resolve every candidate's Track concurrently; None marks a lookup miss."""

        async def _one(cand: Candidate) -> tuple[Candidate, Track | None]:
            try:
                track = await self.mass.music.tracks.get(cand.item_id, cand.provider)
            except MusicAssistantError as err:
                self.logger.debug(
                    "%s lookup failed for %s/%s: %s",
                    log_context,
                    cand.provider,
                    cand.item_id,
                    err,
                )
                return (cand, None)
            return (cand, track)

        return list(await asyncio.gather(*(_one(c) for c in candidates)))

    async def _apply_metadata_filters(
        self,
        results: list[Candidate],
        filter_genres: list[str] | None = None,
        exclude_artists: list[str] | None = None,
    ) -> list[Candidate]:
        """Apply metadata-based filters that require track resolution."""
        if not filter_genres and not exclude_artists:
            return results

        genre_set = {g.lower() for g in filter_genres} if filter_genres else None
        artist_set = {a.lower() for a in exclude_artists} if exclude_artists else None

        filtered: list[Candidate] = []
        for cand, track in await self._resolve_candidate_tracks(results, "filter"):
            if track is None:
                continue

            if genre_set:
                if not self._track_genres(track) & genre_set:
                    continue

            if artist_set:
                track_artists = {a.name.lower() for a in (track.artists or [])}
                if track_artists & artist_set:
                    continue

            filtered.append(cand)
        return filtered

    async def _apply_metadata_reranking(
        self,
        seed_item_ids: list[str],
        results: list[Candidate],
        weights: dict[str, float],
    ) -> list[Candidate]:
        """Apply genre and year bonuses to re-rank candidates.

        :param seed_item_ids: All seed track ids — genres are unioned across
            seeds, year is averaged. With one seed this collapses to the
            single-seed behavior; with N seeds the metadata bonus reflects
            the centroid of the seed set, matching how the audio-distance
            blend already works.
        """
        seed_lookups = [self._resolve_seed_track(sid) for sid in seed_item_ids]
        seed_tracks = [t for t in await asyncio.gather(*seed_lookups) if t is not None]
        if not seed_tracks:
            return results

        seed_genres: set[str] = set()
        seed_years: list[int] = []
        for seed_track in seed_tracks:
            seed_genres |= self._track_genres(seed_track)
            if isinstance(seed_track.album, Album) and seed_track.album.year:
                seed_years.append(seed_track.album.year)
        seed_year_avg = sum(seed_years) / len(seed_years) if seed_years else None

        scored: list[Candidate] = []
        for cand, cand_track in await self._resolve_candidate_tracks(results, "rerank"):
            bonus = 0.0
            if cand_track is None:
                scored.append(cand)
                continue

            genre_weight = weights.get("genre", 0.0)
            year_weight = weights.get("era", 0.0)
            if genre_weight > 0 and seed_genres:
                cand_genres = self._track_genres(cand_track)
                if cand_genres:
                    intersection = len(seed_genres & cand_genres)
                    union_size = len(seed_genres | cand_genres)
                    if union_size > 0:
                        bonus -= METADATA_BONUS_SCALE * genre_weight * (intersection / union_size)

            if year_weight > 0 and seed_year_avg is not None:
                cand_year: int | None = None
                if isinstance(cand_track.album, Album) and cand_track.album.year:
                    cand_year = cand_track.album.year
                if cand_year is not None:
                    year_diff = abs(seed_year_avg - cand_year)
                    bonus -= METADATA_BONUS_SCALE * year_weight * (1.0 / (1.0 + year_diff * 0.1))

            scored.append(cand._replace(distance=cand.distance + bonus))

        scored.sort(key=lambda c: c.distance)
        return scored

    async def _resolve_seed_track(self, seed_item_id: str) -> Track | None:
        """Resolve a seed track from its item_id, falling back to the 'library' provider."""
        seed_prov = self._provider_by_item_id.get(seed_item_id, "library")
        try:
            return await self.mass.music.tracks.get(seed_item_id, seed_prov)
        except MusicAssistantError as err:
            self.logger.debug("Could not resolve seed %s/%s: %s", seed_prov, seed_item_id, err)
            return None

    async def _resolve_results(
        self,
        items: list[tuple[str, str, float, int]],
        debug_breakdown_map: dict[str, dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Resolve track metadata for result items.

        :param items: List of (item_id, provider, distance, generation) tuples to resolve.
        :param debug_breakdown_map: Optional per-track debug breakdown (weighted_distance,
            metadata_bonus, group_distances) keyed by item_id.
        """

        async def _resolve_one(
            item_id: str,
            provider: str,
            dist: float,
            gen: int,
        ) -> dict[str, Any]:
            entry: dict[str, Any] = {
                "item_id": item_id,
                "provider": provider,
                "distance": round(dist, 4),
                "generation": gen,
            }
            try:
                track = await self.mass.music.tracks.get(item_id, provider)
                artists = ", ".join(a.name for a in getattr(track, "artists", []) or [])
                entry["name"] = track.name
                entry["artist"] = artists
            except MusicAssistantError as err:
                self.logger.debug("Could not resolve %s/%s for output: %s", provider, item_id, err)
                entry["name"] = "(unknown)"
                entry["artist"] = ""
            if debug_breakdown_map and item_id in debug_breakdown_map:
                entry.update(debug_breakdown_map[item_id])
            return entry

        return list(
            await asyncio.gather(
                *[_resolve_one(cid, prov, dist, gen) for cid, prov, dist, gen in items]
            )
        )
