"""Sonic Analysis plugin provider."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import librosa
import numpy as np
from aiohttp import web
from music_assistant_models.config_entries import ConfigEntry, ConfigValueType
from music_assistant_models.enums import ConfigEntryType, EventType, MediaType, ProviderFeature
from music_assistant_models.media_items import Track
from music_assistant_models.playback_progress_report import MediaItemPlaybackProgressReport

from music_assistant.constants import DB_TABLE_SONIC_SIGNATURES
from music_assistant.controllers.tasks.context import (
    report_current_task_failure,
    update_current_task_progress_from_index,
    update_current_task_progress_text,
)
from music_assistant.helpers.sonic_analysis import (
    SIGNATURE_DIMENSIONS,
    SIGNATURE_VERSION,
    SonicSignature,
    compute_corpus_stats,
    extract_signature,
    normalize_features,
)
from music_assistant.helpers.uri import parse_uri
from music_assistant.models.plugin import PluginProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.event import MassEvent
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES: set[ProviderFeature] = set()

USEARCH_INDEX_FILENAME = "sonic_signatures.usearch"
CORPUS_STATS_ITEM_ID = "__corpus_stats__"

CONF_ANALYZE_ON_PLAY = "analyze_on_play"
CONF_ANALYZE_ON_SYNC = "analyze_on_sync"
CONF_MAX_CONCURRENT_ANALYSES = "max_concurrent_analyses"

_usearch_index_module: Any = None
_USEARCH_AVAILABLE = False
USearchIndex: Any = None
MetricKind: Any = None
ScalarKind: Any = None

try:
    import usearch.index as _usearch_index_module

    USearchIndex = _usearch_index_module.Index
    MetricKind = _usearch_index_module.MetricKind
    ScalarKind = _usearch_index_module.ScalarKind
    _USEARCH_AVAILABLE = True
except ImportError:
    pass


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return SonicAnalysisProvider(mass, manifest, config, SUPPORTED_FEATURES)


async def get_config_entries(
    mass: MusicAssistant,  # noqa: ARG001
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,  # noqa: ARG001
    values: dict[str, ConfigValueType] | None = None,  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider.

    :param mass: The MusicAssistant instance.
    :param instance_id: ID of an existing provider instance (None if new instance setup).
    :param action: Optional action key called from config entries UI.
    :param values: Intermediate raw values for config entries sent with the action.
    """
    return (
        ConfigEntry(
            key=CONF_ANALYZE_ON_PLAY,
            type=ConfigEntryType.BOOLEAN,
            label="Analyze on play",
            description="Automatically extract a sonic signature when a track is played.",
            default_value=True,
        ),
        ConfigEntry(
            key=CONF_ANALYZE_ON_SYNC,
            type=ConfigEntryType.BOOLEAN,
            label="Analyze on sync",
            description="Automatically extract sonic signatures during library sync.",
            default_value=True,
        ),
        ConfigEntry(
            key=CONF_MAX_CONCURRENT_ANALYSES,
            type=ConfigEntryType.INTEGER,
            label="Max concurrent analyses",
            description="Maximum number of tracks analysed in parallel.",
            default_value=2,
            required=True,
        ),
    )


_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type, Authorization",
}


def _cors_json(data: Any, status: int = 200) -> web.Response:
    """Return a JSON response with CORS headers for debug tooling."""
    resp = web.json_response(data, status=status)
    resp.headers.update(_CORS_HEADERS)
    return resp


def _cors_text(text: str, status: int = 200) -> web.Response:
    """Return a text response with CORS headers for debug tooling."""
    resp = web.Response(status=status, text=text)
    resp.headers.update(_CORS_HEADERS)
    return resp


