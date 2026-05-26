"""Sonic Similarity plugin.

Two similarity engines in one plugin, both backed by usearch HNSW:

* **18-dim weighted-Euclidean** (always on): per-track signature
  assembled from sonic_analysis scalars (BPM, energy, loudness, …) and
  ranked with a configurable weight preset. Atomic mmap-view rebuild.

* **1024-dim CLAP cosine** (opt-in via the ``enable_clap_index`` config
  entry): builds a second usearch index over the CLAP audio embeddings
  already stored by sonic_analysis under
  ``audio_analysis.extra_data["clap_embedding"]``. Track-to-track
  semantic-audio similarity, with no additional dependencies beyond
  usearch itself.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import numpy as np
from music_assistant_models.enums import MediaType, ProviderFeature
from music_assistant_models.errors import MusicAssistantError, SetupFailedError
from music_assistant_models.media_items import Album, RecommendationFolder, SearchResults
from music_assistant_models.unique_list import UniqueList

from music_assistant.models.plugin import PluginProvider
from music_assistant.providers.sonic_similarity.clap_index import (
    CLAP_EMBEDDING_DIM,
    ClapIndex,
)
from music_assistant.providers.sonic_similarity.similarity import (
    Candidate,
    ScoredCandidate,
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

# aa_provider_domain is interpolated into on-disk filename templates above.
# Allow only the shape every real MA provider domain uses (e.g. sonic_analysis,
# spotify, lastfm_recommendations) so a stray '/' or '..' in the config value
# can't escape mass.storage_path during writes/unlinks.
_AA_DOMAIN_PATTERN = re.compile(r"^[a-zA-Z0-9_]+$")
_AA_DOMAIN_DEFAULT = "sonic_analysis"


def _safe_aa_domain(raw: Any, logger: logging.Logger) -> str:
    """Return ``raw`` if it's a valid AA-provider domain, else the default.

    :param raw: The raw value read from the CONF_AA_PROVIDER config entry.
    :param logger: Logger to warn on when falling back to the default.
    """
    candidate = str(raw or _AA_DOMAIN_DEFAULT).strip()
    if _AA_DOMAIN_PATTERN.fullmatch(candidate):
        return candidate
    logger.warning(
        "aa_provider_domain %r is not a valid provider domain (expected "
        "alphanumeric + underscore only); falling back to %r.",
        candidate,
        _AA_DOMAIN_DEFAULT,
    )
    return _AA_DOMAIN_DEFAULT


CONF_ENABLE_CLAP_INDEX = "enable_clap_index"
CONF_ENABLE_TEXT_SEARCH = "enable_text_search"
CONF_ENABLE_DISCOVER_ROW = "enable_discover_row"
CONF_DISCOVER_PRESET = "discover_preset"
CONF_DISCOVER_DIVERSITY = "discover_diversity"
EXTRA_DATA_CLAP_EMBEDDING = "clap_embedding"

# Keys for the read-only status rows on the plugin config page.
CONF_LABEL_STATUS_18DIM = "status_label_18dim"
CONF_LABEL_STATUS_CLAP = "status_label_clap"
CONF_LABEL_STATUS_TEXT = "status_label_text"
# Action keys dispatched back into get_config_entries via the ACTION button entries.
ACTION_REBUILD_18DIM = "rebuild_18dim_index"
ACTION_REBUILD_CLAP = "rebuild_clap_index"

# Features exposed to the cross-provider dispatchers.
# * SIMILAR_TRACKS — controllers/media/tracks.py:378-387 fans out to plugins after
#   music-provider mappings have been tried. We're the local fallback engine.
# * RECOMMENDATIONS — controllers/music.py:803 gathers folders from every plugin
#   declaring this feature and zip-merges them into music/recommendations, which
#   the frontend's HomeWidgetRows.vue renders as discover-page widget rows
#   without any client-side wiring per plugin.
# Both methods return [] when the engine isn't ready, which the dispatchers treat
# as "this provider has nothing right now" — no dynamic feature-set tricks needed.
SUPPORTED_FEATURES = {
    ProviderFeature.SIMILAR_TRACKS,
    ProviderFeature.RECOMMENDATIONS,
}

# Tunables for the recommendations() folder. RECOMMEND_SEED_COUNT keeps the
# fan-out cost bounded; RECOMMEND_ITEM_LIMIT is the visible row length.
RECOMMEND_SEED_COUNT: int = 5
RECOMMEND_PER_SEED_LIMIT: int = 10
RECOMMEND_ITEM_LIMIT: int = 12


def _parse_clap_embedding(raw: Any) -> np.ndarray | None:
    """Coerce a stored embedding (list/tuple) into a 1024-dim float32 array, or None."""
    if raw is None:
        return None
    try:
        arr = np.asarray(raw, dtype=np.float32).reshape(-1)
    except (TypeError, ValueError):
        return None
    if arr.shape != (CLAP_EMBEDDING_DIM,):
        return None
    return arr


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
    candidates: list[ScoredCandidate],
    seed_ids: set[str],
    exclude_track_ids: set[str] | None,
    filter_providers: set[str] | None,
) -> list[ScoredCandidate]:
    """Apply cheap post-ANN filters to candidate list.

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


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider instance with given configuration."""
    features = SUPPORTED_FEATURES.copy()
    if bool(config.get_value(CONF_ENABLE_TEXT_SEARCH)):
        features.add(ProviderFeature.SEARCH)
    return SonicSimilarityPlugin(mass, manifest, config, features)


async def _collect_status_text(
    mass: MusicAssistant, instance_id: str | None
) -> tuple[str, str, str]:
    """Return (18-dim, CLAP, text-encoder) label-text triples for the plugin page.

    Each string is single-line, safe to render in a LABEL config entry, and
    degrades gracefully when the provider is not yet loaded.

    :param mass: MusicAssistant instance used to look up the loaded provider.
    :param instance_id: Provider instance id to inspect, or None before the
        provider is loaded.
    """
    eighteen = "18-dim engine: not yet loaded"
    clap = "CLAP engine: disabled"
    text = "Text encoder: disabled"
    if not instance_id:
        return eighteen, clap, text
    provider = mass.get_provider(instance_id)
    if not isinstance(provider, SonicSimilarityPlugin):
        return eighteen, clap, text

    # Optional coverage lookup against the upstream AA provider via #3851's API.
    coverage_pct: float | None = None
    aa_domain = provider._aa_domain
    try:
        coverage = await mass.streams.audio_analysis.get_coverage(aa_domain)
    except Exception:
        coverage = None
    if coverage is not None:
        total = coverage.analyzed + coverage.pending
        if total > 0:
            coverage_pct = round(100.0 * coverage.analyzed / total, 1)

    # 18-dim line.
    index_size = len(provider._search_index) if provider._search_index is not None else 0
    parts = [
        f"{index_size:,} tracks indexed",
        f"{len(provider._signature_cache):,} signatures cached",
        f"corpus stats {'ready' if provider.corpus_means is not None else 'pending'}",
    ]
    if coverage_pct is not None:
        parts.append(f"{coverage_pct}% coverage")
    eighteen = "18-dim engine: " + " · ".join(parts)
    if (err_18dim := provider._last_rebuild_error.get("18-dim")) is not None:
        eighteen += f" — last rebuild failed: {err_18dim}"

    # CLAP line (only meaningful when the index is built).
    if provider._clap_index is not None:
        clap_size = len(provider._clap_index)
        clap_parts = [f"{clap_size:,} embeddings indexed"]
        if coverage_pct is not None:
            clap_parts.append(f"{coverage_pct}% coverage")
        clap = "CLAP engine: " + " · ".join(clap_parts)
        if (err_clap := provider._last_rebuild_error.get("CLAP")) is not None:
            clap += f" — last rebuild failed: {err_clap}"

    # Text-encoder line — encoder state is independent of the index.
    if bool(provider.config.get_value(CONF_ENABLE_TEXT_SEARCH)):
        if provider._text_encoder is not None:
            text = "Text encoder: loaded (warm)"
        else:
            text = "Text encoder: cold (downloads on first query, ~500MB)"

    return eighteen, clap, text


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider.

    :param mass: MusicAssistant instance.
    :param instance_id: id of an existing provider instance (None if new instance setup).
    :param action: action key called from config entries UI.
    :param values: the (intermediate) raw values for config entries sent with the action.
    """
    from music_assistant_models.config_entries import (  # noqa: PLC0415
        ConfigEntry,
        ConfigValueOption,
    )
    from music_assistant_models.enums import ConfigEntryType  # noqa: PLC0415

    # Dispatch rebuild-button clicks onto the running provider instance. Each
    # rebuild is fire-and-forget (mass.create_task) so the form returns
    # immediately; the per-engine _rebuild_lock / _clap_rebuild_lock serialise
    # double-clicks into a no-op tail.
    if instance_id and action in (ACTION_REBUILD_18DIM, ACTION_REBUILD_CLAP):
        provider = mass.get_provider(instance_id)
        if isinstance(provider, SonicSimilarityPlugin):
            if action == ACTION_REBUILD_18DIM:
                mass.create_task(provider._safe_rebuild("18-dim", provider._rebuild_search_index))
            elif action == ACTION_REBUILD_CLAP and provider._clap_index is not None:
                mass.create_task(
                    provider._safe_rebuild("CLAP", provider._rebuild_clap_index_from_database)
                )

    status_18, status_clap, status_text = await _collect_status_text(mass, instance_id)

    return (
        ConfigEntry(
            key=CONF_AA_PROVIDER,
            type=ConfigEntryType.STRING,
            default_value="sonic_analysis",
            label="Analysis Provider",
            description="Which audio analysis provider's data to use for similarity vectors. "
            "Default: sonic_analysis (librosa + CLAP, on-device).",
        ),
        # --- 18-dim engine: status + rebuild ---
        ConfigEntry(
            key=CONF_LABEL_STATUS_18DIM,
            type=ConfigEntryType.LABEL,
            label=status_18,
            category="status",
        ),
        ConfigEntry(
            key=ACTION_REBUILD_18DIM,
            type=ConfigEntryType.ACTION,
            label="Rebuild 18-dim index",
            description="Re-scan all stored signatures and rebuild the weighted-Euclidean "
            "search index. Runs in the background; refresh the page to see updated counts.",
            action=ACTION_REBUILD_18DIM,
            action_label="Rebuild 18-dim index",
            category="status",
            required=False,
        ),
        # --- CLAP engine toggle ---
        ConfigEntry(
            key=CONF_ENABLE_CLAP_INDEX,
            type=ConfigEntryType.BOOLEAN,
            default_value=False,
            label="Enable CLAP embedding index",
            description="Also build a second usearch index over the 1024-dim CLAP audio "
            "embeddings already stored by sonic_analysis. Enables track-to-track semantic "
            "similarity via the sonic_similarity/similar_clap API. Requires no extra "
            "downloads — uses embeddings already on disk.",
        ),
        # CLAP status + rebuild (auto-hidden when the toggle above is off).
        ConfigEntry(
            key=CONF_LABEL_STATUS_CLAP,
            type=ConfigEntryType.LABEL,
            label=status_clap,
            category="status",
            depends_on=CONF_ENABLE_CLAP_INDEX,
            depends_on_value=True,
        ),
        ConfigEntry(
            key=ACTION_REBUILD_CLAP,
            type=ConfigEntryType.ACTION,
            label="Rebuild CLAP index",
            description="Incrementally re-scan audio_analysis rows and add any missing CLAP "
            "embeddings to the 1024-dim index. Runs in the background; refresh the page to "
            "see updated counts.",
            action=ACTION_REBUILD_CLAP,
            action_label="Rebuild CLAP index",
            category="status",
            required=False,
            depends_on=CONF_ENABLE_CLAP_INDEX,
            depends_on_value=True,
        ),
        # --- text-search toggle + status ---
        ConfigEntry(
            key=CONF_ENABLE_TEXT_SEARCH,
            type=ConfigEntryType.BOOLEAN,
            default_value=False,
            label="Enable free-text search",
            description="Enable natural-language track search (e.g. 'super dancy disco') via "
            "the CLAP GPT2 text encoder, exposed as sonic_similarity/text_search. "
            "First-time use lazily downloads ~500MB of GPT2 weights to the local "
            "HuggingFace cache — the model is loaded on the first query, not at plugin "
            "start. Implicitly enables the CLAP embedding index above (text and audio "
            "share the same 1024-dim joint embedding space).",
        ),
        ConfigEntry(
            key=CONF_LABEL_STATUS_TEXT,
            type=ConfigEntryType.LABEL,
            label=status_text,
            category="status",
            depends_on=CONF_ENABLE_TEXT_SEARCH,
            depends_on_value=True,
        ),
        # --- discover-row controls ---
        ConfigEntry(
            key=CONF_ENABLE_DISCOVER_ROW,
            type=ConfigEntryType.BOOLEAN,
            default_value=True,
            label="Show 'Inspired by recently played' on the discover page",
            description="Yield a discover-page row seeded by your recently-played tracks. "
            "Disable to suppress the row without uninstalling the plugin.",
            category="discover",
        ),
        ConfigEntry(
            key=CONF_DISCOVER_PRESET,
            type=ConfigEntryType.STRING,
            default_value="discover",
            label="Discover row preset",
            description="Similarity weight preset used to rank candidates for the row. "
            "'discover' favours novelty (low genre/era weighting); 'balanced' is uniform; "
            "'vibe' weights mood + timbre; 'party' weights rhythm + regularity; 'genre_era' "
            "stays close to the seed's genre and decade.",
            options=[
                ConfigValueOption("Discover (novelty-leaning)", "discover"),
                ConfigValueOption("Balanced", "balanced"),
                ConfigValueOption("Vibe (mood + timbre)", "vibe"),
                ConfigValueOption("Party (rhythm-heavy)", "party"),
                ConfigValueOption("Genre + Era (stay close)", "genre_era"),
            ],
            category="discover",
            depends_on=CONF_ENABLE_DISCOVER_ROW,
            depends_on_value=True,
        ),
        ConfigEntry(
            key=CONF_DISCOVER_DIVERSITY,
            type=ConfigEntryType.FLOAT,
            default_value=0.2,
            label="Discover row diversity",
            description="0.0 keeps results closest to the seeds; 1.0 maximises variety via "
            "MMR (some results may be less similar but more distinct from each other).",
            category="discover",
            depends_on=CONF_ENABLE_DISCOVER_ROW,
            depends_on_value=True,
        ),
    )


