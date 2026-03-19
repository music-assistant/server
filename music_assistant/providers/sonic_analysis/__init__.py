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
    """
    Return Config entries to setup this provider.

    instance_id: id of an existing provider instance (None if new instance setup).
    action: [optional] action key called from config entries UI.
    values: the (intermediate) raw values for config entries sent with the action.
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
    _analysis_semaphore: asyncio.Semaphore

    async def handle_async_init(self) -> None:
        """Handle async initialisation: create DB table, load corpus stats, init ANN index."""
        self._on_unload = []
        self.corpus_means = None
        self.corpus_stds = None
        self._search_index = None
        self._label_map: dict[int, tuple[str, str]] = {}

        max_concurrent = int(self.config.get_value(CONF_MAX_CONCURRENT_ANALYSES) or 2)  # type: ignore[arg-type]
        self._analysis_semaphore = asyncio.Semaphore(max_concurrent)

        await self._create_db_table()
        await self._load_corpus_stats()
        self._init_search_index()

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
        ):
            self._on_unload.append(
                self.mass.webserver.register_dynamic_route(path, handler, "GET")
            )
            self._on_unload.append(
                self.mass.webserver.register_dynamic_route(
                    path, self._handle_cors_preflight, "OPTIONS"
                )
            )

        if self.config.get_value(CONF_ANALYZE_ON_SYNC):
            self.mass.create_task(self._backfill_unanalyzed_tracks())

    async def _backfill_unanalyzed_tracks(self) -> None:
        """Background task: analyze all local tracks without signatures."""
        self.logger.info("Starting background sonic analysis backfill...")
        analyzed_count = 0
        skipped_count = 0

        try:
            tracks = await self.mass.music.tracks.library_items()
        except Exception:
            self.logger.warning("Could not fetch library tracks for backfill", exc_info=True)
            return

        for track in tracks:
            item_id = str(track.item_id)

            has_signature = False
            for mapping in track.provider_mappings:
                existing = await self.get_sonic_signature(item_id, mapping.provider_instance)
                if existing:
                    has_signature = True
                    break

            if has_signature:
                skipped_count += 1
                continue

            for mapping in track.provider_mappings:
                try:
                    await self._fetch_and_analyze(
                        item_id, mapping.provider_instance, mapping.item_id
                    )
                    analyzed_count += 1
                    break
                except Exception:
                    self.logger.debug(
                        "Backfill: failed to analyze track %s via %s",
                        item_id,
                        mapping.provider_instance,
                    )
                    continue

            await asyncio.sleep(0)

        if analyzed_count > 0:
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

        Runs the blocking librosa extraction in a thread to avoid blocking the event loop.
        Uses _analysis_semaphore to cap the number of concurrent analyses.

        :param item_id: Provider-scoped track identifier.
        :param provider_instance: Provider domain or instance ID that owns the track.
        :param audio: Mono float32 audio samples.
        :param sample_rate: Sample rate of the audio in Hz.
        """
        try:
            async with self._analysis_semaphore:
                signature: SonicSignature = await asyncio.to_thread(
                    extract_signature, audio, sample_rate
                )
        except Exception:
            self.logger.warning("Feature extraction failed for %s/%s", provider_instance, item_id)
            return None

        await self.set_sonic_signature(item_id, provider_instance, signature)

        if self.corpus_means is not None and self.corpus_stds is not None:
            normalized = normalize_features(signature.features, self.corpus_means, self.corpus_stds)
            # Use a stable integer label derived from item_id for the ANN index.
            # Python's built-in hash is deterministic within a process but may vary
            # across runs; for now this is acceptable as the index is rebuilt on startup.
            label = abs(hash((provider_instance, item_id))) % (2**31)
            self._label_map[label] = (item_id, provider_instance)
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

        :param item_id: Library track identifier (used for DB storage).
        :param provider: Provider instance ID that owns the track.
        :param provider_item_id: Provider-scoped track ID for stream details.
            Falls back to item_id if not provided.
        """
        try:
            audio: np.ndarray
            sample_rate = 22050
            prov_item_id = provider_item_id or item_id

            stream_details: Any = None
            try:
                provider_instance: Any = self.mass.get_provider(provider)
                if provider_instance is not None and hasattr(
                    provider_instance, "get_stream_details"
                ):
                    stream_details = await provider_instance.get_stream_details(
                        prov_item_id, MediaType.TRACK
                    )
            except Exception:
                self.logger.debug("Could not resolve stream details for %s/%s", provider, item_id)

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
                # Streaming track analysis is intentionally not supported in v1.
                # Only local filesystem tracks with a resolvable path are analysed.
                # This can be extended in a future iteration using a PCM pipeline.
                self.logger.debug(
                    "Skipping analysis for %s/%s: no local file path available."
                    " Streaming track analysis is not supported in v1.",
                    provider,
                    item_id,
                )
                return

            await self._analyze_track(item_id, provider, audio, sample_rate)
        except Exception as exc:
            self.logger.warning("Analysis failed for %s/%s: %s", provider, item_id, exc)

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

        Returns tracks whose sonic signatures are nearest to the seed track in the
        ANN index.  Query parameters:

        :param request: Incoming aiohttp web request.
        """
        item_id: str | None = request.query.get("item_id")
        if not item_id:
            return _cors_text("Missing required query parameter: item_id", status=400)

        try:
            limit = int(request.query.get("limit", 25))
        except (TypeError, ValueError):
            limit = 25
        limit = min(limit, 100)

        # Look up the seed track's signature in the DB.
        # We iterate over all providers; the first hit wins.
        signature = None
        seed_provider = ""
        assert self.mass.music.database is not None
        rows = await self.mass.music.database.get_rows(
            DB_TABLE_SONIC_SIGNATURES,
            {"item_id": item_id},
        )
        for row in rows:
            if row.get("item_id") == CORPUS_STATS_ITEM_ID:
                continue
            try:
                features: list[float] = json.loads(row["features"])
                signature = SonicSignature(features=features, version=int(row["version"]))
                seed_provider = row.get("provider", "")
                break
            except (KeyError, ValueError, TypeError):
                continue

        if signature is None:
            return _cors_json({"analyzed": False, "items": [], "seed_track_id": item_id})

        if self.corpus_means is None or self.corpus_stds is None:
            return _cors_json({"analyzed": False, "items": [], "seed_track_id": item_id})

        normalized = normalize_features(signature.features, self.corpus_means, self.corpus_stds)
        # Request one extra result so we can discard the seed itself.
        raw_results = self._query_index(normalized, k=limit + 1)

        # Compute the seed label using the provider from the DB row so it matches
        # the hash used at insertion time (fixes hardcoded "local" provider bug).
        seed_label = abs(hash((seed_provider, item_id))) % (2**31)

        items: list[dict[str, Any]] = []
        for result_id, distance in raw_results:
            if result_id == seed_label:
                continue
            if len(items) >= limit:
                break
            # Resolve the ANN label back to (item_id, provider) via the in-memory map.
            resolved = self._label_map.get(result_id)
            if resolved is None:
                continue
            resolved_item_id, resolved_provider = resolved
            items.append(
                {"item_id": resolved_item_id, "provider": resolved_provider, "distance": distance}
            )

        return _cors_json({"analyzed": True, "items": items, "seed_track_id": item_id})

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

        # Count signatures in DB
        db_count = 0
        if self.mass.music.database is not None:
            try:
                rows = await self.mass.music.database.get_rows(
                    DB_TABLE_SONIC_SIGNATURES, match=None, limit=0
                )
                db_count = sum(
                    1 for r in rows if r.get("item_id") != CORPUS_STATS_ITEM_ID
                )
            except Exception:
                db_count = -1

        return _cors_json({
            "index_size": index_size,
            "db_signatures": db_count,
            "label_map_size": label_map_size,
            "has_corpus_stats": has_corpus_stats,
            "signature_version": SIGNATURE_VERSION,
            "signature_dimensions": SIGNATURE_DIMENSIONS,
            "analyze_on_play": bool(self.config.get_value(CONF_ANALYZE_ON_PLAY)),
            "analyze_on_sync": bool(self.config.get_value(CONF_ANALYZE_ON_SYNC)),
        })

    async def _handle_signatures(self, request: Any) -> Any:
        """Handle GET /api/sonic_analysis/signatures — list stored signatures."""
        limit_str = request.query.get("limit", "50")
        offset_str = request.query.get("offset", "0")
        try:
            limit = min(int(limit_str), 500)
            offset = int(offset_str)
        except (TypeError, ValueError):
            limit, offset = 50, 0

        if self.mass.music.database is None:
            return _cors_json({"signatures": [], "total": 0})

        all_rows = await self.mass.music.database.get_rows(
            DB_TABLE_SONIC_SIGNATURES, match=None, limit=0
        )
        track_rows = [r for r in all_rows if r.get("item_id") != CORPUS_STATS_ITEM_ID]
        total = len(track_rows)
        page = track_rows[offset : offset + limit]

        signatures = []
        for row in page:
            signatures.append({
                "item_id": row["item_id"],
                "provider": row["provider"],
                "version": row["version"],
                "feature_count": len(json.loads(row["features"])) if row.get("features") else 0,
            })

        return _cors_json({"signatures": signatures, "total": total})

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

        track_rows = [row for row in all_rows if row.get("item_id") != CORPUS_STATS_ITEM_ID]

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

        means, stds = compute_corpus_stats(all_features)
        await self._save_corpus_stats(means, stds)

        if not _USEARCH_AVAILABLE:
            return

        self._search_index = _usearch_index_module.Index(
            ndim=SIGNATURE_DIMENSIONS,
            metric=MetricKind.Cos,
            dtype=ScalarKind.I8,
        )
        self._label_map = {}

        for item_id, provider_instance, features in parsed:
            normalized = normalize_features(features, means, stds)
            label = abs(hash((provider_instance, item_id))) % (2**31)
            self._label_map[label] = (item_id, provider_instance)
            self._add_to_index(label, normalized)

        await asyncio.to_thread(self._save_search_index)
        self.logger.info("Search index rebuilt with %d entries.", len(parsed))

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider.

        Called when provider is deregistered (e.g. MA exiting or config reloading).

        :param is_removed: True when the provider is permanently removed from configuration.
        """
        for unload_cb in self._on_unload:
            unload_cb()
        await asyncio.to_thread(self._save_search_index)
