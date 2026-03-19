"""Sonic Analysis plugin provider."""

from __future__ import annotations

import asyncio
import io
import json
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
from aiohttp import web
from music_assistant_models.config_entries import ConfigEntry, ConfigValueType
from music_assistant_models.enums import ConfigEntryType, EventType, ProviderFeature

from music_assistant.constants import DB_TABLE_SONIC_SIGNATURES
from music_assistant.helpers.sonic_analysis import (
    SIGNATURE_DIMENSIONS,
    SIGNATURE_VERSION,
    SonicSignature,
    compute_corpus_stats,
    extract_signature,
    normalize_features,
)
from music_assistant.models.plugin import PluginProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.event import MassEvent
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES: set[ProviderFeature] = set()

VOYAGER_INDEX_FILENAME = "sonic_signatures.voy"
CORPUS_STATS_ITEM_ID = "__corpus_stats__"

CONF_ANALYZE_ON_PLAY = "analyze_on_play"
CONF_ANALYZE_ON_SYNC = "analyze_on_sync"
CONF_MAX_CONCURRENT_ANALYSES = "max_concurrent_analyses"

try:
    import voyager
except ImportError:
    voyager = None


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


class SonicAnalysisProvider(PluginProvider):
    """Plugin provider that extracts sonic signatures and enables similarity-based discovery."""

    corpus_means: list[float] | None
    corpus_stds: list[float] | None
    _on_unload: list[Callable[[], None]]
    _voyager_index: Any  # voyager.Index — typed as Any to avoid hard import at class level
    _analysis_semaphore: asyncio.Semaphore

    async def handle_async_init(self) -> None:
        """Handle async initialisation: create DB table, load corpus stats, init ANN index."""
        self._on_unload = []
        self.corpus_means = None
        self.corpus_stds = None
        self._voyager_index = None

        max_concurrent = int(self.config.get_value(CONF_MAX_CONCURRENT_ANALYSES) or 2)  # type: ignore[arg-type]
        self._analysis_semaphore = asyncio.Semaphore(max_concurrent)

        await self._create_db_table()
        await self._load_corpus_stats()
        self._init_voyager_index()

    async def loaded_in_mass(self) -> None:
        """Subscribe to library and playback events based on configuration."""
        await super().loaded_in_mass()

        if self.config.get_value(CONF_ANALYZE_ON_PLAY):
            self._on_unload.append(
                self.mass.subscribe(self._on_media_item_played, EventType.MEDIA_ITEM_PLAYED)
            )

        if self.config.get_value(CONF_ANALYZE_ON_SYNC):
            self._on_unload.append(
                self.mass.subscribe(self._on_media_item_added, EventType.MEDIA_ITEM_ADDED)
            )

        self._on_unload.append(
            self.mass.webserver.register_dynamic_route(
                "/api/sonic_analysis/similar",
                self._handle_similar_tracks,
                "GET",
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
                    await self._fetch_and_analyze(item_id, mapping.provider_instance)
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
            await self._rebuild_voyager_index()

        self.logger.info(
            "Backfill complete: %d analyzed, %d already had signatures",
            analyzed_count,
            skipped_count,
        )

    async def _on_media_item_played(self, event: MassEvent) -> None:
        """Handle media item played — analyze if no signature exists."""
        from music_assistant_models.enums import MediaType
        from music_assistant_models.playback_progress_report import (
            MediaItemPlaybackProgressReport,
        )

        report = event.data
        if not isinstance(report, MediaItemPlaybackProgressReport):
            return
        if report.media_type != MediaType.TRACK:
            return

        from music_assistant.helpers.uri import parse_uri

        try:
            _media_type, provider, item_id = await parse_uri(report.uri)
        except Exception:
            return

        existing = await self.get_sonic_signature(item_id, provider)
        if existing is not None:
            return

        self.mass.create_task(self._fetch_and_analyze(item_id, provider))

    async def _on_media_item_added(self, event: MassEvent) -> None:
        """Handle media item added — queue for background analysis."""
        from music_assistant_models.enums import MediaType
        from music_assistant_models.media_items import Track

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
            # Use a stable integer label derived from item_id for the Voyager index.
            # Python's built-in hash is deterministic within a process but may vary
            # across runs; for now this is acceptable as the index is rebuilt on startup.
            label = abs(hash((provider_instance, item_id))) % (2**31)
            self._add_to_index(label, normalized)

        return signature

    async def _fetch_and_analyze(self, item_id: str, provider: str) -> None:
        """Fetch audio for a track and run the analysis pipeline.

        For local files the path is read directly via librosa; for streamed content
        a PCM pipeline is used (not yet implemented — placeholder logs a warning).

        :param item_id: Provider-scoped track identifier.
        :param provider: Provider domain or instance ID that owns the track.
        """
        import librosa

        try:
            audio: np.ndarray
            sample_rate = 22050

            # Attempt to resolve a local file path via the provider's stream details.
            # Cast to Any so mypy does not enforce the MusicProvider.get_stream_details
            # signature, which requires a media_type argument that is irrelevant here
            # because we only want the path for local-file providers.
            stream_details: Any = None
            try:
                provider_instance: Any = self.mass.get_provider(provider)
                if provider_instance is not None and hasattr(
                    provider_instance, "get_stream_details"
                ):
                    stream_details = await provider_instance.get_stream_details(item_id)
            except Exception:
                pass

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
                self.logger.warning(
                    "No local path available for %s/%s; streaming analysis not yet supported",
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

    def _init_voyager_index(self) -> None:
        """Initialize or load the Voyager ANN index."""
        index_path = Path(self.mass.storage_path) / VOYAGER_INDEX_FILENAME
        if index_path.exists():
            with open(index_path, "rb") as f:
                self._voyager_index = voyager.Index.load(f)
        else:
            self._voyager_index = voyager.Index(
                voyager.Space.Cosine,
                num_dimensions=SIGNATURE_DIMENSIONS,
                storage_data_type=voyager.StorageDataType.E4M3,
            )

    def _add_to_index(self, item_id_int: int, normalized_features: list[float]) -> None:
        """Add a normalised feature vector to the ANN index with an explicit item ID.

        :param item_id_int: Integer library item ID used as the Voyager label.
        :param normalized_features: Z-score normalised feature values, one per dimension.
        """
        vector_2d = np.array([normalized_features], dtype=np.float32)
        self._voyager_index.add_items(vector_2d, ids=np.array([item_id_int], dtype=np.int64))

    def _query_index(
        self, normalized_features: list[float], k: int = 25
    ) -> list[tuple[int, float]]:
        """Query the ANN index for the k nearest neighbours.

        Returns an empty list when the index has no elements.
        When k exceeds num_elements the query is clamped automatically.

        :param normalized_features: Per-feature z-score normalised query vector.
        :param k: Maximum number of nearest neighbours to return.
        """
        if self._voyager_index.num_elements == 0:
            return []

        effective_k = min(k, self._voyager_index.num_elements)
        query_2d = np.array([normalized_features], dtype=np.float32)
        ids_2d, distances_2d = self._voyager_index.query(query_2d, k=effective_k)
        return [(int(ids_2d[0][i]), float(distances_2d[0][i])) for i in range(len(ids_2d[0]))]

    def _save_voyager_index(self) -> None:
        """Persist the Voyager ANN index to storage_path."""
        if self._voyager_index is None:
            return

        index_path = Path(self.mass.storage_path) / VOYAGER_INDEX_FILENAME
        if not index_path.parent.exists():
            return

        # voyager.Index.save(path_str) is unreliable on Windows; writing via BytesIO is safe.
        buf = io.BytesIO()
        self._voyager_index.save(buf)
        buf.seek(0)
        index_path.write_bytes(buf.read())

    async def _handle_similar_tracks(self, request: Any) -> Any:
        """Handle GET /api/sonic_analysis/similar endpoint.

        Returns tracks whose sonic signatures are nearest to the seed track in the
        Voyager ANN index.  Query parameters:

        :param request: Incoming aiohttp web request.
        """
        item_id: str | None = request.query.get("item_id")
        if not item_id:
            return web.Response(status=400, text="Missing required query parameter: item_id")

        try:
            limit = int(request.query.get("limit", 25))
        except (TypeError, ValueError):
            limit = 25
        limit = min(limit, 100)

        # Look up the seed track's signature in the DB.
        # We iterate over all providers; the first hit wins.
        signature = None
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
                break
            except (KeyError, ValueError, TypeError):
                continue

        if signature is None:
            return web.json_response({"analyzed": False, "items": [], "seed_track_id": item_id})

        if self.corpus_means is None or self.corpus_stds is None:
            return web.json_response({"analyzed": False, "items": [], "seed_track_id": item_id})

        normalized = normalize_features(signature.features, self.corpus_means, self.corpus_stds)
        # Request one extra result so we can discard the seed itself.
        raw_results = self._query_index(normalized, k=limit + 1)

        seed_label = abs(hash(("local", item_id))) % (2**31)
        items: list[dict[str, Any]] = []
        for result_id, distance in raw_results:
            if result_id == seed_label:
                continue
            if len(items) >= limit:
                break
            # Voyager labels are opaque integer hashes; we cannot recover the
            # original (provider, item_id) pair needed by TracksController.get,
            # so we expose the raw label and distance for callers to resolve.
            items.append({"id": result_id, "distance": distance})

        return web.json_response({"analyzed": True, "items": items, "seed_track_id": item_id})

    async def _rebuild_voyager_index(self) -> None:
        """Rebuild the Voyager index from all signatures in the DB.

        Fetches every row from the sonic_signatures table, recomputes corpus statistics,
        creates a fresh E4M3 index, and populates it with normalised feature vectors.
        Useful for recovery after index loss or a schema migration.
        """
        assert self.mass.music.database is not None
        all_rows = await self.mass.music.database.get_rows(DB_TABLE_SONIC_SIGNATURES, {})

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

        self._voyager_index = voyager.Index(
            voyager.Space.Cosine,
            num_dimensions=SIGNATURE_DIMENSIONS,
            storage_data_type=voyager.StorageDataType.E4M3,
        )

        for item_id, provider_instance, features in parsed:
            normalized = normalize_features(features, means, stds)
            label = abs(hash((provider_instance, item_id))) % (2**31)
            self._add_to_index(label, normalized)

        self._save_voyager_index()
        self.logger.info("Voyager index rebuilt with %d entries.", len(parsed))

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider.

        Called when provider is deregistered (e.g. MA exiting or config reloading).

        :param is_removed: True when the provider is permanently removed from configuration.
        """
        for unload_cb in self._on_unload:
            unload_cb()
        self._save_voyager_index()