class SonicSimilarityPlugin(PluginProvider):
    """Plugin that provides similarity search over sonic analysis signatures."""

    def __init__(
        self,
        mass: MusicAssistant,
        manifest: ProviderManifest,
        config: ProviderConfig,
        supported_features: set[ProviderFeature] | None = None,
    ) -> None:
        """Initialize the Sonic Similarity plugin."""
        super().__init__(mass, manifest, config, supported_features)
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
        # CLAP index — only populated when CONF_ENABLE_CLAP_INDEX is true
        # (or implicitly via CONF_ENABLE_TEXT_SEARCH, which requires it).
        self._clap_index: ClapIndex | None = None
        self._clap_rebuild_lock = asyncio.Lock()
        # CLAP text encoder — lazy: stays None until the first text_search call.
        self._text_encoder: Any = None
        self._text_encoder_lock = asyncio.Lock()
        # Per-label last error from fire-and-forget rebuild tasks.
        self._last_rebuild_error: dict[str, str] = {}

    async def _safe_rebuild(self, label: str, rebuild_fn: Callable[[], Awaitable[None]]) -> None:
        """Run a rebuild fn from a background task, swallowing failures into status state.

        :param label: Engine label used as the status-row error key (e.g. "18-dim", "CLAP").
        :param rebuild_fn: Zero-arg coroutine-returning callable to execute.
        """
        try:
            await rebuild_fn()
            self._last_rebuild_error.pop(label, None)
        except Exception as err:
            self.logger.exception("%s rebuild failed", label)
            self._last_rebuild_error[label] = str(err)

    async def handle_async_init(self) -> None:
        """Build the 18-dim search index before the provider is registered.

        Failures here raise SetupFailedError so the loader surfaces them through
        MA's standard provider-failure UI (a silent failure in loaded_in_mass
        would be swallowed by its fire-and-forget task wrapper).
        """
        self._aa_domain = _safe_aa_domain(self.config.get_value(CONF_AA_PROVIDER), self.logger)
        self.logger.info(
            "Sonic Similarity initializing (aa_provider=%s), building search index...",
            self._aa_domain,
        )
        try:
            await self._rebuild_search_index()
        except Exception as err:
            msg = f"Failed to build 18-dim search index: {err}"
            raise SetupFailedError(msg) from err
        self.logger.info(
            "Search index ready: %d signatures cached, corpus_stats=%s",
            len(self._signature_cache),
            self.corpus_means is not None,
        )

    async def loaded_in_mass(self) -> None:
        """Register similarity API commands and set up the optional CLAP engine."""
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

        text_search_enabled = bool(self.config.get_value(CONF_ENABLE_TEXT_SEARCH))
        # Text search requires the 1024-dim CLAP index — silently auto-enable it
        # when text search is on, since they share the same joint embedding space.
        clap_enabled = bool(self.config.get_value(CONF_ENABLE_CLAP_INDEX)) or text_search_enabled

        if clap_enabled:
            try:
                self._clap_index = ClapIndex(self.mass, self.logger)
                await self._clap_index.load()
                self._unregister_handles.append(
                    self.mass.register_api_command(
                        "sonic_similarity/similar_clap", self._handle_similar_clap
                    )
                )
                await self._rebuild_clap_index_from_database()
                self.logger.info("CLAP index ready: %d embeddings", len(self._clap_index))
            except Exception:
                # CLAP is optional — failure must not block the 18-dim engine.
                self.logger.exception("CLAP index setup failed; CLAP engine will be unavailable")
                self._clap_index = None

        if text_search_enabled:
            # Encoder load is deferred to the first /text_search call (lazy).
            self._unregister_handles.append(
                self.mass.register_api_command(
                    "sonic_similarity/text_search", self._handle_text_search
                )
            )
            self.logger.info("Text search ready (encoder will load on first query)")

    async def unload(self, is_removed: bool = False) -> None:
        """Unregister API commands; delete on-disk indexes when the provider is uninstalled."""
        for unregister in self._unregister_handles:
            unregister()
        self._unregister_handles.clear()
        if self._clap_index is not None:
            try:
                await self._clap_index.close()
            except Exception as err:
                self.logger.debug("CLAP index close failed: %s", err)
            self._clap_index = None
        # Drop encoder ref so its (large) tensors can be GC'd.
        self._text_encoder = None
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

    def _empty_similar_response(self, params: SimilarParams, reason: str) -> dict[str, Any]:
        """Build the early-return shape for _handle_similar."""
        return {
            "analyzed": False,
            "reason": reason,
            "seed_track_ids": params.item_ids,
            "blend_mode": params.blend_mode,
            "depth": params.depth,
            "items": [],
        }

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
        if self.corpus_means is None or self.corpus_stds is None:
            return self._empty_similar_response(params, "corpus_not_ready")
        if not seed_sigs:
            return self._empty_similar_response(params, "seed_not_in_index")

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

            raw_candidates: list[ScoredCandidate] = [
                ScoredCandidate(
                    item_id=cand_id,
                    provider=id_to_prov.get(cand_id, "library"),
                    distance=cos_dist,
                )
                for cand_id, cos_dist in candidate_ids
            ]

            seed_id_set = set(ctx.valid_seed_ids)
            exclude_set = (
                set(ctx.params.exclude_track_ids) if ctx.params.exclude_track_ids else None
            )
            filter_prov_set = (
                set(ctx.params.filter_providers) if ctx.params.filter_providers else None
            )
            filtered = apply_filters(
                raw_candidates, seed_id_set | seen, exclude_set, filter_prov_set
            )

            results: list[tuple[str, str, list[float], float]] = []
            for cand in filtered:
                cand_features = self._signature_cache.get((cand.item_id, cand.provider))
                if cand_features is None:
                    continue
                cand_normalized = normalize_features(
                    cand_features, ctx.corpus_means, ctx.corpus_stds
                )
                dist = compute_weighted_distance(ctx.orig_normalized, cand_normalized, ctx.weights)
                results.append((cand.item_id, cand.provider, cand_features, dist))

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

    # ------------------------------------------------------------------
    # Cross-provider SIMILAR_TRACKS hook (PluginProvider feature surface)
    # ------------------------------------------------------------------

    async def get_similar_tracks(self, track: Track, limit: int = 25) -> list[Track]:
        """Implement ProviderFeature.SIMILAR_TRACKS via the 18-dim engine.

        Called by mass.music.tracks.similar_tracks() when no MusicProvider
        mapping yielded similar tracks itself (see
        controllers/media/tracks.py:378-387). Returns [] when the corpus
        isn't ready, when none of the track's provider mappings are
        indexed, or when the engine returns no candidates — all three
        states are interchangeable to the dispatcher's truthy check.

        :param track: Full Track object (with provider_mappings) as
            handed to us by the cross-provider dispatcher.
        :param limit: Max number of similar tracks to return.
        """
        if self.corpus_means is None or not self._signature_cache:
            return []

        # Pick the first provider mapping that's actually indexed. Skip the
        # library aggregator since audio_analysis is keyed on the streaming
        # provider's item_id, not on library numeric ids — the discover row
        # uses the same logic.
        seed_item_id: str | None = None
        for mapping in track.provider_mappings or ():
            if mapping.provider_domain == "library":
                continue
            if (mapping.item_id, mapping.provider_instance) in self._signature_cache:
                seed_item_id = mapping.item_id
                break
            # Fall back to provider_by_item_id (last-write-wins by item_id only),
            # which is how the public /similar handler resolves seeds too.
            if mapping.item_id in self._signatures_by_id:
                seed_item_id = mapping.item_id
                break
        if seed_item_id is None:
            return []

        response = await self._handle_similar(item_id=seed_item_id, limit=limit)
        items = response.get("items") or []
        if not items:
            return []

        async def _resolve(entry: dict[str, Any]) -> Track | None:
            try:
                return await self.mass.music.tracks.get(entry["item_id"], entry["provider"])
            except MusicAssistantError:
                return None

        resolved = await asyncio.gather(*[_resolve(e) for e in items])
        return [t for t in resolved if t is not None]

    # ------------------------------------------------------------------
    # Cross-provider RECOMMENDATIONS hook (home/discover page)
    # ------------------------------------------------------------------

    async def recommendations(self) -> list[RecommendationFolder]:  # noqa: PLR0915
        """Yield an 'Inspired by recently played' folder for the discover page.

        Picked up by the music/recommendations dispatcher (controllers/music.py:803)
        and rendered by HomeWidgetRows.vue alongside the library's own
        recommendation folders. Returns [] when the engine isn't ready or when
        no recent tracks intersect the index — the dispatcher then simply
        omits us from the response (no empty card on the page).

        Internally: sample up to RECOMMEND_SEED_COUNT recent tracks, find the
        ones we have indexed, fan out per-seed via _handle_similar to get a
        diverse pool, dedupe by (provider, item_id), and resolve the first
        RECOMMEND_ITEM_LIMIT to full Tracks.
        """
        if not bool(self.config.get_value(CONF_ENABLE_DISCOVER_ROW)):
            return []
        if self.corpus_means is None or not self._signature_cache:
            return []

        try:
            recent = await self.mass.music.recently_played(
                limit=RECOMMEND_SEED_COUNT,
                media_types=[MediaType.TRACK],
                fully_played_only=False,
            )
        except Exception as err:
            self.logger.debug("recently_played failed: %s", err)
            return []
        if not recent:
            return []

        # Walk each recent mapping into a seed item_id our index has analysed.
        # The mapping is library-aggregated; we need the underlying streaming
        # provider's id (or the filesystem path) — same logic as
        # get_similar_tracks but starting from an ItemMapping instead of a Track.
        seed_ids: list[str] = []
        seen_seeds: set[str] = set()
        for mapping in recent:
            try:
                track = await self.mass.music.tracks.get(mapping.item_id, mapping.provider)
            except MusicAssistantError:
                continue
            seed_id: str | None = None
            for pm in track.provider_mappings or ():
                if pm.provider_domain == "library":
                    continue
                if (pm.item_id, pm.provider_instance) in self._signature_cache:
                    seed_id = pm.item_id
                    break
                if pm.item_id in self._signatures_by_id:
                    seed_id = pm.item_id
                    break
            if seed_id and seed_id not in seen_seeds:
                seed_ids.append(seed_id)
                seen_seeds.add(seed_id)

        if not seed_ids:
            return []

        preset = str(self.config.get_value(CONF_DISCOVER_PRESET) or "discover")
        try:
            diversity = float(str(self.config.get_value(CONF_DISCOVER_DIVERSITY) or 0.0))
        except (TypeError, ValueError):
            diversity = 0.0

        # Fan out per seed; union results, first-occurrence wins (we already
        # ordered seeds by recency above, so earlier seeds get priority).
        candidate_order: list[tuple[str, str]] = []
        candidate_seen: set[tuple[str, str]] = set()
        for sid in seed_ids:
            response = await self._handle_similar(
                item_id=sid,
                limit=RECOMMEND_PER_SEED_LIMIT,
                preset=preset,
                diversity=diversity,
            )
            for entry in response.get("items") or []:
                key = (entry["provider"], entry["item_id"])
                if key in candidate_seen:
                    continue
                candidate_seen.add(key)
                candidate_order.append(key)
                if len(candidate_order) >= RECOMMEND_ITEM_LIMIT:
                    break
            if len(candidate_order) >= RECOMMEND_ITEM_LIMIT:
                break

        if not candidate_order:
            return []

        async def _resolve(provider: str, item_id: str) -> Track | None:
            try:
                return await self.mass.music.tracks.get(item_id, provider)
            except MusicAssistantError:
                return None

        resolved = await asyncio.gather(*[_resolve(p, i) for p, i in candidate_order])
        items = [t for t in resolved if t is not None]
        if not items:
            return []

        return [
            RecommendationFolder(
                item_id="inspired_by_recently_played",
                provider=self.instance_id,
                name="Inspired by recently played",
                translation_key="inspired_by_recently_played",
                icon="mdi-shimmer",
                items=UniqueList(items),
            ),
        ]

    async def search(
        self,
        search_query: str,
        media_types: list[MediaType],
        limit: int = 5,
    ) -> SearchResults:
        """Implement ProviderFeature.SEARCH via CLAP free-text → track similarity."""
        if MediaType.TRACK not in media_types:
            return SearchResults()
        if self._clap_index is None or len(self._clap_index) == 0:
            return SearchResults()
        emb_np = await self._embed_text_query(search_query)
        if emb_np is None:
            return SearchResults()
        matches = await self._clap_index.search(emb_np, limit)

        async def _resolve(provider: str, item_id: str) -> Track | None:
            try:
                return await self.mass.music.tracks.get(item_id, provider)
            except MusicAssistantError:
                return None

        resolved = await asyncio.gather(
            *[_resolve(cand.provider, cand.item_id) for cand in matches]
        )
        return SearchResults(tracks=[t for t in resolved if t is not None])

    async def _embed_text_query(self, query: str) -> np.ndarray | None:
        """Encode a free-text query through the CLAP text encoder, or None if unavailable."""
        encoder = await self._get_text_encoder()
        if encoder is None:
            return None
        text_emb = await asyncio.to_thread(encoder.get_text_embeddings, [query])
        return cast("np.ndarray", text_emb[0].detach().cpu().numpy().astype(np.float32).reshape(-1))

    async def _handle_status(self) -> dict[str, Any]:
        """Return current analysis status."""
        index_size = len(self._search_index) if self._search_index is not None else 0
        text_search_enabled = bool(self.config.get_value(CONF_ENABLE_TEXT_SEARCH))
        status: dict[str, Any] = {
            "index_size": index_size,
            "has_corpus_stats": self.corpus_means is not None,
            "cached_signatures": len(self._signature_cache),
            "aa_provider_domain": self._aa_domain,
            "clap_index_enabled": self._clap_index is not None,
            "text_search_enabled": text_search_enabled,
            "text_encoder_loaded": self._text_encoder is not None,
        }
        if self._clap_index is not None:
            status["clap_index_size"] = len(self._clap_index)
        return status

    async def _handle_rebuild_index(self) -> dict[str, Any]:
        """Rebuild the USearch index(es) from stored analysis data."""
        await self._rebuild_search_index()
        result: dict[str, Any] = {
            "status": "rebuilt",
            "index_size": len(self._search_index) if self._search_index is not None else 0,
        }
        if self._clap_index is not None:
            await self._rebuild_clap_index_from_database()
            result["clap_index_size"] = len(self._clap_index)
        return result

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

    async def _rebuild_search_index_locked(self) -> None:  # noqa: PLR0915
        """Rebuild body — assumes self._rebuild_lock is held."""
        # Cross-AA-provider merge runs in the controller, streamed as an
        # AsyncGenerator (iter_merged_audio_analysis_rows). Conflict resolution
        # is timestamp-order (latest non-None write wins per field), which means
        # a re-run of the primary analyzer can override fields a secondary
        # analyzer populated earlier — accept that for symmetry with the rest
        # of MA's analysis stack. Rows from currently-unavailable AA providers
        # are skipped by the helper.

        # Build new state in LOCALS — old self.* state continues to serve queries
        # until we atomically swap at the end.
        new_label_map: dict[int, tuple[str, str]] = {}
        new_signature_cache: dict[tuple[str, str], list[float]] = {}
        new_signatures_by_id: dict[str, list[float]] = {}
        new_provider_by_item_id: dict[str, str] = {}
        next_label = 1

        all_features: list[list[float]] = []
        row_entries: list[tuple[int, list[float]]] = []  # (label, raw vec)
        # Sample up to 3 early entries for the "0 signatures" diagnostic peek
        # below; the controller streams now, so we can't slice the full list.
        sampled_for_diag: list[tuple[str, str, Any]] = []
        total_merged_rows = 0

        async for (
            item_id,
            provider,
            data,
        ) in self.mass.streams.audio_analysis.iter_merged_audio_analysis_rows(self._aa_domain):
            total_merged_rows += 1
            if len(sampled_for_diag) < 3:
                sampled_for_diag.append((item_id, provider, data))
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

        if total_merged_rows == 0:
            self.logger.info("No analysis rows found in database, skipping index rebuild")
            return

        if not all_features:
            # Help the user diagnose the "250 rows, 0 signatures" case using the
            # entries sampled during the stream above.
            missing_report: list[str] = []
            for item_id, _provider, data in sampled_for_diag:
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
                total_merged_rows,
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

    # ------------------------------------------------------------------
    # Optional CLAP index (1024-dim cosine over sonic_analysis embeddings)
    # ------------------------------------------------------------------

    async def _rebuild_clap_index_from_database(self) -> None:
        """Add any audio_analysis rows with clap_embedding that aren't yet indexed.

        Idempotent and incremental: existing entries are skipped via the
        index's contains() check, so a rebuild after no new analyses is
        cheap. Guarded by _clap_rebuild_lock so a manual rebuild_index
        cannot interleave with a scheduled refresh.
        """
        if self._clap_index is None:
            return
        async with self._clap_rebuild_lock:
            added = 0
            seen: set[tuple[str, str]] = set()
            async for row in self.mass.streams.audio_analysis.iter_audio_analysis_rows(
                self._aa_domain
            ):
                key = (row["provider"], row["item_id"])
                if key in seen:
                    continue
                seen.add(key)
                if self._clap_index.contains(row["provider"], row["item_id"]):
                    continue
                try:
                    raw = json.loads(row["analysis_data"])
                except (json.JSONDecodeError, TypeError):
                    continue
                emb = _parse_clap_embedding(
                    (raw.get("extra_data") or {}).get(EXTRA_DATA_CLAP_EMBEDDING)
                )
                if emb is None:
                    continue
                try:
                    await self._clap_index.add(row["provider"], row["item_id"], emb)
                    added += 1
                except Exception as err:
                    self.logger.debug(
                        "Add to CLAP index failed for %s/%s: %s",
                        row["provider"],
                        row["item_id"],
                        err,
                    )
            if added > 0:
                await self._clap_index.save()
                self.logger.info("Added %d new embeddings to CLAP index", added)

    async def _handle_similar_clap(self, item_id: str, limit: int = 25) -> dict[str, Any]:
        """Return tracks whose CLAP audio embedding is closest to the seed track's.

        :param item_id: Seed track identifier (provider-agnostic). The first
            label whose reverse-map entry matches is used.
        :param limit: Max number of neighbours to return.
        """
        if self._clap_index is None:
            return {
                "analyzed": False,
                "reason": "clap_index_disabled",
                "seed_track_id": item_id,
                "items": [],
            }
        lookup = self._clap_index.get_embedding_by_item_id(item_id)
        if lookup is None:
            return {
                "analyzed": False,
                "reason": "seed_not_in_index",
                "seed_track_id": item_id,
                "items": [],
            }
        _seed_provider, seed_embedding = lookup
        # +1 because the seed itself is the nearest neighbour; we drop it after.
        raw_results = await self._clap_index.search(seed_embedding, limit + 1)
        items: list[dict[str, Any]] = []
        for cand in raw_results:
            if cand.item_id == item_id:
                continue
            items.append(
                {"item_id": cand.item_id, "provider": cand.provider, "distance": cand.distance}
            )
            if len(items) >= limit:
                break
        return {
            "analyzed": True,
            "seed_track_id": item_id,
            "items": items,
        }

    # ------------------------------------------------------------------
    # Optional natural-language text search (lazy GPT2 text encoder)
    # ------------------------------------------------------------------

    async def _get_text_encoder(self) -> Any:
        """Return a CLAP wrapper with the text encoder loaded; lazy-load on first call.

        Re-entrancy is guarded by self._text_encoder_lock so that two concurrent
        first-callers can't both pay the ~30s download + load cost.
        """
        existing = self._text_encoder
        if existing is not None:
            return existing
        async with self._text_encoder_lock:
            existing = self._text_encoder
            if existing is not None:
                return existing
            try:
                self._text_encoder = await asyncio.to_thread(self._load_text_encoder)
                self.logger.info("CLAP text encoder loaded (lazy)")
            except Exception as err:
                self.logger.warning("CLAP text encoder load failed: %s", err)
                self._text_encoder = None
        return self._text_encoder

    @staticmethod
    def _load_text_encoder() -> Any:
        """Construct a CLAP wrapper with the GPT2 text encoder enabled.

        Runs on a worker thread (see _get_text_encoder). First call may block
        for tens of seconds while ~500MB of GPT2 weights download into the
        local HuggingFace cache; subsequent calls hit the cache.
        """
        from music_assistant.providers.sonic_analysis.vendored_clap import (  # noqa: PLC0415
            CLAP,
        )

        return CLAP(version="2023", use_cuda=False, text_enabled=True)

    async def _handle_text_search(
        self, query: str, limit: int = 25, resolve: bool = False
    ) -> dict[str, Any]:
        """Return tracks closest to a natural-language query in CLAP's joint space.

        :param query: Free-text query (e.g. "super dancy disco track").
        :param limit: Max matches to return.
        :param resolve: When True, include track name and artist for each item.
        """
        if self._clap_index is None or len(self._clap_index) == 0:
            return {
                "analyzed": False,
                "reason": "clap_index_empty",
                "query": query,
                "items": [],
            }
        emb_np = await self._embed_text_query(query)
        if emb_np is None:
            return {
                "analyzed": False,
                "reason": "text_encoder_unavailable",
                "query": query,
                "items": [],
            }
        matches = await self._clap_index.search(emb_np, limit)

        if not resolve:
            return {
                "analyzed": True,
                "query": query,
                "items": [
                    {
                        "provider": cand.provider,
                        "item_id": cand.item_id,
                        "distance": round(float(cand.distance), 4),
                    }
                    for cand in matches
                ],
            }

        async def _resolve(provider: str, item_id: str, distance: float) -> dict[str, Any]:
            entry: dict[str, Any] = {
                "provider": provider,
                "item_id": item_id,
                "distance": round(float(distance), 4),
                "name": "(unknown)",
                "artist": "",
            }
            try:
                track = await self.mass.music.tracks.get(item_id, provider)
                entry["name"] = track.name
                entry["artist"] = ", ".join(a.name for a in getattr(track, "artists", []) or [])
            except MusicAssistantError as err:
                self.logger.debug("Could not resolve %s/%s: %s", provider, item_id, err)
            return entry

        items = list(
            await asyncio.gather(
                *[_resolve(cand.provider, cand.item_id, cand.distance) for cand in matches]
            )
        )
        return {"analyzed": True, "query": query, "items": items}
