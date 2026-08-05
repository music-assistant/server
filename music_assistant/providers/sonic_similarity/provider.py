"""
SonicSimilarityPlugin — the provider class for the Sonic Similarity plugin.

See the package ``__init__.py`` docstring for an overview of the two
engines this plugin hosts (18-dim weighted-Euclidean + optional 1024-dim
CLAP cosine). This module contains only the provider class itself; the
framework-facing entry points (``setup``, ``get_config_entries``) live in
the package init.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import numpy as np
from music_assistant_models.auth import Scope
from music_assistant_models.background_task import TaskSchedule
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType, MediaType, ProviderFeature
from music_assistant_models.errors import MusicAssistantError, SetupFailedError
from music_assistant_models.media_items import (
    Album,
    BrowseFolder,
    ItemMapping,
    MediaItemType,
    RecommendationFolder,
    SearchResults,
)
from music_assistant_models.unique_list import UniqueList

from music_assistant.constants import DB_TABLE_AUDIO_ANALYSIS
from music_assistant.controllers.cache import use_cache
from music_assistant.controllers.streams.audio_analysis import SMART_FADES_ANALYSIS_DOMAIN
from music_assistant.models.plugin import PluginProvider
from music_assistant.providers.sonic_similarity.clap_index import ClapIndex
from music_assistant.providers.sonic_similarity.constants import (
    AA_PROVIDER_DOMAIN,
    ACTION_REBUILD_18DIM,
    ACTION_REBUILD_CLAP,
    CONF_DISCOVER_DIVERSITY,
    CONF_DISCOVER_ENGINE,
    CONF_DISCOVER_PRESET,
    CONF_ENABLE_CLAP_INDEX,
    CONF_ENABLE_DISCOVER_ROW,
    CONF_ENABLE_TEXT_SEARCH,
    CONF_LABEL_STATUS_18DIM,
    CONF_LABEL_STATUS_CLAP,
    CONF_LABEL_STATUS_TEXT,
    CONF_SIMILAR_DIVERSITY,
    CONF_SIMILAR_PRESET,
    CONF_SIMILAR_TRACKS_ENGINE,
    EXTRA_DATA_CLAP_EMBEDDING,
    METADATA_BONUS_SCALE,
    PERIODIC_REFRESH_INTERVAL_HOURS,
    PERIODIC_REFRESH_TASK_ID,
    RECOMMEND_ITEM_LIMIT,
    RECOMMEND_PER_SEED_LIMIT,
    RECOMMEND_SEED_COUNT,
    SIMILAR_ENGINE_18DIM,
    SIMILAR_ENGINE_CLAP,
    USEARCH_INDEX_FILENAME_GLOB,
    USEARCH_INDEX_FILENAME_TPL,
)
from music_assistant.providers.sonic_similarity.helpers import (
    _parse_clap_embedding,
    _parse_similar_params,
    _parse_weights,
    apply_filters,
    format_text_query,
)
from music_assistant.providers.sonic_similarity.index_io import save_index
from music_assistant.providers.sonic_similarity.models import SimilarParams, _SearchContext
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
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.media_items import Track
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant


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
        self._label_map: dict[int, tuple[str, str]] = {}
        # Keyed on (item_id, provider) to avoid cross-provider collisions;
        # _signatures_by_id is the item_id-only fallback (last-write-wins) for seed lookup.
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
        self._last_seen_row_count: int = 0

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to configure this provider."""
        status_18, status_clap, status_text = await self._collect_status_text()

        return (
            # === Generic: the two opt-in engine toggles ===
            ConfigEntry(
                key=CONF_ENABLE_CLAP_INDEX,
                type=ConfigEntryType.BOOLEAN,
                default_value=False,
            ),
            ConfigEntry(
                key=CONF_ENABLE_TEXT_SEARCH,
                type=ConfigEntryType.BOOLEAN,
                default_value=False,
            ),
            # === Similarity search: engine choice + 18-dim tuning ===
            ConfigEntry(
                key=CONF_SIMILAR_TRACKS_ENGINE,
                type=ConfigEntryType.STRING,
                default_value=SIMILAR_ENGINE_18DIM,
                options=[
                    ConfigValueOption(SIMILAR_ENGINE_18DIM),
                    ConfigValueOption(SIMILAR_ENGINE_CLAP),
                ],
                category="similarity_search",
                depends_on=CONF_ENABLE_CLAP_INDEX,
                depends_on_value=True,
            ),
            ConfigEntry(
                key=CONF_SIMILAR_PRESET,
                type=ConfigEntryType.STRING,
                default_value="balanced",
                options=[
                    ConfigValueOption("balanced"),
                    ConfigValueOption("vibe"),
                    ConfigValueOption("party"),
                    ConfigValueOption("genre_era"),
                    ConfigValueOption("discover"),
                ],
                category="similarity_search",
                depends_on=CONF_SIMILAR_TRACKS_ENGINE,
                depends_on_value=SIMILAR_ENGINE_18DIM,
            ),
            ConfigEntry(
                key=CONF_SIMILAR_DIVERSITY,
                type=ConfigEntryType.INTEGER,
                default_value=0,
                range=(0, 10),
                category="similarity_search",
                depends_on=CONF_SIMILAR_TRACKS_ENGINE,
                depends_on_value=SIMILAR_ENGINE_18DIM,
            ),
            # === Discover row ===
            ConfigEntry(
                key=CONF_ENABLE_DISCOVER_ROW,
                type=ConfigEntryType.BOOLEAN,
                default_value=True,
                category="discover",
            ),
            ConfigEntry(
                key=CONF_DISCOVER_ENGINE,
                type=ConfigEntryType.STRING,
                default_value=SIMILAR_ENGINE_18DIM,
                options=[
                    ConfigValueOption(SIMILAR_ENGINE_18DIM),
                    ConfigValueOption(SIMILAR_ENGINE_CLAP),
                ],
                category="discover",
                depends_on=CONF_ENABLE_DISCOVER_ROW,
                depends_on_value=True,
            ),
            ConfigEntry(
                key=CONF_DISCOVER_PRESET,
                type=ConfigEntryType.STRING,
                default_value="discover",
                options=[
                    ConfigValueOption("discover"),
                    ConfigValueOption("balanced"),
                    ConfigValueOption("vibe"),
                    ConfigValueOption("party"),
                    ConfigValueOption("genre_era"),
                ],
                category="discover",
                depends_on=CONF_DISCOVER_ENGINE,
                depends_on_value=SIMILAR_ENGINE_18DIM,
            ),
            ConfigEntry(
                key=CONF_DISCOVER_DIVERSITY,
                type=ConfigEntryType.INTEGER,
                default_value=2,
                range=(0, 10),
                category="discover",
                depends_on=CONF_DISCOVER_ENGINE,
                depends_on_value=SIMILAR_ENGINE_18DIM,
            ),
            # === Status: each rebuild (advanced) sits directly under its own status row ===
            ConfigEntry(
                key=CONF_LABEL_STATUS_18DIM,
                type=ConfigEntryType.LABEL,
                label=status_18,
                category="status",
            ),
            ConfigEntry(
                key=ACTION_REBUILD_18DIM,
                type=ConfigEntryType.ACTION,
                action=ACTION_REBUILD_18DIM,
                category="status",
                advanced=True,
                required=False,
            ),
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
                action=ACTION_REBUILD_CLAP,
                category="status",
                advanced=True,
                required=False,
                depends_on=CONF_ENABLE_CLAP_INDEX,
                depends_on_value=True,
            ),
            ConfigEntry(
                key=CONF_LABEL_STATUS_TEXT,
                type=ConfigEntryType.LABEL,
                label=status_text,
                category="status",
                depends_on=CONF_ENABLE_TEXT_SEARCH,
                depends_on_value=True,
            ),
        )

    async def handle_config_action(self, action: str) -> tuple[ConfigEntry, ...]:
        """Handle a rebuild button press (fire-and-forget) and re-render the entries."""
        if action == ACTION_REBUILD_18DIM:
            # per-engine locks in _safe_rebuild serialise double-clicks
            self.mass.create_task(self._safe_rebuild("Traits", self._rebuild_search_index))
            return await self.get_config_entries()
        if action == ACTION_REBUILD_CLAP:
            if self._clap_index is not None:
                self.mass.create_task(
                    self._safe_rebuild("Character", self._rebuild_clap_index_from_database)
                )
            return await self.get_config_entries()
        return await super().handle_config_action(action)

    async def handle_async_init(self) -> None:
        """
        Build the 18-dim search index before the provider is registered.

        Failures here raise SetupFailedError so the loader surfaces them through
        MA's standard provider-failure UI (a silent failure in loaded_in_mass
        would be swallowed by its fire-and-forget task wrapper).
        """
        self.logger.info("Sonic Similarity initializing, building search index...")
        try:
            await self._rebuild_search_index()
        except Exception as err:
            msg = f"Failed to build Traits search index: {err}"
            raise SetupFailedError(msg) from err
        self.logger.info(
            "Search index ready: %d signatures cached, corpus_stats=%s",
            len(self._signature_cache),
            self.corpus_means is not None,
        )

    async def loaded_in_mass(self) -> None:
        """Register similarity API commands and set up the optional CLAP engine."""
        self._unregister_handles.append(
            self.mass.register_api_command(
                "sonic_similarity/similar", self._handle_similar, required_scope=Scope.LIBRARY_READ
            )
        )
        self._unregister_handles.append(
            self.mass.register_api_command(
                "sonic_similarity/status", self._handle_status, required_scope=Scope.LIBRARY_READ
            )
        )
        self._unregister_handles.append(
            self.mass.register_api_command(
                "sonic_similarity/rebuild_index",
                self._handle_rebuild_index,
                required_scope=Scope.LIBRARY_MANAGE,
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
                        "sonic_similarity/similar_clap",
                        self._handle_similar_clap,
                        required_scope=Scope.LIBRARY_READ,
                    )
                )
                await self._rebuild_clap_index_from_database()
                self.logger.info("Character index ready: %d embeddings", len(self._clap_index))
            except Exception:
                # CLAP is optional — failure must not block the 18-dim engine.
                self.logger.exception(
                    "Character index setup failed; Character engine will be unavailable"
                )
                self._clap_index = None

        if text_search_enabled:
            self._unregister_handles.append(
                self.mass.register_api_command(
                    "sonic_similarity/text_search",
                    self._handle_text_search,
                    required_scope=Scope.LIBRARY_READ,
                )
            )
            # The ~500MB GPT2 text encoder loads lazily on the first query (see search()
            # and _handle_text_search), not at plugin start.
            self.logger.info("Text search enabled (encoder loads on first query)")

        self.mass.tasks.register_scheduled_task(
            task_id=PERIODIC_REFRESH_TASK_ID,
            name="Sonic Similarity — periodic index refresh",
            handler=self._periodic_refresh,
            schedule=TaskSchedule.hourly(every=PERIODIC_REFRESH_INTERVAL_HOURS),
            allow_retry=True,
        )

    async def unload(self, is_removed: bool = False) -> None:
        """Unregister API commands; delete on-disk indexes when the provider is uninstalled."""
        for unregister in self._unregister_handles:
            unregister()
        self._unregister_handles.clear()
        try:
            self.mass.tasks.unregister_scheduled_task(PERIODIC_REFRESH_TASK_ID)
        except Exception as err:
            self.logger.debug("Periodic refresh task unregister failed: %s", err)
        if self._clap_index is not None:
            try:
                await self._clap_index.close()
            except Exception as err:
                self.logger.debug("Character index close failed: %s", err)
            self._clap_index = None
        # Drop encoder ref so its (large) tensors can be GC'd.
        self._text_encoder = None
        if is_removed:
            self._search_index = None
            await asyncio.to_thread(self._delete_all_index_files)
        await super().unload(is_removed)

    # ------------------------------------------------------------------
    # Cross-provider SIMILAR_TRACKS hook (PluginProvider feature surface)
    # ------------------------------------------------------------------

    async def get_similar_tracks(self, track: Track, limit: int = 25) -> list[Track]:
        """
        Implement ProviderFeature.SIMILAR_TRACKS via the configured engine.

        Routes to the 1024-dim CLAP engine when it is selected and its index
        loaded, otherwise the 18-dim weighted-Euclidean engine. There is no
        cross-engine fallback: the chosen engine returns [] for a track it
        cannot serve, which is interchangeable to the cross-provider
        dispatcher's truthy check.

        :param track: Full Track object (with provider_mappings) as
            handed to us by the cross-provider dispatcher.
        :param limit: Max number of similar tracks to return.
        """
        engine = str(self.config.get_value(CONF_SIMILAR_TRACKS_ENGINE) or SIMILAR_ENGINE_18DIM)
        if engine == SIMILAR_ENGINE_CLAP and self._clap_index is not None:
            return await self._similar_tracks_via_clap(track, limit)
        if engine == SIMILAR_ENGINE_CLAP:
            self.logger.debug(
                "Similar Tracks for %s: Character engine selected but index unavailable; "
                "using Traits",
                track.uri,
            )
        return await self._similar_tracks_via_18dim(track, limit)

    async def get_recommendations(self) -> list[RecommendationFolder]:
        """
        Get the 'Inspired by recently played' row descriptor, without items.

        Returns [] when the discover row is disabled in the plugin config.
        """
        if not bool(self.config.get_value(CONF_ENABLE_DISCOVER_ROW)):
            return []
        return [
            RecommendationFolder(
                item_id="inspired_by_recently_played",
                provider=self.instance_id,
                name="Inspired by recently played",
                translation_key="inspired_by_recently_played",
                icon="mdi-shimmer",
            ),
        ]

    async def get_recommendation_items(
        self, item_id: str
    ) -> UniqueList[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Get the items for the 'Inspired by recently played' row.

        Returns an empty list for an unknown row, when the row is disabled,
        when the engine isn't ready or when no recent tracks intersect the
        index.

        :param item_id: The item_id of the row, as returned by get_recommendations.
        """
        if item_id != "inspired_by_recently_played":
            return UniqueList()
        folder = await self._get_inspired_recommendations()
        if folder is None:
            return UniqueList()
        return folder.items

    @use_cache(60, base_class=RecommendationFolder, allow_expired_cache=True)
    async def _get_inspired_recommendations(self) -> RecommendationFolder | None:
        """
        Build the 'Inspired by recently played' folder with its items.

        Returns None when the row is disabled, the engine isn't ready or no
        recent tracks intersect the index. A None result is still cached
        (a negative hit), matching the previous behavior of caching the
        empty result for the same TTL.
        """
        if not bool(self.config.get_value(CONF_ENABLE_DISCOVER_ROW)):
            return None

        discover_engine = str(self.config.get_value(CONF_DISCOVER_ENGINE) or SIMILAR_ENGINE_18DIM)
        use_clap = discover_engine == SIMILAR_ENGINE_CLAP and self._clap_index is not None
        if not use_clap and (self.corpus_means is None or not self._signature_cache):
            return None

        try:
            recent = await self.mass.music.recently_played(
                limit=RECOMMEND_SEED_COUNT,
                media_types=[MediaType.TRACK],
                fully_played_only=False,
            )
        except Exception as err:
            self.logger.debug("recently_played failed: %s", err)
            return None
        if not recent:
            return None

        # Walk each recent mapping into a seed item_id our index has analysed.
        # The mapping is library-aggregated; we need the underlying streaming
        # provider's id (or the filesystem path).
        seeds: list[tuple[str, str | None]] = []
        seen_seeds: set[str] = set()
        for mapping in recent:
            try:
                # Structural walk — we read provider_mappings to find an
                # indexed seed; skip the metadata refresh side effect.
                track = await self.mass.music.tracks.get(
                    mapping.item_id, mapping.provider, allow_update_metadata=False
                )
            except MusicAssistantError:
                continue
            seed_id, seed_provider = self._find_discover_seed(track, use_clap)
            if seed_id and seed_id not in seen_seeds:
                seeds.append((seed_id, seed_provider))
                seen_seeds.add(seed_id)

        if not seeds:
            return None

        preset = str(self.config.get_value(CONF_DISCOVER_PRESET) or "discover")
        # The slider is a 0-10 integer; the engine wants a 0.0-1.0 weight.
        try:
            diversity = int(str(self.config.get_value(CONF_DISCOVER_DIVERSITY) or 0)) / 10.0
        except TypeError, ValueError:
            diversity = 0.0
        self.logger.debug(
            "Discover row: engine=%s seeds=%d preset=%s diversity=%s",
            "Character" if use_clap else "Traits",
            len(seeds),
            preset,
            diversity,
        )

        # Fan out per seed; union results, first-occurrence wins (we already
        # ordered seeds by recency above, so earlier seeds get priority).
        candidate_order: list[tuple[str, str]] = []
        candidate_seen: set[tuple[str, str]] = set()
        for sid, sprov in seeds:
            if use_clap:
                response = await self._handle_similar_clap(
                    item_id=sid, limit=RECOMMEND_PER_SEED_LIMIT, seed_provider=sprov
                )
            else:
                response = await self._handle_similar(
                    item_id=sid,
                    seed_provider=sprov,
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
            return None

        async def _resolve(provider: str, item_id: str) -> Track | None:
            try:
                return await self.mass.music.tracks.get(item_id, provider)
            except MusicAssistantError:
                return None

        resolved = await asyncio.gather(*[_resolve(p, i) for p, i in candidate_order])
        items: UniqueList[MediaItemType | ItemMapping | BrowseFolder] = UniqueList(
            [t for t in resolved if t is not None]
        )
        return RecommendationFolder(
            item_id="inspired_by_recently_played",
            provider=self.instance_id,
            name="Inspired by recently played",
            translation_key="inspired_by_recently_played",
            icon="mdi-shimmer",
            items=items,
        )

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
        # Never hold up the timeout-less global SEARCH gather on the ~500MB encoder load:
        # warm it in the background and short-circuit until it is ready. create_task dedupes
        # on task_id while a load is in flight, and re-attempts after a previous load failed.
        if self._text_encoder is None:
            self.mass.create_task(
                self._get_text_encoder, task_id="sonic_similarity_text_encoder_warm"
            )
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

    async def _collect_status_text(self) -> tuple[str, str, str]:
        """
        Return (18-dim, CLAP, text-encoder) label-text triples for the plugin page.

        Each string is single-line, safe to render in a LABEL config entry, and
        degrades gracefully depending on the current engine/index state.
        """
        eighteen = "Traits engine: not yet loaded"
        clap = "Character engine: disabled"
        text = "Text encoder: disabled"

        # Optional coverage lookup against the upstream AA provider via #3851's API.
        coverage_pct: float | None = None
        try:
            coverage = await self.mass.streams.audio_analysis.get_coverage(AA_PROVIDER_DOMAIN)
        except Exception:
            coverage = None
        if coverage is not None:
            total = coverage.analyzed + coverage.pending
            if total > 0:
                coverage_pct = round(100.0 * coverage.analyzed / total, 1)

        # 18-dim line.
        index_size = len(self._search_index) if self._search_index is not None else 0
        parts = [
            f"{index_size:,} tracks indexed",
            f"{len(self._signature_cache):,} signatures cached",
            f"corpus stats {'ready' if self.corpus_means is not None else 'pending'}",
        ]
        if coverage_pct is not None:
            parts.append(f"{coverage_pct}% coverage")
        eighteen = "Traits engine: " + " · ".join(parts)
        if (err_18dim := self._last_rebuild_error.get("Traits")) is not None:
            eighteen += f" — last rebuild failed: {err_18dim}"

        # CLAP line (only meaningful when the index is built).
        if self._clap_index is not None:
            clap_size = len(self._clap_index)
            clap_parts = [f"{clap_size:,} embeddings indexed"]
            if coverage_pct is not None:
                clap_parts.append(f"{coverage_pct}% coverage")
            clap = "Character engine: " + " · ".join(clap_parts)
            if (err_clap := self._last_rebuild_error.get("Character")) is not None:
                clap += f" — last rebuild failed: {err_clap}"

        # Text-encoder line - encoder state is independent of the index.
        if bool(self.config.get_value(CONF_ENABLE_TEXT_SEARCH)):
            if self._text_encoder is not None:
                text = "Text encoder: loaded (warm)"
            else:
                text = "Text encoder: cold (downloads on first query, ~500MB)"

        return eighteen, clap, text

    async def _safe_rebuild(self, label: str, rebuild_fn: Callable[[], Awaitable[None]]) -> None:
        """
        Run a rebuild fn from a background task, swallowing failures into status state.

        :param label: Engine label used as the status-row error key (e.g. "Traits", "Character").
        :param rebuild_fn: Zero-arg coroutine-returning callable to execute.
        """
        try:
            await rebuild_fn()
            self._last_rebuild_error.pop(label, None)
        except Exception as err:
            self.logger.exception("%s rebuild failed", label)
            self._last_rebuild_error[label] = str(err)

    async def _count_analysis_rows(self) -> int:
        """Return the current count of sonic_analysis track rows in the database."""
        return await self.mass.music.database.get_count_from_query(
            f"SELECT 1 FROM {DB_TABLE_AUDIO_ANALYSIS} "
            "WHERE aa_provider_domain = :aa_provider_domain AND media_type = :media_type",
            {"aa_provider_domain": AA_PROVIDER_DOMAIN, "media_type": MediaType.TRACK.value},
        )

    async def _periodic_refresh(self) -> None:
        """Scheduled-task handler: rebuild indexes when the analysis row count changed."""
        try:
            current = await self._count_analysis_rows()
        except Exception:
            self.logger.exception("Periodic refresh: row count query failed; skipping")
            return
        if current == self._last_seen_row_count:
            return
        self.logger.info(
            "Periodic refresh: analysis row count changed %d → %d, rebuilding indexes",
            self._last_seen_row_count,
            current,
        )
        await self._safe_rebuild("Traits", self._rebuild_search_index)
        if self._clap_index is not None:
            await self._safe_rebuild("Character", self._rebuild_clap_index_from_database)

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
        # Candidate pool size for the weighted rerank (large enough for the aggressive presets).
        candidates: int = 200,
        filter_genres: list[str] | None = None,
        filter_providers: list[str] | None = None,
        exclude_track_ids: list[str] | None = None,
        exclude_artists: list[str] | None = None,
        resolve: bool = False,
        include_group_distances: bool = False,
        seed_provider: str | None = None,
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
            seed_provider=seed_provider,
            **kwargs,
        )
        weights = _parse_weights({**params.weight_overrides, "preset": params.preset})

        seed_sigs, valid_seed_ids = self._lookup_seed_signatures(
            params.item_ids, params.seed_provider
        )
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

    def _lookup_seed_signatures(
        self, item_ids: list[str], seed_provider: str | None = None
    ) -> tuple[list[list[float]], list[str]]:
        """
        Look up signatures by item_id; warn on misses; return (sigs, valid_ids) in input order.

        :param item_ids: Seed item ids to resolve.
        :param seed_provider: When set, disambiguates id collisions via the
            authoritative (item_id, provider) cache; falls back to the
            item_id-only cache (last-write-wins) when None.
        """
        seed_sigs: list[list[float]] = []
        valid_seed_ids: list[str] = []
        for sid in item_ids:
            sig: list[float] | None = None
            if seed_provider is not None:
                sig = self._signature_cache.get((sid, seed_provider))
            if sig is None:
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
        needs_filter = bool(ctx.params.filter_genres or ctx.params.exclude_artists)
        needs_rerank = ctx.weights.get("genre", 0.0) > 0 or ctx.weights.get("era", 0.0) > 0
        if not (needs_filter or needs_rerank):
            return candidates
        # Resolve each candidate's track once; filter and rerank share this map
        # so a filtered call no longer resolves its survivors twice.
        resolved = await self._resolve_candidate_track_map(candidates)
        if needs_filter:
            candidates = await self._apply_metadata_filters(
                candidates,
                resolved,
                filter_genres=ctx.params.filter_genres,
                exclude_artists=ctx.params.exclude_artists,
            )
        if needs_rerank:
            candidates = await self._apply_metadata_reranking(
                ctx.valid_seed_ids, candidates, ctx.weights, resolved
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

    async def _similar_tracks_via_18dim(self, track: Track, limit: int) -> list[Track]:
        """Serve SIMILAR_TRACKS from the 18-dim weighted-Euclidean engine."""
        if self.corpus_means is None or not self._signature_cache:
            return []

        # Pick the first provider mapping that's actually indexed. Skip the
        # library aggregator since audio_analysis is keyed on the streaming
        # provider's item_id, not on library numeric ids.
        seed_item_id: str | None = None
        seed_provider: str | None = None
        for mapping in track.provider_mappings or ():
            if mapping.provider_domain == "library":
                continue
            if (mapping.item_id, mapping.provider_instance) in self._signature_cache:
                seed_item_id = mapping.item_id
                seed_provider = mapping.provider_instance
                break
            if mapping.item_id in self._signatures_by_id:
                seed_item_id = mapping.item_id
                break
        if seed_item_id is None:
            self.logger.debug("Traits Similar Tracks for %s: no indexed seed mapping", track.uri)
            return []

        preset = str(self.config.get_value(CONF_SIMILAR_PRESET) or "balanced")
        # The slider is a 0-10 integer; the engine wants a 0.0-1.0 weight.
        try:
            diversity = int(str(self.config.get_value(CONF_SIMILAR_DIVERSITY) or 0)) / 10.0
        except TypeError, ValueError:
            diversity = 0.0
        self.logger.debug(
            "Traits Similar Tracks: seed=%s/%s preset=%s diversity=%s limit=%d",
            seed_provider or "?",
            seed_item_id,
            preset,
            diversity,
            limit,
        )
        response = await self._handle_similar(
            item_id=seed_item_id,
            seed_provider=seed_provider,
            limit=limit,
            preset=preset,
            diversity=diversity,
        )
        results = await self._resolve_similar_items(response.get("items") or [])
        self.logger.debug("Traits Similar Tracks: returning %d tracks", len(results))
        return results

    async def _similar_tracks_via_clap(self, track: Track, limit: int) -> list[Track]:
        """Serve SIMILAR_TRACKS from the 1024-dim CLAP index; [] if the seed isn't indexed."""
        if self._clap_index is None:
            return []
        seed_item_id: str | None = None
        seed_provider: str | None = None
        for mapping in track.provider_mappings or ():
            if mapping.provider_domain == "library":
                continue
            if self._clap_index.contains(mapping.provider_instance, mapping.item_id):
                seed_item_id = mapping.item_id
                seed_provider = mapping.provider_instance
                break
        if seed_item_id is None:
            self.logger.debug("Character Similar Tracks for %s: no indexed seed mapping", track.uri)
            return []
        self.logger.debug(
            "Character Similar Tracks: seed=%s/%s limit=%d", seed_provider, seed_item_id, limit
        )
        response = await self._handle_similar_clap(
            item_id=seed_item_id, limit=limit, seed_provider=seed_provider
        )
        results = await self._resolve_similar_items(response.get("items") or [])
        self.logger.debug("Character Similar Tracks: returning %d tracks", len(results))
        return results

    async def _resolve_similar_items(self, items: list[dict[str, Any]]) -> list[Track]:
        """Resolve [{item_id, provider}, …] response rows to Tracks, dropping lookup misses."""

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

    def _find_discover_seed(self, track: Track, use_clap: bool) -> tuple[str | None, str | None]:
        """
        Return (seed_id, seed_provider) for the first mapping the chosen engine indexes.

        :param track: Recently-played track to walk into an indexed seed.
        :param use_clap: Test CLAP-index membership when True, else the 18-dim
            signature caches. The 18-dim by-id fallback returns provider None.
        """
        for pm in track.provider_mappings or ():
            if pm.provider_domain == "library":
                continue
            if use_clap:
                if self._clap_index is not None and self._clap_index.contains(
                    pm.provider_instance, pm.item_id
                ):
                    return pm.item_id, pm.provider_instance
            else:
                if (pm.item_id, pm.provider_instance) in self._signature_cache:
                    return pm.item_id, pm.provider_instance
                if pm.item_id in self._signatures_by_id:
                    return pm.item_id, None
        return None, None

    async def _embed_text_query(
        self, query: str, exclude: str | None = None, exclude_weight: float = 1.0
    ) -> np.ndarray | None:
        """
        Encode a free-text query as a unit vector, or None when unusable.

        Returns None when the encoder is unavailable, the query is empty, or the
        embedding is degenerate (zero norm, which the cosine index cannot rank) —
        including when ``exclude`` cancels ``query``.

        :param query: Free-text query.
        :param exclude: Optional text to steer the query embedding away from.
        :param exclude_weight: Strength of the exclusion.
        """
        encoder = await self._get_text_encoder()
        if encoder is None:
            return None
        keep_text = format_text_query(query)
        if not keep_text:
            return None
        # CLAP ignores literal negation ("not loud" ~= "loud"), so exclusion is a
        # vector subtraction. Each side is unit-normalised first so the exclude term
        # steers by direction, not by raw magnitude.
        exclude_text = format_text_query(exclude) if exclude else ""
        prompts = [keep_text, exclude_text] if exclude_text else [keep_text]
        embeddings = await asyncio.to_thread(encoder.get_text_embeddings, prompts)

        def _unit(index: int) -> np.ndarray | None:
            vec = embeddings[index].detach().cpu().numpy().astype(np.float32).reshape(-1)
            norm = float(np.linalg.norm(vec))
            return cast("np.ndarray", vec / norm) if norm else None

        keep = _unit(0)
        if keep is None:
            return None
        if not exclude_text:
            return keep
        neg = _unit(1)
        if neg is None:
            return keep
        result = keep - exclude_weight * neg
        norm = float(np.linalg.norm(result))
        return cast("np.ndarray", result / norm) if norm else None

    async def _handle_status(self) -> dict[str, Any]:
        """Return current analysis status."""
        index_size = len(self._search_index) if self._search_index is not None else 0
        text_search_enabled = bool(self.config.get_value(CONF_ENABLE_TEXT_SEARCH))
        status: dict[str, Any] = {
            "index_size": index_size,
            "has_corpus_stats": self.corpus_means is not None,
            "cached_signatures": len(self._signature_cache),
            "aa_provider_domain": AA_PROVIDER_DOMAIN,
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
        """List on-disk index files for the 18-dim engine, oldest first."""
        storage = Path(self.mass.storage_path)
        pattern = USEARCH_INDEX_FILENAME_GLOB.format(domain=AA_PROVIDER_DOMAIN)
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
        """
        Search the index for the k nearest neighbors.

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
        # Snapshot the count BEFORE the rebuild: SELECT is snapshot-isolated, so rows
        # added mid-rebuild are missed — counting after would skip the next refresh.
        try:
            pre_count = await self._count_analysis_rows()
        except Exception:
            self.logger.debug("Pre-rebuild row count failed; counter unchanged", exc_info=True)
            pre_count = None
        async with self._rebuild_lock:
            await self._rebuild_search_index_locked()
        if pre_count is not None:
            self._last_seen_row_count = pre_count

    async def _rebuild_search_index_locked(self) -> None:  # noqa: PLR0915
        """Rebuild body — assumes self._rebuild_lock is held."""
        # Controller streams the cross-AA-provider merge (latest non-None write wins
        # per field); rows from currently-unavailable AA providers are skipped.

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
        ) in self.mass.streams.audio_analysis.iter_merged_audio_analysis_rows(
            AA_PROVIDER_DOMAIN,
            # similarity vectors need sonic's own RMS loudness feature (not the EBU R128
            # value from loudness_analysis) plus smart_fades' bpm/key/mode; sonic wins
            # the shared loudness_integrated/loudness_range fields.
            priority=(AA_PROVIDER_DOMAIN, SMART_FADES_ANALYSIS_DOMAIN),
        ):
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
                "No valid signatures assembled from %d merged tracks, skipping index "
                "rebuild. Sample diagnostics: %s. Common cause: sonic_analysis hasn't "
                "yet populated the required scalar fields for these tracks — verify "
                "analysis has finished.",
                total_merged_rows,
                "; ".join(missing_report),
            )
            return

        new_corpus_means, new_corpus_stds = compute_corpus_stats(all_features)

        # Each rebuild writes a NEW versioned file so the previous viewer's mmap
        # is never disturbed. After the atomic swap the old file gets cleaned up.
        new_index_path = Path(self.mass.storage_path) / USEARCH_INDEX_FILENAME_TPL.format(
            domain=AA_PROVIDER_DOMAIN, version=int(time.time() * 1000)
        )

        def _build_save_and_view() -> Any:
            builder = self._make_empty_index()
            for label, features in row_entries:
                normalized = normalize_features(features, new_corpus_means, new_corpus_stds)
                builder.add(label, np.array(normalized, dtype=np.float32))
            save_index(builder, new_index_path)
            viewer = self._make_empty_index()
            viewer.view(str(new_index_path))
            return viewer

        new_viewer = await asyncio.to_thread(_build_save_and_view)

        # Atomic swap: no `await` between these writes, so queries see fully old or
        # fully new state, never a half-rotated mix.
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

    async def _resolve_candidate_track_map(
        self, candidates: list[Candidate]
    ) -> dict[tuple[str, str], Track]:
        """
        Resolve the library Track for each candidate, keyed by (item_id, provider).

        Candidates with no library match are omitted; a missing key means
        unresolved (no rerank bonus, and dropped when filtering).

        :param candidates: The ANN candidates to resolve for metadata scoring.
        """
        # Scoring reads only genres/artists/year, so resolve library items in one
        # query per provider rather than a tracks.get() per candidate. Stay
        # library-only: sonic_analysis also indexes streamed-but-not-added tracks,
        # and resolving those via the provider would burst a rate-limited API for a
        # soft ranking bonus, so non-library candidates are left unresolved.
        # Dedupe (item_id, provider) and group item ids by provider for one query each.
        ids_by_provider: dict[str, list[str]] = {}
        seen: set[tuple[str, str]] = set()
        for cand in candidates:
            key = (cand.item_id, cand.provider)
            if key in seen:
                continue
            seen.add(key)
            ids_by_provider.setdefault(cand.provider, []).append(cand.item_id)

        resolved: dict[tuple[str, str], Track] = {}
        for provider, item_ids in ids_by_provider.items():
            library_tracks = await self.mass.music.tracks.get_library_items_by_prov_id(
                provider_instance_id_or_domain=provider,
                provider_item_ids=item_ids,
                limit=len(item_ids),
            )
            for track in library_tracks:
                for mapping in track.provider_mappings:
                    if provider in (mapping.provider_instance, mapping.provider_domain):
                        resolved[(mapping.item_id, provider)] = track

        return resolved

    async def _apply_metadata_filters(
        self,
        results: list[Candidate],
        resolved: dict[tuple[str, str], Track],
        filter_genres: list[str] | None = None,
        exclude_artists: list[str] | None = None,
    ) -> list[Candidate]:
        """Apply metadata-based filters that require track resolution."""
        if not filter_genres and not exclude_artists:
            return results

        genre_set = {g.lower() for g in filter_genres} if filter_genres else None
        artist_set = {a.lower() for a in exclude_artists} if exclude_artists else None

        filtered: list[Candidate] = []
        for cand in results:
            track = resolved.get((cand.item_id, cand.provider))
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
        resolved: dict[tuple[str, str], Track],
    ) -> list[Candidate]:
        """
        Apply genre and year bonuses to re-rank candidates.

        :param seed_item_ids: All seed track ids — genres are unioned across
            seeds, year is averaged. With one seed this collapses to the
            single-seed behavior; with N seeds the metadata bonus reflects
            the centroid of the seed set, matching how the audio-distance
            blend already works.
        :param resolved: Bulk-resolved candidate library tracks keyed by
            (item_id, provider); a missing key marks an unresolved candidate,
            which receives no bonus.
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
        for cand in results:
            cand_track = resolved.get((cand.item_id, cand.provider))
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
        """
        Resolve a seed track from its item_id, falling back to the 'library' provider.

        Used by metadata rerank to read seed genres/year — scoring, not display.
        """
        seed_prov = self._provider_by_item_id.get(seed_item_id, "library")
        try:
            return await self.mass.music.tracks.get(
                seed_item_id, seed_prov, allow_update_metadata=False
            )
        except MusicAssistantError as err:
            self.logger.debug("Could not resolve seed %s/%s: %s", seed_prov, seed_item_id, err)
            return None

    async def _resolve_results(
        self,
        items: list[tuple[str, str, float, int]],
        debug_breakdown_map: dict[str, dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """
        Resolve track metadata for result items.

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
        """
        Add any audio_analysis rows with clap_embedding that aren't yet indexed.

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
                AA_PROVIDER_DOMAIN
            ):
                key = (row["provider"], row["item_id"])
                if key in seen:
                    continue
                seen.add(key)
                if self._clap_index.contains(row["provider"], row["item_id"]):
                    continue
                try:
                    raw = json.loads(row["analysis_data"])
                except json.JSONDecodeError, TypeError:
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
                        "Add to Character index failed for %s/%s: %s",
                        row["provider"],
                        row["item_id"],
                        err,
                    )
            if added > 0:
                await self._clap_index.save()
                self.logger.info("Added %d new embeddings to Character index", added)

    async def _handle_similar_clap(
        self, item_id: str, limit: int = 25, seed_provider: str | None = None
    ) -> dict[str, Any]:
        """
        Return tracks whose CLAP audio embedding is closest to the seed track's.

        :param item_id: Seed track identifier.
        :param limit: Max number of neighbours to return.
        :param seed_provider: When set, the seed embedding is fetched in O(1) via
            its derived label; when None, the reverse map is scanned by item_id.
        """
        if self._clap_index is None:
            return {
                "analyzed": False,
                "reason": "clap_index_disabled",
                "seed_track_id": item_id,
                "items": [],
            }
        if seed_provider is not None:
            seed_embedding = self._clap_index.get_embedding(seed_provider, item_id)
        else:
            lookup = self._clap_index.get_embedding_by_item_id(item_id)
            seed_embedding = lookup[1] if lookup is not None else None
        if seed_embedding is None:
            return {
                "analyzed": False,
                "reason": "seed_not_in_index",
                "seed_track_id": item_id,
                "items": [],
            }
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
        """
        Return a CLAP wrapper with the text encoder loaded; lazy-load on first call.

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
        """
        Construct a CLAP wrapper with the GPT2 text encoder enabled.

        Runs on a worker thread (see _get_text_encoder). First call may block
        for tens of seconds while ~500MB of GPT2 weights download into the
        local HuggingFace cache; subsequent calls hit the cache.
        """
        from music_assistant.providers.sonic_analysis.vendored_clap import (  # noqa: PLC0415
            CLAP,
        )

        return CLAP(version="2023", use_cuda=False, text_enabled=True)

    async def _handle_text_search(
        self,
        query: str,
        exclude: str | None = None,
        exclude_weight: float = 1.0,
        limit: int = 25,
        resolve: bool = False,
    ) -> dict[str, Any]:
        """
        Return tracks closest to a natural-language query in CLAP's joint space.

        :param query: Free-text query (e.g. "super dancy disco track").
        :param exclude: Optional text to steer results away from (e.g. "vocals").
        :param exclude_weight: Strength of the exclusion, clamped to [0, 2].
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
        if not format_text_query(query):
            return {
                "analyzed": False,
                "reason": "empty_query",
                "query": query,
                "items": [],
            }
        exclude_weight = max(0.0, min(2.0, exclude_weight))
        emb_np = await self._embed_text_query(query, exclude=exclude, exclude_weight=exclude_weight)
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