class SonicAnalysisProvider(PluginProvider):
    """Plugin provider that extracts sonic signatures and enables similarity-based discovery."""

    corpus_means: list[float] | None
    corpus_stds: list[float] | None
    _on_unload: list[Callable[[], None]]
    _search_index: Any  # USearchIndex — typed as Any to avoid hard import at class level
    _label_map: dict[int, tuple[str, str]]
    _reverse_label_map: dict[tuple[str, str], int]
    _next_label: int
    _analysis_semaphore: asyncio.Semaphore

    async def handle_async_init(self) -> None:
        """Handle async initialisation: create DB table, load corpus stats, init ANN index."""
        self._on_unload = []
        self.corpus_means = None
        self.corpus_stds = None
        self._search_index = None
        self._label_map: dict[int, tuple[str, str]] = {}
        self._reverse_label_map: dict[tuple[str, str], int] = {}
        self._next_label = 1

        max_concurrent = int(self.config.get_value(CONF_MAX_CONCURRENT_ANALYSES) or 2)  # type: ignore[arg-type]
        self._analysis_semaphore = asyncio.Semaphore(max_concurrent)

        await self._create_db_table()
        await self._load_corpus_stats()
        self._init_search_index()
        await self._restore_label_maps()

    async def _restore_label_maps(self) -> None:
        """Rebuild the in-memory label maps from stored signatures.

        The USearch index is persisted to disk and loaded by _init_search_index,
        but the label-to-track mappings are memory-only. This method restores
        them from the DB so that similarity queries can resolve results
        immediately after startup without a full index rebuild.
        """
        assert self.mass.music.database is not None
        try:
            rows = await self.mass.music.database.get_rows(
                DB_TABLE_SONIC_SIGNATURES, match=None, limit=0
            )
        except Exception:
            return

        for row in rows:
            if row["item_id"] == CORPUS_STATS_ITEM_ID:
                continue
            self._get_or_assign_label(row["item_id"], row["provider"])

        self.logger.info(
            "Restored %d label mappings from DB", len(self._label_map)
        )

    async def loaded_in_mass(self) -> None:
        """Subscribe to library and playback events based on configuration."""
        await super().loaded_in_mass()
        self.logger.info(
            "loaded_in_mass called. analyze_on_play=%s, analyze_on_sync=%s",
            self.config.get_value(CONF_ANALYZE_ON_PLAY),
            self.config.get_value(CONF_ANALYZE_ON_SYNC),
        )

        if self.config.get_value(CONF_ANALYZE_ON_PLAY):
            self._on_unload.append(
                self.mass.subscribe(self._on_media_item_played, EventType.MEDIA_ITEM_PLAYED)
            )

        if self.config.get_value(CONF_ANALYZE_ON_SYNC):
            self._on_unload.append(
                self.mass.subscribe(self._on_media_item_added, EventType.MEDIA_ITEM_ADDED)
            )

        for path, handler in (
            ("/api/sonic_analysis/similar", self._handle_similar_tracks),
            ("/api/sonic_analysis/status", self._handle_status),
            ("/api/sonic_analysis/signatures", self._handle_signatures),
            ("/api/sonic_analysis/trigger_backfill", self._handle_trigger_backfill),
            ("/api/sonic_analysis/rebuild_index", self._handle_rebuild_index),
            ("/api/sonic_analysis/clear_all", self._handle_clear_all),
            ("/api/sonic_analysis/make_playlist", self._handle_make_playlist),
            ("/api/sonic_analysis/debug", self._handle_debug_page),
        ):
            self._on_unload.append(self.mass.webserver.register_dynamic_route(path, handler, "GET"))
            self._on_unload.append(
                self.mass.webserver.register_dynamic_route(
                    path, self._handle_cors_preflight, "OPTIONS"
                )
            )

        if self.config.get_value(CONF_ANALYZE_ON_SYNC):
            self.mass.tasks.create_task(
                task_id="sonic_analysis_backfill",
                name="Sonic Analysis: library backfill",
                handler=self._backfill_unanalyzed_tracks,
                allow_cancel=True,
                allow_retry=True,
            )

    async def _collect_unanalyzed_tracks(
        self, all_tracks: list[Any]
    ) -> tuple[list[tuple[str, Any]], int]:
        """Separate library tracks into those needing analysis and those already done.

        :param all_tracks: Full list of library tracks to inspect.
        :returns: Tuple of (tracks_to_analyze, skipped_count).
        """
        to_analyze: list[tuple[str, Any]] = []
        skipped_count = 0
        for track in all_tracks:
            item_id = str(track.item_id)
            has_signature = False
            for mapping in track.provider_mappings:
                existing = await self.get_sonic_signature(item_id, mapping.provider_instance)
                if existing:
                    has_signature = True
                    break
            if has_signature:
                skipped_count += 1
            else:
                to_analyze.append((item_id, track))
        return to_analyze, skipped_count

    async def _run_concurrent_analyses(
        self,
        to_analyze: list[tuple[str, Any]],
        skipped_count: int,
        total: int,
    ) -> int:
        """Run analyses concurrently, bounded by the configured semaphore.

        :param to_analyze: List of (item_id, track) pairs to analyze.
        :param skipped_count: Number of tracks already skipped (for progress reporting).
        :param total: Total number of tracks (for progress reporting).
        :returns: Number of successfully analyzed tracks.
        """

        async def _analyze_one(item_id: str, track: Any) -> bool:
            for mapping in track.provider_mappings:
                try:
                    await self._fetch_and_analyze(
                        item_id, mapping.provider_instance, mapping.item_id
                    )
                    return True
                except Exception as exc:
                    report_current_task_failure(
                        f"Track {item_id} via {mapping.provider_instance}: {exc}"
                    )
                    continue
            return False

        analyzed_count = 0
        pending: set[asyncio.Task[bool]] = set()
        processed = 0
        for item_id, track in to_analyze:
            task = asyncio.create_task(_analyze_one(item_id, track))
            pending.add(task)
            task.add_done_callback(pending.discard)

            max_conc = int(self.config.get_value(CONF_MAX_CONCURRENT_ANALYSES) or 2)  # type: ignore[arg-type]
            if len(pending) >= max_conc:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for t in done:
                    if t.result():
                        analyzed_count += 1
                    processed += 1
                update_current_task_progress_from_index(
                    skipped_count + processed,
                    total,
                    f"Analyzed {analyzed_count} of {len(to_analyze)} ({processed} processed)",
                )

        if pending:
            done_results = await asyncio.gather(*pending, return_exceptions=True)
            for result in done_results:
                if result is True:
                    analyzed_count += 1
                processed += 1

        return analyzed_count

    async def _backfill_unanalyzed_tracks(self) -> None:
        """Background task: analyze all local tracks without signatures."""
        self.logger.info("Starting background sonic analysis backfill...")

        update_current_task_progress_text("Scanning library...")
        page_size = 500
        offset = 0
        to_analyze: list[tuple[str, Any]] = []
        skipped_count = 0
        total_scanned = 0

        try:
            while True:
                page = await self.mass.music.tracks.library_items(limit=page_size, offset=offset)
                if not page:
                    break

                page_new, page_skipped = await self._collect_unanalyzed_tracks(page)
                to_analyze.extend(page_new)
                skipped_count += page_skipped
                total_scanned += len(page)

                update_current_task_progress_text(
                    f"Scanning: {total_scanned} checked, "
                    f"{len(to_analyze)} to analyze, {skipped_count} skipped"
                )

                if len(page) < page_size:
                    break
                offset += page_size
        except Exception:
            self.logger.warning("Could not fetch library tracks for backfill", exc_info=True)
            return

        total = total_scanned
        self.logger.info(
            "Backfill: %d to analyze, %d already have signatures",
            len(to_analyze),
            skipped_count,
        )
        update_current_task_progress_from_index(
            skipped_count,
            total,
            f"Checked: {skipped_count} skipped, {len(to_analyze)} to analyze",
        )

        analyzed_count = await self._run_concurrent_analyses(to_analyze, skipped_count, total)

        if analyzed_count > 0:
            update_current_task_progress_text("Rebuilding search index...")
            await self._rebuild_search_index()

        self.logger.info(
            "Backfill complete: %d analyzed, %d already had signatures",
            analyzed_count,
            skipped_count,
        )

    async def _on_media_item_played(self, event: MassEvent) -> None:
        """Handle media item played — analyze if no signature exists."""
        report = event.data
        if not isinstance(report, MediaItemPlaybackProgressReport):
            return
        if report.media_type != MediaType.TRACK:
            return

        try:
            _media_type, provider, item_id = await parse_uri(report.uri)
        except Exception:
            return

        # Only attempt analysis when the track has a resolvable local file path.
        # Streaming-only tracks are silently skipped here; _fetch_and_analyze will
        # log a debug message for non-local tracks (v1 limitation).
        try:
            track = await self.mass.music.tracks.get(item_id, provider)
        except Exception:
            track = None
        has_local_mapping = track is not None and any(
            getattr(m, "provider_instance", "").startswith("filesystem")
            for m in getattr(track, "provider_mappings", [])
        )
        if not has_local_mapping:
            return

        existing = await self.get_sonic_signature(item_id, provider)
        if existing is not None:
            return

        self.mass.create_task(self._fetch_and_analyze(item_id, provider))

    async def _on_media_item_added(self, event: MassEvent) -> None:
        """Handle media item added — queue for background analysis."""
        item = event.data
        if not isinstance(item, Track):
            return
        if item.media_type != MediaType.TRACK:
            return

        existing = await self.get_sonic_signature(item.item_id, item.provider)
        if existing is not None:
            return

        self.mass.create_task(self._fetch_and_analyze(item.item_id, item.provider))

    async def _analyze_track(
        self,
        item_id: str,
        provider_instance: str,
        audio: np.ndarray,
        sample_rate: int,
    ) -> SonicSignature | None:
        """Extract, store and optionally index a sonic signature for a track.

        Runs the blocking librosa extraction in a thread to avoid blocking the
        event loop. Callers are responsible for concurrency gating (the backfill
        path uses ``_analysis_semaphore``; event-triggered paths use
        ``_fetch_and_analyze`` which acquires the semaphore internally).

        :param item_id: Provider-scoped track identifier.
        :param provider_instance: Provider domain or instance ID that owns the track.
        :param audio: Mono float32 audio samples.
        :param sample_rate: Sample rate of the audio in Hz.
        """
        try:
            signature: SonicSignature = await asyncio.to_thread(
                extract_signature, audio, sample_rate
            )
        except Exception:
            self.logger.warning("Feature extraction failed for %s/%s", provider_instance, item_id)
            return None

        await self.set_sonic_signature(item_id, provider_instance, signature)

        if self.corpus_means is not None and self.corpus_stds is not None:
            normalized = normalize_features(signature.features, self.corpus_means, self.corpus_stds)
            label = self._get_or_assign_label(item_id, provider_instance)
            self._add_to_index(label, normalized)

        return signature

    async def _fetch_and_analyze(
        self,
        item_id: str,
        provider: str,
        provider_item_id: str | None = None,
    ) -> None:
        """Fetch audio for a track and run the analysis pipeline.

        For local files the path is read directly via librosa; for streamed content
        a PCM pipeline is used (not yet implemented — placeholder logs a warning).

        Acquires ``_analysis_semaphore`` to limit concurrent analyses across
        both event-triggered and manually triggered code paths.

        :param item_id: Library track identifier (used for DB storage).
        :param provider: Provider instance ID that owns the track.
        :param provider_item_id: Provider-scoped track ID for stream details.
            Falls back to item_id if not provided.
        """
        async with self._analysis_semaphore:
            try:
                audio: np.ndarray
                sample_rate = 22050
                prov_item_id = provider_item_id or item_id

                stream_details: Any = None
                try:
                    prov_inst: Any = self.mass.get_provider(provider)
                    if prov_inst is not None and hasattr(prov_inst, "get_stream_details"):
                        stream_details = await prov_inst.get_stream_details(
                            prov_item_id, MediaType.TRACK
                        )
                except Exception:
                    self.logger.debug(
                        "Could not resolve stream details for %s/%s",
                        provider,
                        item_id,
                    )

                file_path: str | None = (
                    str(stream_details.path)
                    if stream_details is not None and getattr(stream_details, "path", None)
                    else None
                )
                if file_path is not None:
                    audio, _sr = await asyncio.to_thread(
                        librosa.load, file_path, sr=sample_rate, mono=True
                    )
                else:
                    self.logger.debug(
                        "Skipping analysis for %s/%s: no local file path."
                        " Streaming analysis is not supported in v1.",
                        provider,
                        item_id,
                    )
                    return

                await self._analyze_track(item_id, provider, audio, sample_rate)
            except Exception as exc:
                self.logger.warning("Analysis failed for %s/%s: %s", provider, item_id, exc)

    def _get_or_assign_label(self, item_id: str, provider: str) -> int:
        """Return the integer label for a (item_id, provider) pair, assigning one if new."""
        key = (item_id, provider)
        if key in self._reverse_label_map:
            return self._reverse_label_map[key]
        label = self._next_label
        self._next_label += 1
        self._label_map[label] = key
        self._reverse_label_map[key] = label
        return label

    async def _has_signature(self, item_id: str) -> bool:
        """Return True if at least one signature row exists for *item_id*."""
        assert self.mass.music.database is not None
        rows = await self.mass.music.database.get_rows(
            DB_TABLE_SONIC_SIGNATURES, {"item_id": item_id}
        )
        return any(r["item_id"] != CORPUS_STATS_ITEM_ID for r in rows)

    async def _create_db_table(self) -> None:
        """Create the sonic_signatures table if it does not already exist."""
        assert self.mass.music.database is not None
        await self.mass.music.database.execute(
            f"""CREATE TABLE IF NOT EXISTS {DB_TABLE_SONIC_SIGNATURES}(
                [id] INTEGER PRIMARY KEY AUTOINCREMENT,
                [item_id] TEXT NOT NULL,
                [provider] TEXT NOT NULL,
                [features] TEXT NOT NULL,
                [version] INTEGER NOT NULL,
                [timestamp] REAL NOT NULL DEFAULT (cast(strftime('%s','now') as int)),
                UNIQUE(item_id, provider)
            )"""
        )
        await self.mass.music.database.commit()

    async def _load_corpus_stats(self) -> None:
        """Load corpus statistics from the special sentinel DB row, if present."""
        assert self.mass.music.database is not None
        row = await self.mass.music.database.get_row(
            DB_TABLE_SONIC_SIGNATURES,
            {"item_id": CORPUS_STATS_ITEM_ID, "provider": CORPUS_STATS_ITEM_ID},
        )
        if row is None:
            return
        try:
            payload: dict[str, list[float]] = json.loads(row["features"])
            self.corpus_means = payload["means"]
            self.corpus_stds = payload["stds"]
        except (KeyError, ValueError, TypeError):
            self.logger.warning("Failed to deserialise corpus stats from DB; resetting.")
            self.corpus_means = None
            self.corpus_stds = None

    async def _save_corpus_stats(self, means: list[float], stds: list[float]) -> None:
        """Persist corpus statistics as a sentinel row in the DB.

        :param means: Per-feature mean values computed over the corpus.
        :param stds: Per-feature standard deviation values computed over the corpus.
        """
        assert self.mass.music.database is not None
        payload = json.dumps({"means": means, "stds": stds})
        await self.mass.music.database.insert_or_replace(
            DB_TABLE_SONIC_SIGNATURES,
            {
                "item_id": CORPUS_STATS_ITEM_ID,
                "provider": CORPUS_STATS_ITEM_ID,
                "features": payload,
                "version": SIGNATURE_VERSION,
            },
        )
        self.corpus_means = means
        self.corpus_stds = stds

    async def get_sonic_signature(self, item_id: str, provider: str) -> SonicSignature | None:
        """Return the stored sonic signature for a track, or None if not found.

        :param item_id: Provider-scoped track identifier.
        :param provider: Provider domain that owns the track.
        """
        assert self.mass.music.database is not None
        row = await self.mass.music.database.get_row(
            DB_TABLE_SONIC_SIGNATURES,
            {"item_id": item_id, "provider": provider},
        )
        if row is None:
            return None
        try:
            features: list[float] = json.loads(row["features"])
            return SonicSignature(features=features, version=int(row["version"]))
        except (KeyError, ValueError, TypeError):
            self.logger.warning(
                "Failed to deserialise sonic signature for %s/%s", provider, item_id
            )
            return None

    async def set_sonic_signature(
        self, item_id: str, provider: str, signature: SonicSignature
    ) -> None:
        """Persist a sonic signature for a track.

        :param item_id: Provider-scoped track identifier.
        :param provider: Provider domain that owns the track.
        :param signature: The sonic signature to store.
        """
        assert self.mass.music.database is not None
        await self.mass.music.database.insert_or_replace(
            DB_TABLE_SONIC_SIGNATURES,
            {
                "item_id": item_id,
                "provider": provider,
                "features": json.dumps(signature.features),
                "version": signature.version,
            },
        )

    def _init_search_index(self) -> None:
        """Initialize or load the USearch ANN index."""
        if not _USEARCH_AVAILABLE:
            return

        index_path = Path(self.mass.storage_path) / USEARCH_INDEX_FILENAME
        if index_path.exists():
            self._search_index = _usearch_index_module.Index.restore(str(index_path))
        else:
            self._search_index = _usearch_index_module.Index(
                ndim=SIGNATURE_DIMENSIONS,
                metric=MetricKind.Cos,
                dtype=ScalarKind.I8,
            )

    def _add_to_index(self, item_id_int: int, normalized_features: list[float]) -> None:
        """Add a normalised feature vector to the ANN index with an explicit item ID.

        :param item_id_int: Integer label used as the USearch key.
        :param normalized_features: Z-score normalised feature values, one per dimension.
        """
        vector = np.array(normalized_features, dtype=np.float32)
        if item_id_int in self._search_index:
            self._search_index.remove(item_id_int)
        self._search_index.add(item_id_int, vector)

    def _query_index(
        self, normalized_features: list[float], k: int = 25
    ) -> list[tuple[int, float]]:
        """Query the ANN index for the k nearest neighbours.

        Returns an empty list when the index has no elements.
        When k exceeds len(index) the query is clamped automatically.

        :param normalized_features: Per-feature z-score normalised query vector.
        :param k: Maximum number of nearest neighbours to return.
        """
        if len(self._search_index) == 0:
            return []

        effective_k = min(k, len(self._search_index))
        query = np.array(normalized_features, dtype=np.float32)
        matches = self._search_index.search(query, count=effective_k)
        return [(int(matches.keys[i]), float(matches.distances[i])) for i in range(len(matches))]

    def _save_search_index(self) -> None:
        """Persist the USearch ANN index to storage_path."""
        if self._search_index is None:
            return

        index_path = Path(self.mass.storage_path) / USEARCH_INDEX_FILENAME
        if not index_path.parent.exists():
            return

        self._search_index.save(str(index_path))

    async def _handle_similar_tracks(self, request: Any) -> Any:
        """Handle GET /api/sonic_analysis/similar endpoint.

        Query parameters: item_id (required), limit, genre_weight, year_weight.

        :param request: Incoming aiohttp web request.
        """
        item_id: str | None = request.query.get("item_id")
        if not item_id:
            return _cors_text("Missing required query parameter: item_id", status=400)

        try:
            limit = min(int(request.query.get("limit", 25)), 100)
        except (TypeError, ValueError):
            limit = 25

        try:
            genre_weight = max(0.0, min(1.0, float(request.query.get("genre_weight", 0))))
        except (TypeError, ValueError):
            genre_weight = 0.0

        try:
            year_weight = max(0.0, min(1.0, float(request.query.get("year_weight", 0))))
        except (TypeError, ValueError):
            year_weight = 0.0

        results = await self._get_similar_item_ids(
            item_id, limit=limit, genre_weight=genre_weight, year_weight=year_weight
        )

        if not results and (self.corpus_means is None or not await self._has_signature(item_id)):
            return _cors_json({"analyzed": False, "items": [], "seed_track_id": item_id})

        items = [{"item_id": rid, "distance": dist} for rid, dist in results]
        return _cors_json({"analyzed": True, "items": items, "seed_track_id": item_id})

    async def _handle_trigger_backfill(self, request: Any) -> Any:
        """Handle GET /api/sonic_analysis/trigger_backfill — manually start backfill."""
        try:
            await self._create_db_table()
            self.mass.tasks.create_task(
                task_id="sonic_analysis_backfill",
                name="Sonic Analysis: library backfill",
                handler=self._backfill_unanalyzed_tracks,
                allow_cancel=True,
                allow_retry=True,
            )
            return _cors_json({"status": "backfill_started"})
        except Exception as exc:
            self.logger.exception("trigger_backfill failed")
            return _cors_json({"status": "error", "error": str(exc)})

    async def _handle_clear_all(self, request: Any) -> Any:
        """Handle GET /api/sonic_analysis/clear_all — drop all signatures and reset index."""
        try:
            assert self.mass.music.database is not None
            await self.mass.music.database.execute(f"DELETE FROM {DB_TABLE_SONIC_SIGNATURES}")
            await self.mass.music.database.commit()
            self._label_map.clear()
            self._reverse_label_map.clear()
            self._next_label = 1
            self.corpus_means = None
            self.corpus_stds = None
            # Delete the index file before re-init so it creates a fresh empty one
            index_path = Path(self.mass.storage_path) / USEARCH_INDEX_FILENAME
            if index_path.exists():
                index_path.unlink()
            self._init_search_index()
            self.logger.info("Cleared all sonic signatures and reset index.")
            return _cors_json({"status": "cleared"})
        except Exception as exc:
            self.logger.exception("clear_all failed")
            return _cors_json({"status": "error", "error": str(exc)})

    async def _handle_make_playlist(self, request: Any) -> Any:
        """Handle GET /api/sonic_analysis/make_playlist — create a playlist from similar tracks."""
        try:
            item_id = request.query.get("item_id")
            if not item_id:
                return _cors_text("Missing required query parameter: item_id", status=400)

            try:
                genre_weight = max(0.0, min(1.0, float(request.query.get("genre_weight", 0))))
            except (TypeError, ValueError):
                genre_weight = 0.0
            try:
                year_weight = max(0.0, min(1.0, float(request.query.get("year_weight", 0))))
            except (TypeError, ValueError):
                year_weight = 0.0

            # Get the seed track name
            try:
                seed_track = await self.mass.music.tracks.get(item_id, "library")
                seed_name = seed_track.name
            except Exception:
                seed_name = f"Track {item_id}"

            # Get top 10 similar to the seed
            similar_result = await self._get_similar_item_ids(
                item_id, limit=10, genre_weight=genre_weight, year_weight=year_weight
            )
            if not similar_result:
                return _cors_json({"status": "error", "error": "No similar tracks found"})

            # Collect all unique track IDs: seed + tier1 + tier2
            seen_ids: set[str] = {item_id}
            ordered_ids: list[str] = []

            # Add tier 1 (direct similar)
            tier1_ids = []
            for sim_id, _dist in similar_result:
                if sim_id not in seen_ids:
                    seen_ids.add(sim_id)
                    ordered_ids.append(sim_id)
                    tier1_ids.append(sim_id)

            # For each tier 1 track, get its top 10 similar (tier 2)
            for t1_id in tier1_ids:
                tier2_result = await self._get_similar_item_ids(
                    t1_id, limit=10, genre_weight=genre_weight, year_weight=year_weight
                )
                for sim_id, _dist in tier2_result:
                    if sim_id not in seen_ids:
                        seen_ids.add(sim_id)
                        ordered_ids.append(sim_id)

            # Create the playlist
            safe_name = seed_name.replace("/", "-").replace("\\", "-").replace("..", "")
            playlist_name = f"Songs like {safe_name}"
            playlist = await self.mass.music.playlists.create_playlist(playlist_name)

            # Build URIs for all tracks
            uris = [f"library://track/{tid}" for tid in ordered_ids]

            if uris:
                await self.mass.music.playlists.add_playlist_tracks(playlist.item_id, uris)

            self.logger.info("Created playlist '%s' with %d tracks", playlist_name, len(uris))
            return _cors_json(
                {
                    "status": "created",
                    "playlist_name": playlist_name,
                    "track_count": len(uris),
                    "playlist_id": playlist.item_id,
                }
            )
        except Exception as exc:
            self.logger.exception("make_playlist failed")
            return _cors_json({"status": "error", "error": str(exc)})

    async def _get_similar_item_ids(
        self,
        item_id: str,
        limit: int = 10,
        genre_weight: float = 0.0,
        year_weight: float = 0.0,
    ) -> list[tuple[str, float]]:
        """Get similar track item_ids from the index with optional metadata re-ranking.

        :param item_id: library item ID of the seed track.
        :param limit: max results to return.
        :param genre_weight: 0.0-1.0 how much to boost tracks sharing genres with the seed.
        :param year_weight: 0.0-1.0 how much to boost tracks from a similar year/decade.
        """
        assert self.mass.music.database is not None
        rows = await self.mass.music.database.get_rows(
            DB_TABLE_SONIC_SIGNATURES, {"item_id": item_id}
        )
        sig_row = None
        seed_provider = ""
        for row in rows:
            if row["item_id"] != CORPUS_STATS_ITEM_ID:
                sig_row = row
                seed_provider = row["provider"]
                break
        if sig_row is None or self.corpus_means is None or self.corpus_stds is None:
            return []

        features = json.loads(sig_row["features"])
        normalized = normalize_features(features, self.corpus_means, self.corpus_stds)

        # Fetch extra candidates for re-ranking when metadata weights are active
        fetch_k = limit * 5 if (genre_weight > 0 or year_weight > 0) else limit + 1
        raw_results = self._query_index(normalized, k=fetch_k)

        seed_label = self._reverse_label_map.get((item_id, seed_provider))
        candidates: list[tuple[str, float]] = []
        for result_id, distance in raw_results:
            if result_id == seed_label:
                continue
            resolved = self._label_map.get(result_id)
            if resolved is None:
                continue
            candidates.append((resolved[0], distance))

        if not candidates:
            return []

        if genre_weight <= 0 and year_weight <= 0:
            return candidates[:limit]

        return await self._rerank_with_metadata(
            item_id, candidates, limit, genre_weight, year_weight
        )

    async def _rerank_with_metadata(
        self,
        seed_item_id: str,
        candidates: list[tuple[str, float]],
        limit: int,
        genre_weight: float,
        year_weight: float,
    ) -> list[tuple[str, float]]:
        """Re-rank sonic similarity candidates using genre and year metadata.

        :param seed_item_id: library item ID of the seed track.
        :param candidates: list of (item_id, sonic_distance) from the ANN index.
        :param limit: max results to return.
        :param genre_weight: 0.0-1.0 genre influence.
        :param year_weight: 0.0-1.0 year influence.
        """
        try:
            seed_track = await self.mass.music.tracks.get(seed_item_id, "library")
            seed_genres = {g.name.lower() for g in getattr(seed_track, "genres", []) or []}
            seed_year = getattr(seed_track, "year", None) or 0
        except Exception:
            return candidates[:limit]

        async def _safe_get(tid: str) -> Track | None:
            try:
                return await self.mass.music.tracks.get(tid, "library")
            except Exception:
                return None

        cand_ids = [cid for cid, _ in candidates]
        cand_tracks = await asyncio.gather(*[_safe_get(cid) for cid in cand_ids])
        cand_track_map: dict[str, Track] = {
            cid: t for cid, t in zip(cand_ids, cand_tracks, strict=True) if t is not None
        }

        scored: list[tuple[str, float]] = []
        for cand_id, sonic_dist in candidates:
            cand_track = cand_track_map.get(cand_id)
            if cand_track is None:
                scored.append((cand_id, sonic_dist))
                continue

            cand_genres: set[str] = {
                g.name.lower() for g in getattr(cand_track, "genres", []) or []
            }
            cand_year: int = getattr(cand_track, "year", None) or 0

            genre_bonus = 0.0
            if seed_genres and cand_genres and genre_weight > 0:
                overlap = len(seed_genres & cand_genres)
                union = len(seed_genres | cand_genres)
                genre_bonus = (overlap / union) if union > 0 else 0.0

            year_bonus = 0.0
            if seed_year and cand_year and year_weight > 0:
                year_diff = abs(seed_year - cand_year)
                year_bonus = max(0.0, 1.0 - (year_diff / 30.0))

            metadata_boost = genre_bonus * genre_weight + year_bonus * year_weight
            blended = sonic_dist - (metadata_boost * 0.5)
            scored.append((cand_id, blended))

        scored.sort(key=lambda x: x[1])
        return scored[:limit]

    async def _handle_rebuild_index(self, request: Any) -> Any:
        """Handle GET /api/sonic_analysis/rebuild_index — rebuild USearch index from DB."""
        try:
            await self._rebuild_search_index()
            index_size = len(self._search_index) if self._search_index is not None else 0
            return _cors_json({"status": "rebuilt", "index_size": index_size})
        except Exception as exc:
            self.logger.exception("rebuild_index failed")
            return _cors_json({"status": "error", "error": str(exc)})

    async def _handle_debug_page(self, request: Any) -> Any:
        """Serve the built-in debug console as an HTML page."""
        base_url = f"{request.scheme}://{request.host}/api/sonic_analysis"
        html = _DEBUG_HTML.replace("%%BASE_URL%%", base_url)
        return web.Response(text=html, content_type="text/html")

    async def _handle_cors_preflight(self, request: Any) -> Any:
        """Handle OPTIONS preflight requests for CORS."""
        return web.Response(
            status=204,
            headers={
                **_CORS_HEADERS,
                "Access-Control-Allow-Methods": "GET, OPTIONS",
                "Access-Control-Max-Age": "3600",
            },
        )

    async def _handle_status(self, request: Any) -> Any:
        """Handle GET /api/sonic_analysis/status — return plugin and index stats."""
        index_size = len(self._search_index) if self._search_index is not None else 0
        has_corpus_stats = self.corpus_means is not None and len(self.corpus_means) > 0
        label_map_size = len(self._label_map)

        db_count = 0
        db_error = ""
        if self.mass.music.database is not None:
            try:
                db_count = await self.mass.music.database.get_count_from_query(
                    f"SELECT * FROM {DB_TABLE_SONIC_SIGNATURES} WHERE item_id != :skip",
                    {"skip": CORPUS_STATS_ITEM_ID},
                )
            except Exception as exc:
                db_count = -1
                db_error = str(exc)

        return _cors_json(
            {
                "index_size": index_size,
                "db_signatures": db_count,
                "db_error": db_error,
                "label_map_size": label_map_size,
                "has_corpus_stats": has_corpus_stats,
                "signature_version": SIGNATURE_VERSION,
                "signature_dimensions": SIGNATURE_DIMENSIONS,
                "analyze_on_play": bool(self.config.get_value(CONF_ANALYZE_ON_PLAY)),
                "analyze_on_sync": bool(self.config.get_value(CONF_ANALYZE_ON_SYNC)),
            }
        )

    async def _handle_signatures(self, request: Any) -> Any:
        """Handle GET /api/sonic_analysis/signatures — list stored signatures."""
        try:
            limit_str = request.query.get("limit", "50")
            offset_str = request.query.get("offset", "0")
            try:
                limit = min(int(limit_str), 500)
                offset = int(offset_str)
            except (TypeError, ValueError):
                limit, offset = 50, 0

            db = self.mass.music.database
            assert db is not None
            base_query = f"SELECT * FROM {DB_TABLE_SONIC_SIGNATURES} WHERE item_id != :skip"
            query_params = {"skip": CORPUS_STATS_ITEM_ID}

            total = await db.get_count_from_query(base_query, query_params)

            all_rows = await db.get_rows_from_query(
                f"SELECT item_id, provider, version, features"
                f" FROM {DB_TABLE_SONIC_SIGNATURES}"
                " WHERE item_id != :skip",
                query_params,
                limit=limit,
                offset=offset,
            )

            signatures = []
            for row in all_rows:
                try:
                    feat_count = len(json.loads(row["features"]))
                except (KeyError, json.JSONDecodeError, TypeError):
                    feat_count = 0
                signatures.append(
                    {
                        "item_id": row["item_id"],
                        "provider": row["provider"],
                        "version": row["version"],
                        "feature_count": feat_count,
                    }
                )

            return _cors_json({"signatures": signatures, "total": total})
        except Exception as exc:
            self.logger.exception("_handle_signatures failed")
            return _cors_json({"signatures": [], "total": 0, "error": str(exc)})

    async def _rebuild_search_index(self) -> None:
        """Rebuild the USearch index from all signatures in the DB.

        Fetches every row from the sonic_signatures table, recomputes corpus statistics,
        creates a fresh index, and populates it with normalised feature vectors.
        Useful for recovery after index loss or a schema migration.
        """
        assert self.mass.music.database is not None
        all_rows = await self.mass.music.database.get_rows(
            DB_TABLE_SONIC_SIGNATURES, match=None, limit=0
        )

        track_rows = [row for row in all_rows if row["item_id"] != CORPUS_STATS_ITEM_ID]

        if not track_rows:
            self.logger.info("No sonic signatures found in DB; skipping index rebuild.")
            return

        all_features: list[list[float]] = []
        parsed: list[tuple[str, str, list[float]]] = []
        for row in track_rows:
            try:
                features = json.loads(row["features"])
                all_features.append(features)
                parsed.append((row["item_id"], row["provider"], features))
            except (KeyError, ValueError, TypeError):
                continue

        if not all_features:
            self.logger.info("No valid sonic signatures found; skipping index rebuild.")
            return

        means, stds = await asyncio.to_thread(compute_corpus_stats, all_features)
        await self._save_corpus_stats(means, stds)

        if not _USEARCH_AVAILABLE:
            return

        def _populate_index() -> None:
            self._search_index = _usearch_index_module.Index(
                ndim=SIGNATURE_DIMENSIONS,
                metric=MetricKind.Cos,
                dtype=ScalarKind.I8,
            )
            self._label_map = {}
            self._reverse_label_map = {}
            self._next_label = 1

            for item_id, provider_instance, features in parsed:
                normalized = normalize_features(features, means, stds)
                label = self._get_or_assign_label(item_id, provider_instance)
                self._add_to_index(label, normalized)

            self._save_search_index()

        await asyncio.to_thread(_populate_index)
        self.logger.info("Search index rebuilt with %d entries.", len(parsed))

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider.

        Called when provider is deregistered (e.g. MA exiting or config reloading).

        :param is_removed: True when the provider is permanently removed from configuration.
        """
        for unload_cb in self._on_unload:
            unload_cb()
        await asyncio.to_thread(self._save_search_index)


_DEBUG_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sonic Analysis Debug</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:monospace;background:#0a0a0f;color:#e0e0ea;padding:1.5rem}
h1{font-size:1.1rem;color:#00e5a0;margin-bottom:1rem}
h1 span{color:#7a7a8e;font-weight:400}
h2{font-size:.8rem;color:#7a7a8e;text-transform:uppercase;letter-spacing:.1em;margin:1rem 0 .5rem}
.g{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));
gap:.5rem;margin-bottom:1rem}
.c{background:#12121a;border:1px solid #2a2a3a;border-radius:6px;padding:.75rem}
.c .l{font-size:.6rem;color:#7a7a8e;text-transform:uppercase;letter-spacing:.08em}
.c .v{font-size:1.4rem;font-weight:700;color:#00e5a0;margin-top:.25rem}
.c .v.w{color:#ff6b6b}.c .v.i{color:#6bc5ff}.c .v.d{color:#7a7a8e}
input,select{font-family:monospace;font-size:.8rem;padding:.4rem .6rem;
background:#12121a;border:1px solid #2a2a3a;
border-radius:4px;color:#e0e0ea;outline:0}
input:focus{border-color:#00e5a0}
button{font-family:monospace;font-size:.75rem;font-weight:600;
padding:.4rem 1rem;border:1px solid #00e5a0;
background:0 0;color:#00e5a0;border-radius:4px;cursor:pointer;text-transform:uppercase}
button:hover{background:#00e5a0;color:#0a0a0f}
.row{display:flex;gap:.5rem;align-items:center;margin-bottom:.75rem;flex-wrap:wrap}
table{width:100%;border-collapse:collapse;font-size:.75rem;margin:.5rem 0}
th{background:#1a1a26;padding:.4rem .6rem;text-align:left;
color:#7a7a8e;font-size:.65rem;text-transform:uppercase}
td{padding:.4rem .6rem;border-bottom:1px solid #2a2a3a}
tr:hover td{background:#00e5a010}
.log{background:#12121a;border:1px solid #2a2a3a;border-radius:4px;
padding:.5rem;font-size:.7rem;
max-height:200px;overflow:auto;color:#7a7a8e;line-height:1.5;margin:.5rem 0}
.ri{display:flex;align-items:center;gap:.75rem;padding:.5rem .75rem;
background:#12121a;
border:1px solid #2a2a3a;border-radius:4px;margin-bottom:.4rem}
.ri:hover{border-color:#00e5a0}
.ri .rk{color:#7a7a8e;min-width:24px;text-align:center;font-size:.7rem;font-weight:700}
.ri .id{font-weight:600;font-size:.78rem}.ri .pv{font-size:.65rem;color:#7a7a8e}
.ri .ds{font-weight:700;padding:.2rem .5rem;border-radius:3px;
min-width:60px;text-align:center;font-size:.8rem}
.dc{background:#00e5a018;color:#00e5a0}
.dm{background:#6bc5ff18;color:#6bc5ff}
.df{background:#ff6b6b18;color:#ff6b6b}
pre.raw{background:#12121a;border:1px solid #2a2a3a;border-radius:4px;
padding:.5rem;font-size:.7rem;
overflow:auto;max-height:300px;white-space:pre-wrap;word-break:break-all;color:#7a7a8e}
</style></head><body>
<h1>sonic_analysis <span>// debug console</span></h1>

<h2>Status</h2>
<div class="g" id="sg"></div>
<button onclick="fetchStatus()">Refresh Status</button>
<button onclick="triggerBackfill()"
  style="border-color:#6bc5ff;color:#6bc5ff">Trigger Backfill</button>
<button onclick="rebuildIndex()"
  style="border-color:#ff6b6b;color:#ff6b6b">Rebuild Index</button>
<button onclick="if(confirm('Delete ALL signatures and reset index?'))clearAll()"
  style="border-color:#ff6b6b;color:#ff6b6b">Clear All Data</button>
<div id="dberr" style="color:#ff6b6b;font-size:.75rem;margin-top:.5rem"></div>

<h2>Log</h2>
<div class="log" id="lo"></div>

<h2>Signatures in DB</h2>
<div class="row">
<button onclick="prevP()">&lt;</button>
<span id="pi" style="font-size:.75rem;color:#7a7a8e"></span>
<button onclick="nextP()">&gt;</button>
<button onclick="fetchSigs()">Refresh</button>
</div>
<div id="st"></div>

<h2>Similarity Search</h2>
<div class="row">
<input id="si" placeholder="item_id" style="width:180px">
<input id="sl" type="number" value="10" min="1" max="100" style="width:60px">
<button onclick="doSearch()">Search</button>
<button onclick="makePlaylist()"
  style="border-color:#6bc5ff;color:#6bc5ff">Make Playlist</button>
</div>
<div class="row" style="font-size:.75rem;gap:1rem">
<label style="color:#7a7a8e">Genre:
  <input id="gw" type="range" min="0" max="100" value="0"
    style="width:100px;vertical-align:middle">
  <span id="gwv">0%</span></label>
<label style="color:#7a7a8e">Year:
  <input id="yw" type="range" min="0" max="100" value="0"
    style="width:100px;vertical-align:middle">
  <span id="ywv">0%</span></label>
</div>
<div id="sr"></div>

<h2>Raw API</h2>
<div class="row">
<select id="ae">
<option value="status">GET /status</option>
<option value="signatures">GET /signatures</option>
<option value="similar">GET /similar</option>
</select>
<input id="ap" placeholder="item_id=123&limit=10" style="flex:1">
<button onclick="doRaw()">Send</button>
</div>
<pre class="raw" id="ro">—</pre>

<script>
var BASE='%%BASE_URL%%';
var pg=0,PS=50;
document.getElementById('gw').oninput=function(){
  document.getElementById('gwv').textContent=this.value+'%'
};
document.getElementById('yw').oninput=function(){
  document.getElementById('ywv').textContent=this.value+'%'
};
function getWeights(){
  return{
    genre_weight:document.getElementById('gw').value/100,
    year_weight:document.getElementById('yw').value/100
  }
}

function cl(p){while(p.firstChild)p.removeChild(p.firstChild)}
function tx(t){return document.createTextNode(t)}
function mk(tag,cls,text){
  var e=document.createElement(tag);
  if(cls)e.className=cls;
  if(text)e.textContent=text;
  return e
}

function logMsg(m,ok){
  var d=document.getElementById('lo');
  var line=mk('div','',new Date().toLocaleTimeString()+' '+m);
  if(ok===true)line.style.color='#00e5a0';
  else if(ok===false)line.style.color='#ff6b6b';
  else line.style.color='#6bc5ff';
  d.appendChild(line);d.scrollTop=d.scrollHeight;
}

function api(ep,params){
  var u=BASE+'/'+ep;
  if(params){var s=new URLSearchParams(params);u+='?'+s.toString()}
  return fetch(u).then(function(r){
    if(!r.ok)throw new Error('HTTP '+r.status);
    return r.json()
  });
}

function fetchStatus(){
  api('status').then(function(d){
    var g=document.getElementById('sg');cl(g);
    var items=[
      ['DB Sigs',d.db_signatures,d.db_signatures>0?'':'d'],
      ['Index',d.index_size,d.index_size>0?'':'d'],
      ['Labels',d.label_map_size,'i'],
      ['Corpus',d.has_corpus_stats?'YES':'NO',d.has_corpus_stats?'':'w'],
      ['Dims',d.signature_dimensions,'i'],
      ['Ver','v'+d.signature_version,'i'],
      ['On Play',d.analyze_on_play?'ON':'OFF',d.analyze_on_play?'':'d'],
      ['On Sync',d.analyze_on_sync?'ON':'OFF',d.analyze_on_sync?'':'d'],
    ];
    items.forEach(function(x){
      var c=mk('div','c');
      c.appendChild(mk('div','l',x[0]));
      c.appendChild(mk('div','v '+x[2],String(x[1])));
      g.appendChild(c);
    });
    var errEl=document.getElementById('dberr');
    errEl.textContent=d.db_error||'';
    logMsg('Status: '+d.db_signatures+' sigs, '+d.index_size+' indexed',true);
  }).catch(function(e){logMsg('Status error: '+e.message,false)});
}

function triggerBackfill(){
  logMsg('Triggering backfill...');
  api('trigger_backfill').then(function(d){
    logMsg('Backfill response: '+d.status,d.status==='backfill_started');
    if(d.error)logMsg('Error: '+d.error,false);
  }).catch(function(e){logMsg('Trigger failed: '+e.message,false)});
}

function rebuildIndex(){
  logMsg('Rebuilding index from DB...');
  api('rebuild_index').then(function(d){
    logMsg('Rebuild: '+d.status+' (index_size='+d.index_size+')',d.status==='rebuilt');
    if(d.error)logMsg('Error: '+d.error,false);
    fetchStatus();
  }).catch(function(e){logMsg('Rebuild failed: '+e.message,false)});
}

function clearAll(){
  logMsg('Clearing all data...');
  api('clear_all').then(function(d){
    logMsg('Clear: '+d.status,d.status==='cleared');
    if(d.error)logMsg('Error: '+d.error,false);
    fetchStatus();fetchSigs();
  }).catch(function(e){logMsg('Clear failed: '+e.message,false)});
}

function fetchSigs(){
  api('signatures',{limit:PS,offset:pg*PS}).then(function(d){
    var tp=Math.ceil(d.total/PS)||1;
    document.getElementById('pi').textContent=(pg+1)+'/'+tp+' ('+d.total+')';
    var w=document.getElementById('st');cl(w);
    if(!d.signatures.length){w.appendChild(mk('div','','No signatures'));return}
    var t=mk('table');
    var th=mk('thead');var hr=mk('tr');
    ['#','Item ID','Provider','Ver','Feat',''].forEach(function(h){
      hr.appendChild(mk('th','',h))
    });
    th.appendChild(hr);t.appendChild(th);
    var tb=mk('tbody');
    d.signatures.forEach(function(s,i){
      var r=mk('tr');
      r.appendChild(mk('td','',String(pg*PS+i+1)));
      var idTd=mk('td','',s.item_id);idTd.title=s.item_id;r.appendChild(idTd);
      r.appendChild(mk('td','',s.provider));
      r.appendChild(mk('td','','v'+s.version));
      r.appendChild(mk('td','',String(s.feature_count)));
      var btn=mk('button','','Similar');
      btn.style.padding='0.15rem 0.4rem';btn.style.fontSize='0.6rem';
      btn.addEventListener('click',function(){
        document.getElementById('si').value=s.item_id;doSearch()
      });
      var btd=mk('td');btd.appendChild(btn);r.appendChild(btd);
      tb.appendChild(r);
    });
    t.appendChild(tb);w.appendChild(t);
  }).catch(function(e){logMsg('Sigs error: '+e.message,false)});
}

function prevP(){if(pg>0){pg--;fetchSigs()}}
function nextP(){pg++;fetchSigs()}

function doSearch(){
  var id=document.getElementById('si').value.trim();
  var lim=document.getElementById('sl').value;
  if(!id)return;
  var wt=getWeights();
  var w=document.getElementById('sr');cl(w);
  w.appendChild(mk('div','','Searching...'));
  logMsg(
    'Searching similar to '+id
    +' (genre='+Math.round(wt.genre_weight*100)
    +'% year='+Math.round(wt.year_weight*100)+'%)...'
  );
  api('similar',{
    item_id:id,limit:lim,
    genre_weight:wt.genre_weight,year_weight:wt.year_weight
  }).then(function(d){
    cl(w);
    if(!d.analyzed){
      w.appendChild(mk('div','','Not analyzed yet'));
      logMsg('No signature for '+id,false);
      return
    }
    if(!d.items.length){w.appendChild(mk('div','','No results'));return}
    d.items.forEach(function(it,i){
      var dist=it.distance;
      var cls=dist<0.3?'dc':dist<0.7?'dm':'df';
      var row=mk('div','ri');
      row.appendChild(mk('div','rk','#'+(i+1)));
      var info=mk('div');
      info.appendChild(mk('div','id',it.item_id));
      info.appendChild(mk('div','pv',it.provider));
      info.style.flex='1';
      row.appendChild(info);
      row.appendChild(mk('div','ds '+cls,dist.toFixed(4)));
      w.appendChild(row);
    });
    logMsg('Found '+d.items.length+' similar tracks',true);
  }).catch(function(e){
    cl(w);w.appendChild(mk('div','',e.message));
    logMsg('Search error: '+e.message,false)
  });
}

function makePlaylist(){
  var id=document.getElementById('si').value.trim();
  if(!id){logMsg('Enter a track ID first',false);return}
  logMsg('Creating playlist from similar tracks...');
  var w=document.getElementById('sr');
  w.appendChild(mk('div','','Creating playlist...'));
  var wt=getWeights();
  api('make_playlist',{
    item_id:id,genre_weight:wt.genre_weight,year_weight:wt.year_weight
  }).then(function(d){
    if(d.status==='created'){
      logMsg('Playlist "'+d.playlist_name+'" created with '+d.track_count+' tracks!',true);
      w.appendChild(mk('div','',
        'Playlist "'+d.playlist_name+'" created with '+d.track_count+' tracks'
      ));
    } else {
      logMsg('Playlist error: '+(d.error||d.status),false);
    }
  }).catch(function(e){logMsg('Playlist failed: '+e.message,false)});
}

function doRaw(){
  var ep=document.getElementById('ae').value;
  var ps=document.getElementById('ap').value;
  var o=document.getElementById('ro');
  o.textContent='Loading...';
  var params={};
  if(ps)ps.split('&').forEach(function(p){
    var kv=p.split('=');
    if(kv[0])params[kv[0].trim()]=(kv[1]||'').trim()
  });
  api(ep,params).then(function(d){o.textContent=JSON.stringify(d,null,2)})
  .catch(function(e){o.textContent='ERROR: '+e.message});
}

fetchStatus();fetchSigs();
</script></body></html>
"""
