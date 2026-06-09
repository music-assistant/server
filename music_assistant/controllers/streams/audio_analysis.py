"""Controller for distributing audio analysis to providers."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import os
import time
from collections.abc import AsyncGenerator, Iterable, Mapping
from math import inf
from typing import TYPE_CHECKING, Any

import torch
from music_assistant_models.audio_analysis import AudioAnalysisCoverage
from music_assistant_models.background_task import TaskSchedule
from music_assistant_models.enums import ContentType, MediaType, ProviderType, StreamType
from music_assistant_models.errors import ProviderUnavailableError

from music_assistant.constants import (
    CONF_BACKGROUND_SCAN_CONCURRENCY,
    DB_TABLE_AUDIO_ANALYSIS,
    DB_TABLE_PROVIDER_MAPPINGS,
    DEFAULT_BACKGROUND_SCAN_CONCURRENCY,
    LOUDNESS_MEASUREMENT_MIN_LUFS,
    MASS_LOGGER_NAME,
)
from music_assistant.helpers.api import api_command
from music_assistant.helpers.datetime import local_clock_time_to_utc
from music_assistant.helpers.json import json_dumps, json_loads
from music_assistant.models.audio_analysis import AudioAnalysisData
from music_assistant.models.audio_analysis_provider import AudioAnalysisProvider
from music_assistant.models.music_provider import MusicProvider

LOUDNESS_ANALYSIS_DOMAIN = "loudness_analysis"
SMART_FADES_ANALYSIS_DOMAIN = "smart_fades"
SONIC_ANALYSIS_DOMAIN = "sonic_analysis"
BACKGROUND_SCAN_TASK_ID = "audio_analysis_background_scan"
BACKGROUND_PER_TRACK_TIMEOUT_SECONDS = 300
BACKGROUND_PER_TRACK_TIMEOUT_DURATION_MULTIPLIER = 1.5
# Per-run wall-clock cap; in-flight tracks finish, new ones defer to the next run.
BACKGROUND_SCAN_RUN_BUDGET_SECONDS = 4 * 3600
# Per-chunk dispatch interval bounds. One PCM chunk = one audio-second of decoded data:
# the floor is the fastest pace allowed; the ceiling is both the slowest pace and the
# per-chunk processing timeout that evicts unresponsive providers.
REAL_TIME_PACE_INTERVAL_SECONDS_FLOOR = 0.100
REAL_TIME_PACE_INTERVAL_SECONDS_CEILING = 1.0
BACKGROUND_PACE_INTERVAL_SECONDS_FLOOR = 0.250
BACKGROUND_PACE_INTERVAL_SECONDS_CEILING = 4.0
ANALYSIS_QUEUE_MAXSIZE = 30
FILESYSTEM_PROVIDER_DOMAINS: tuple[str, ...] = (
    "filesystem_local",
    "filesystem_smb",
    "filesystem_nfs",
)

LOGGER = logging.getLogger(f"{MASS_LOGGER_NAME}.audio_analysis")

if TYPE_CHECKING:
    from music_assistant_models.media_items import AudioFormat
    from music_assistant_models.streamdetails import StreamDetails

    from music_assistant.controllers.streams.audio_buffer import AudioBuffer
    from music_assistant.controllers.streams.controller import StreamsController


def _parse_row(row: Mapping[str, Any]) -> AudioAnalysisData | None:
    """Parse a single audio_analysis row's analysis_data, logging and skipping on error."""
    try:
        return AudioAnalysisData.from_dict(json_loads(row["analysis_data"]))
    except (ValueError, TypeError, KeyError) as err:
        LOGGER.warning(
            "Skipping unparsable audio_analysis row (id=%s, aa_provider_domain=%s): %s",
            row.get("id"),
            row.get("aa_provider_domain"),
            err,
        )
        return None


def _merged_from_rows(
    rows: Iterable[Mapping[str, Any]],
    available_aa_domains: set[str],
    priority: tuple[str, ...] | None = None,
) -> AudioAnalysisData | None:
    """
    Fold audio_analysis rows into one merged result.

    Rows from AA providers not in available_aa_domains, and rows whose analysis_data
    is unparsable, are always skipped. Returns None when no usable row remains.

    :param rows: audio_analysis rows ordered oldest-first; each must carry
        aa_provider_domain and analysis_data.
    :param available_aa_domains: AA provider domains currently available.
    :param priority: When None, merge all available providers' rows with latest-write-wins
        (non-None fields). When a tuple of AA provider domains is given, only those domains
        are considered and the first-listed domain wins each per-field conflict.
    """
    merged = AudioAnalysisData()
    found = False
    if priority is None:
        for row in rows:
            if row["aa_provider_domain"] not in available_aa_domains:
                continue
            if (row_data := _parse_row(row)) is None:
                continue
            merged.update(row_data)
            found = True
        return merged if found else None

    # priority given: merge only these domains, first-listed wins each field.
    wanted = tuple(d for d in priority if d in available_aa_domains)
    wanted_set = set(wanted)
    by_domain: dict[str, AudioAnalysisData] = {}
    for row in rows:
        domain = row["aa_provider_domain"]
        if domain not in wanted_set or domain in by_domain:
            continue
        if (row_data := _parse_row(row)) is None:
            continue
        by_domain[domain] = row_data
    for domain in reversed(wanted):
        if (row_data := by_domain.get(domain)) is not None:
            merged.update(row_data)
            found = True
    return merged if found else None


class AudioAnalysisController:
    """Controller that distributes PCM chunks to all registered AudioAnalysisProviders."""

    def __init__(self, streams: StreamsController) -> None:
        """Initialize the AudioAnalysisController."""
        self.streams = streams
        self.mass = streams.mass
        self.logger = self.mass.logger.getChild("audio_analysis")
        self._active_sessions: dict[str, set[str]] = {}
        self._workers: dict[str, asyncio.Task[None]] = {}

    def setup(self) -> None:
        """Register the nightly background scan task and apply CPU caps."""
        self._configure_thread_caps()
        utc_hour, utc_minute = local_clock_time_to_utc(0, 0)
        self.mass.tasks.register_scheduled_task(
            task_id=BACKGROUND_SCAN_TASK_ID,
            name="Audio analysis — background scan of local files",
            handler=self._run_background_scan,
            schedule=TaskSchedule.daily(hour=utc_hour, minute=utc_minute),
            metadata={"task_domain": "audio_analysis"},
            allow_retry=True,
        )

    async def close(self) -> None:
        """Drain in-flight sessions and chunk workers on shutdown."""
        workers = list(self._workers.values())
        self._workers.clear()
        for worker in workers:
            if not worker.done():
                worker.cancel()
        for session_key in list(self._active_sessions):
            self._cancel_providers(session_key)
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)

    def _configure_thread_caps(self) -> None:
        """Cap PyTorch threading so Audio Analysis inference stays around a quarter of cpu_count."""
        budget = self._aa_thread_budget()
        torch.set_num_threads(budget)
        with contextlib.suppress(RuntimeError):
            # set_num_interop_threads can only be called before the first torch op
            torch.set_num_interop_threads(1)
        self.logger.info(
            "AudioAnalysis thread caps: torch intra=%d, torch interop=%d",
            torch.get_num_threads(),
            torch.get_num_interop_threads(),
        )

    @property
    def providers(self) -> list[AudioAnalysisProvider]:
        """Return all available audio analysis providers."""
        return [
            prov
            for prov in self.mass.get_providers(ProviderType.AUDIO_ANALYSIS)
            if isinstance(prov, AudioAnalysisProvider) and prov.available
        ]

    async def start_analysis(
        self,
        audio_buffer: AudioBuffer,
        streamdetails: StreamDetails,
    ) -> None:
        """
        Start analysis session for a track across all providers.

        :param audio_buffer: The AudioBuffer to observe for PCM chunks.
        :param streamdetails: The stream details for the item being analyzed.
        """
        providers = self.providers
        if not providers:
            self.logger.debug("No audio analysis providers available")
            return

        session_key = streamdetails.uri

        # Skip if another queue already has an analysis running for the same item
        if session_key in self._active_sessions:
            self.logger.debug(
                "Analysis session already active for %s, ignoring start request",
                session_key,
            )
            return

        provider_ids = await self._start_analysis_on_providers(
            session_key, streamdetails, audio_buffer.pcm_format, providers
        )
        if not provider_ids:
            self.logger.debug("No providers accepted analysis for %s", session_key)
            return

        self._active_sessions[session_key] = provider_ids
        queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=ANALYSIS_QUEUE_MAXSIZE)
        self._workers[session_key] = self.mass.create_task(
            self._chunk_worker(
                session_key,
                queue,
                min_interval=REAL_TIME_PACE_INTERVAL_SECONDS_FLOOR,
                max_interval=REAL_TIME_PACE_INTERVAL_SECONDS_CEILING,
            )
        )

        finalized = False

        async def _on_chunk(position_seconds: int, pcm_data: bytes, is_last_chunk: bool) -> None:  # noqa: ARG001
            nonlocal finalized
            if finalized or session_key not in self._active_sessions:
                return
            if is_last_chunk:
                finalized = True
                await queue.put(None)
                self.mass.create_task(_finalize_session())
                return
            try:
                await asyncio.wait_for(
                    queue.put(pcm_data), timeout=REAL_TIME_PACE_INTERVAL_SECONDS_CEILING
                )
            except (TimeoutError, asyncio.QueueFull):
                return

        async def _finalize_session() -> None:
            """Await the worker, then dispatch finalize to each provider."""
            worker = self._workers.pop(session_key, None)
            if worker is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await worker
            self._finalize_providers(session_key)

        def _on_cancel() -> None:
            self.logger.debug("Cancelling analysis session %s", session_key)
            worker = self._workers.pop(session_key, None)
            if worker is not None:
                worker.cancel()
            self._cancel_providers(session_key)

        audio_buffer.register_chunk_callback(_on_chunk)
        audio_buffer.register_cancel_callback(_on_cancel)

    async def set_audio_analysis(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        aa_provider_domain: str,
        analysis: AudioAnalysisData,
        analysis_version: int = 1,
        media_type: MediaType = MediaType.TRACK,
    ) -> None:
        """
        Store audio analysis results from an Audio Analysis provider.

        :param item_id: Provider-native item ID from streamdetails.item_id.
        :param provider_instance_id_or_domain: Music provider instance ID or domain.
        :param aa_provider_domain: Domain of the AA provider that produced the data.
        :param analysis: The analysis data to store.
        :param analysis_version: Version of the AA provider's algorithm.
        :param media_type: The media type of the item being analyzed.
        """
        provider = self.mass.get_provider(provider_instance_id_or_domain)
        if not isinstance(provider, MusicProvider):
            return
        prov_key = provider.domain if provider.is_streaming_provider else provider.instance_id
        data_json = json_dumps(analysis.to_dict())
        await self.mass.music.database.insert_or_replace(
            DB_TABLE_AUDIO_ANALYSIS,
            {
                "media_type": media_type.value,
                "item_id": item_id,
                "provider": prov_key,
                "aa_provider_domain": aa_provider_domain,
                "analysis_data": data_json,
                "analysis_version": analysis_version,
            },
        )

    async def get_audio_analysis(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        media_type: MediaType = MediaType.TRACK,
        priority: tuple[str, ...] | None = None,
    ) -> AudioAnalysisData | None:
        """
        Get merged audio analysis data for a track.

        Only rows from currently available AA providers are included.

        :param item_id: Provider-native item ID from streamdetails.item_id.
        :param provider_instance_id_or_domain: Music provider instance ID or domain.
        :param media_type: The media type of the item.
        :param priority: AA provider domains the values must come from. When None, all
            available providers are merged latest-write-wins. With a single domain, only
            that provider's values are used. With multiple domains, only those are merged
            and the first-listed domain wins each per-field conflict. Use this when a field
            (e.g. loudness_integrated) is written by several providers with different
            semantics, so the authoritative source is selected.
        """
        provider = self.mass.get_provider(provider_instance_id_or_domain)
        if not isinstance(provider, MusicProvider):
            return None
        prov_key = provider.domain if provider.is_streaming_provider else provider.instance_id
        rows = await self.mass.music.database.get_rows(
            DB_TABLE_AUDIO_ANALYSIS,
            {
                "item_id": item_id,
                "provider": prov_key,
                "media_type": media_type.value,
            },
            order_by="timestamp_created ASC",
        )
        if not rows:
            return None

        available_aa_domains = {
            p.domain for p in self.mass.get_providers(ProviderType.AUDIO_ANALYSIS) if p.available
        }
        return _merged_from_rows(rows, available_aa_domains, priority)

    async def set_track_loudness(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        loudness: float,
        loudness_album: float | None = None,
        media_type: MediaType = MediaType.TRACK,
    ) -> None:
        """
        Store track loudness measurement from an external source (tags, ReplayGain, etc).

        Persists the loudness values under the builtin loudness_analysis provider so
        the runtime ebur128 analysis will not re-analyze the track on playback.

        :param item_id: Provider-native item ID.
        :param provider_instance_id_or_domain: Music provider instance ID or domain.
        :param loudness: Integrated track loudness in LUFS.
        :param loudness_album: Optional album-level integrated loudness in LUFS.
        :param media_type: The media type of the item.
        """
        if loudness in (None, inf, -inf) or loudness <= LOUDNESS_MEASUREMENT_MIN_LUFS:
            return
        if (
            loudness_album is None
            or loudness_album in (inf, -inf)
            or loudness_album <= LOUDNESS_MEASUREMENT_MIN_LUFS
        ):
            loudness_album = None
        analysis = AudioAnalysisData(
            loudness_integrated=loudness,
            loudness_album=loudness_album,
        )
        await self.set_audio_analysis(
            item_id=item_id,
            provider_instance_id_or_domain=provider_instance_id_or_domain,
            aa_provider_domain=LOUDNESS_ANALYSIS_DOMAIN,
            analysis=analysis,
            media_type=media_type,
        )

    async def get_extra_data_for_album_tracks(
        self,
        track_item_ids: list[str],
        provider_instance_id_or_domain: str,
        aa_provider_domain: str,
    ) -> list[dict[str, Any]]:
        """
        Return one AA provider's ``extra_data`` for each given track that has one.

        :param track_item_ids: Provider-native track IDs to look up.
        :param provider_instance_id_or_domain: Music provider instance ID or domain.
        :param aa_provider_domain: Domain of the AA provider whose rows to fetch.
        """
        if not track_item_ids:
            return []
        provider = self.mass.get_provider(
            provider_instance_id_or_domain, provider_type=MusicProvider
        )
        if provider is None:
            return []
        prov_key = provider.domain if provider.is_streaming_provider else provider.instance_id

        placeholders = ",".join(f":id{i}" for i in range(len(track_item_ids)))
        params: dict[str, Any] = {f"id{i}": tid for i, tid in enumerate(track_item_ids)}
        params["provider"] = prov_key
        params["domain"] = aa_provider_domain
        params["media_type"] = MediaType.TRACK.value

        query = (
            f"SELECT analysis_data FROM {DB_TABLE_AUDIO_ANALYSIS} "
            f"WHERE aa_provider_domain = :domain "
            f"AND media_type = :media_type "
            f"AND provider = :provider "
            f"AND item_id IN ({placeholders})"
        )
        rows = await self.mass.music.database.get_rows_from_query(
            query, params, limit=len(track_item_ids)
        )

        results: list[dict[str, Any]] = []
        for row in rows:
            try:
                data = json_loads(row["analysis_data"])
            except (ValueError, TypeError):
                continue
            if not isinstance(data, dict):
                continue
            extra = data.get("extra_data")
            if isinstance(extra, dict):
                results.append(extra)
        return results

    async def get_audio_analysis_version(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        aa_provider_domain: str,
        media_type: MediaType = MediaType.TRACK,
    ) -> int | None:
        """
        Get the stored analysis version for a specific AA provider and track.

        :param item_id: Provider-native item ID from streamdetails.item_id.
        :param provider_instance_id_or_domain: Music provider instance ID or domain.
        :param aa_provider_domain: Domain of the AA provider.
        :param media_type: The media type of the item.
        """
        provider = self.mass.get_provider(provider_instance_id_or_domain)
        if not isinstance(provider, MusicProvider):
            return None
        prov_key = provider.domain if provider.is_streaming_provider else provider.instance_id
        row = await self.mass.music.database.get_row(
            DB_TABLE_AUDIO_ANALYSIS,
            {
                "item_id": item_id,
                "provider": prov_key,
                "aa_provider_domain": aa_provider_domain,
                "media_type": media_type.value,
            },
        )
        if not row:
            return None
        return int(row["analysis_version"])

    async def get_audio_analysis_count(
        self,
        aa_provider_domain: str,
        media_type: MediaType = MediaType.TRACK,
    ) -> int:
        """
        Count audio_analysis rows for a given aa_provider_domain.

        :param aa_provider_domain: Domain of the AA provider whose rows to count.
        :param media_type: The media type to count rows for.
        """
        return await self.mass.music.database.get_count_from_query(
            f"SELECT id FROM {DB_TABLE_AUDIO_ANALYSIS} "
            f"WHERE aa_provider_domain = :aa_provider_domain AND media_type = :media_type",
            {"aa_provider_domain": aa_provider_domain, "media_type": media_type.value},
        )

    async def iter_audio_analysis_rows(
        self,
        aa_provider_domain: str,
        media_type: MediaType = MediaType.TRACK,
    ) -> AsyncGenerator[Mapping[str, Any]]:
        """
        Stream audio_analysis rows for a given aa_provider_domain.

        :param aa_provider_domain: Domain of the AA provider whose rows to yield.
        :param media_type: The media type to filter rows by.
        """
        query = (
            f"SELECT * FROM {DB_TABLE_AUDIO_ANALYSIS} "
            f"WHERE aa_provider_domain = :aa_provider_domain AND media_type = :media_type"
        )
        async for row in self.mass.music.database.iter_rows_from_query(
            query,
            {"aa_provider_domain": aa_provider_domain, "media_type": media_type.value},
        ):
            yield row

    async def iter_merged_audio_analysis_rows(
        self,
        primary_aa_domain: str,
        media_type: MediaType = MediaType.TRACK,
        priority: tuple[str, ...] | None = None,
    ) -> AsyncGenerator[tuple[str, str, AudioAnalysisData]]:
        """
        Yield one merged AudioAnalysisData per track present in primary_aa_domain.

        Unlike get_audio_analysis, the music provider need not be loaded — rows
        are merged purely from the database, gated only on AA-provider
        availability. Used by bulk consumers (e.g. similarity index rebuild).

        Rows are streamed and grouped on the fly: only the rows for the
        currently-folding (item_id, provider) pair are held in memory at once,
        so peak memory is proportional to one track, not the whole library.

        If primary_aa_domain is not currently available, no rows can satisfy
        the availability gate and the generator yields nothing (a WARNING is
        logged so callers can distinguish "offline" from "empty").

        :param primary_aa_domain: AA provider domain that defines the universe of
            tracks to yield. Only (item_id, provider) pairs with at least one
            row in this domain are emitted.
        :param media_type: The media type to filter on.
        :param priority: AA provider domains the merged values must come from, first-listed
            wins per-field conflicts (see get_audio_analysis). When None, all available
            providers are merged latest-write-wins.
        """
        available_aa_domains = {
            p.domain for p in self.mass.get_providers(ProviderType.AUDIO_ANALYSIS) if p.available
        }
        if primary_aa_domain not in available_aa_domains:
            LOGGER.warning(
                "iter_merged_audio_analysis_rows called with offline primary AA domain "
                "%r; yielding no rows. Available domains: %s",
                primary_aa_domain,
                sorted(available_aa_domains),
            )
            return
        # EXISTS subquery scopes to the primary domain's universe at the DB level;
        # ORDER BY (item_id, provider, ts) lets us fold each track in one streaming pass.
        query = (
            f"SELECT item_id, provider, aa_provider_domain, analysis_data, id "
            f"FROM {DB_TABLE_AUDIO_ANALYSIS} aa1 "
            f"WHERE aa1.media_type = :media_type "
            f"AND EXISTS ("
            f"    SELECT 1 FROM {DB_TABLE_AUDIO_ANALYSIS} aa2 "
            f"    WHERE aa2.item_id = aa1.item_id "
            f"    AND aa2.provider = aa1.provider "
            f"    AND aa2.aa_provider_domain = :primary_aa_domain "
            f"    AND aa2.media_type = :media_type"
            f") "
            f"ORDER BY aa1.item_id, aa1.provider, aa1.timestamp_created ASC"
        )
        current_key: tuple[str, str] | None = None
        current_group: list[Mapping[str, Any]] = []
        async for row in self.mass.music.database.iter_rows_from_query(
            query,
            {"media_type": media_type.value, "primary_aa_domain": primary_aa_domain},
        ):
            key = (row["item_id"], row["provider"])
            if current_key is not None and key != current_key:
                merged = _merged_from_rows(current_group, available_aa_domains, priority)
                if merged is not None:
                    yield (*current_key, merged)
                current_group = []
            current_key = key
            current_group.append(row)
        if current_key is not None:
            merged = _merged_from_rows(current_group, available_aa_domains, priority)
            if merged is not None:
                yield (*current_key, merged)

    @api_command("audio_analysis/coverage")
    async def get_coverage(self, aa_domain: str) -> AudioAnalysisCoverage:
        """
        Return analysis-coverage health counts for an AA provider.

        :param aa_domain: AA provider domain to query.
        :returns: Counts where ``pending`` reflects filesystem-source tracks only;
            streaming-provider tracks are never considered for background analysis
            and are excluded.
        """
        provider = self.mass.get_provider(
            aa_domain,
            provider_type=AudioAnalysisProvider,  # type: ignore[type-abstract]
        )
        if provider is None:
            raise ProviderUnavailableError(f"{aa_domain} is not available")

        analyzed = await self.get_audio_analysis_count(aa_domain)
        pending = await self._count_candidates_missing_analysis(
            aa_domain, provider.analysis_version
        )
        # NULL analysis_version (pre-versioning rows) is treated as stale: SQLite
        # evaluates `NULL < N` as NULL (falsy), so it must be matched explicitly.
        stale_query = (
            f"SELECT id FROM {DB_TABLE_AUDIO_ANALYSIS} "
            f"WHERE aa_provider_domain = :aa_domain "
            f"  AND media_type = :media_type "
            f"  AND (analysis_version IS NULL OR analysis_version < :current_version)"
        )
        stale_version = await self.mass.music.database.get_count_from_query(
            stale_query,
            {
                "aa_domain": aa_domain,
                "media_type": MediaType.TRACK.value,
                "current_version": provider.analysis_version,
            },
        )
        return AudioAnalysisCoverage(
            analyzed=analyzed,
            pending=pending,
            stale_version=stale_version,
            analysis_version=provider.analysis_version,
        )

    async def _run_background_scan(self) -> None:
        """Run the scan as decode-once-fan-out streaming over candidate tracks."""
        providers = self.providers
        if not providers:
            return

        provider_versions = {p.domain: p.analysis_version for p in providers}
        candidates = await self._find_candidates_missing_analysis(provider_versions, limit=0)
        if not candidates:
            return

        scan_started = time.monotonic()
        run_deadline = scan_started + BACKGROUND_SCAN_RUN_BUDGET_SECONDS
        self.logger.info(
            "Background analysis (streaming): %d track(s) pending across %d provider(s); "
            "run budget %.1fh",
            len(candidates),
            len(providers),
            BACKGROUND_SCAN_RUN_BUDGET_SECONDS / 3600,
        )

        concurrency = self._get_scan_concurrency()
        semaphore = asyncio.Semaphore(concurrency)
        provider_by_domain = {p.domain: p for p in providers}

        processed = 0
        deferred = 0

        async def _run_one(candidate: dict[str, Any]) -> None:
            nonlocal processed, deferred
            async with semaphore:
                if time.monotonic() >= run_deadline:
                    deferred += 1
                    return

                item_id = candidate["item_id"]
                provider_instance = candidate["provider_instance"]
                missing = candidate["missing_domains"]

                music_prov = self.mass.get_provider(provider_instance, provider_type=MusicProvider)
                if music_prov is None or not music_prov.available:
                    self.logger.debug(
                        "Skipping %s: music provider %s unavailable", item_id, provider_instance
                    )
                    return

                try:
                    streamdetails = await music_prov.get_stream_details(item_id, MediaType.TRACK)
                except Exception as err:
                    self.logger.debug("Skipping %s: stream details failed: %s", item_id, err)
                    return

                if streamdetails.stream_type != StreamType.LOCAL_FILE:
                    return
                if not isinstance(streamdetails.path, str) or not streamdetails.path:
                    return

                providers_for_track = [
                    p
                    for p in (provider_by_domain.get(d) for d in missing)
                    if p is not None and p.available
                ]
                if not providers_for_track:
                    return

                await self._run_background_streaming_for_track(
                    streamdetails,
                    providers_for_track,
                )
                processed += 1

        await asyncio.gather(*(_run_one(c) for c in candidates))

        elapsed = time.monotonic() - scan_started
        if deferred:
            self.logger.info(
                "Background analysis: run-budget reached "
                "(%d processed, %d deferred to next run, %.1fs elapsed)",
                processed,
                deferred,
                elapsed,
            )
        else:
            self.logger.info(
                "Background analysis: complete (%d candidates processed in %.1fs)",
                processed,
                elapsed,
            )

    async def _run_background_streaming_for_track(
        self,
        streamdetails: StreamDetails,
        providers: list[AudioAnalysisProvider],
        min_interval: float = BACKGROUND_PACE_INTERVAL_SECONDS_FLOOR,
        max_interval: float = BACKGROUND_PACE_INTERVAL_SECONDS_CEILING,
    ) -> None:
        """
        Run a single track through the streaming pipeline using ffmpeg as the source.

        :param streamdetails: Stream details for the track being analyzed.
        :param providers: Audio analysis providers to dispatch chunks to.
        :param min_interval: Floor on wall-seconds between consecutive chunk dispatches.
        :param max_interval: Ceiling on wall-seconds between consecutive chunk dispatches.
        """
        session_key = streamdetails.uri
        if session_key in self._active_sessions:
            self.logger.debug(
                "Background streaming: session already active for %s, skipping", session_key
            )
            return

        # Floor at the fixed budget so short tracks keep ffmpeg-startup headroom.
        timeout_seconds = max(
            BACKGROUND_PER_TRACK_TIMEOUT_SECONDS,
            int((streamdetails.duration or 0) * BACKGROUND_PER_TRACK_TIMEOUT_DURATION_MULTIPLIER),
        )

        try:
            await asyncio.wait_for(
                self._run_background_streaming_inner(
                    session_key,
                    streamdetails,
                    providers,
                    min_interval=min_interval,
                    max_interval=max_interval,
                ),
                timeout=timeout_seconds,
            )
        except asyncio.CancelledError:
            # CancelledError inherits from BaseException — the broad except below
            # does not catch it. Clean up the session, then re-raise.
            self.logger.debug("Background analysis cancelled for %s", session_key)
            self._cancel_providers(session_key)
            raise
        except TimeoutError:
            self.logger.warning(
                "Background analysis exceeded %ds budget for %s, skipping",
                timeout_seconds,
                session_key,
            )
            self._cancel_providers(session_key)
            self.mass.tasks.add_task_failure(
                BACKGROUND_SCAN_TASK_ID,
                f"Timed out after {timeout_seconds}s: {session_key}",
            )
        except Exception as err:
            self.logger.warning("Background analysis failed for %s: %s", session_key, err)
            self._cancel_providers(session_key)
            self.mass.tasks.add_task_failure(
                BACKGROUND_SCAN_TASK_ID,
                f"Failed: {session_key}: {err}",
            )

    async def _run_background_streaming_inner(
        self,
        session_key: str,
        streamdetails: StreamDetails,
        providers: list[AudioAnalysisProvider],
        min_interval: float = BACKGROUND_PACE_INTERVAL_SECONDS_FLOOR,
        max_interval: float = BACKGROUND_PACE_INTERVAL_SECONDS_CEILING,
    ) -> None:
        """
        Inner body of _run_background_streaming_for_track, wrapped by wait_for.

        :param session_key: Active-session key for this track.
        :param streamdetails: Stream details for the track being analyzed.
        :param providers: Audio analysis providers to dispatch chunks to.
        :param min_interval: Floor on wall-seconds between consecutive chunk dispatches.
        :param max_interval: Ceiling on wall-seconds between consecutive chunk dispatches.
        """
        if not isinstance(streamdetails.path, str) or not streamdetails.path:
            return

        # Override content_type so ffmpeg decodes rather than re-muxing the source codec.
        pcm_format = dataclasses.replace(
            streamdetails.audio_format,
            content_type=ContentType.from_bit_depth(streamdetails.audio_format.bit_depth),
        )

        accepted = await self._start_analysis_on_providers(
            session_key, streamdetails, pcm_format, providers
        )
        if not accepted:
            self.logger.debug("No providers accepted background analysis for %s", session_key)
            return
        self._active_sessions[session_key] = accepted

        audio_source = self.mass.streams.audio.get_media_stream(streamdetails, pcm_format)
        next_allowed = time.monotonic()
        async for chunk in audio_source:
            if session_key not in self._active_sessions:
                # all providers evicted — bail early
                break
            now = time.monotonic()
            if now < next_allowed:
                await asyncio.sleep(next_allowed - now)
            await self._distribute_chunk(session_key, chunk, max_interval=max_interval)
            next_allowed = time.monotonic() + min_interval
        if session_key in self._active_sessions:
            self._finalize_providers(session_key)

    def _available_filesystem_domains(self) -> tuple[str, ...]:
        """Return configured filesystem provider domains that are currently available."""
        return tuple(
            domain
            for domain in FILESYSTEM_PROVIDER_DOMAINS
            if any(
                p.domain == domain and p.available
                for p in self.mass.get_providers(ProviderType.MUSIC)
            )
        )

    async def _find_candidates_missing_analysis(
        self,
        aa_provider_versions: Mapping[str, int],
        limit: int,
    ) -> list[dict[str, Any]]:
        """
        Return tracks that need (re)analysis for one or more AA providers.

        A track is a candidate for a given AA provider domain when it has no
        analysis row for that domain, or when its stored row predates the
        provider's current analysis_version (a NULL stored version, from
        pre-versioning rows, is also treated as stale). This mirrors the
        per-track version gate in AudioAnalysisProvider.start_analysis so a
        provider bumping its analysis_version triggers a background re-scan.

        :param aa_provider_versions: Mapping of AA provider domain to the
            provider's current analysis_version.
        :param limit: Maximum number of candidate rows to return (0 for no limit).
        :returns: Rows {item_id, provider_instance, missing_domains} where
            missing_domains lists the AA provider domains needing analysis.
        """
        if not aa_provider_versions:
            return []

        filesystem_domains = self._available_filesystem_domains()
        if not filesystem_domains:
            return []

        # CROSS JOIN (track x possible domain), keep pairs with no up-to-date analysis
        # row, GROUP_CONCAT the missing domains per track.
        aa_domains = list(aa_provider_versions)
        fs_inline = ", ".join(f"'{d}'" for d in filesystem_domains)
        aa_select_terms = " UNION ALL ".join(
            f"SELECT :aa_{i} AS aa_provider_domain, :ver_{i} AS current_version"
            for i in range(len(aa_domains))
        )
        params: dict[str, Any] = {
            "media_type": MediaType.TRACK.value,
            **{f"aa_{i}": d for i, d in enumerate(aa_domains)},
            **{f"ver_{i}": aa_provider_versions[d] for i, d in enumerate(aa_domains)},
        }
        # The NOT EXISTS gate only counts an analysis row as up-to-date when its
        # analysis_version is non-NULL and >= the provider's current version, so
        # missing rows and stale-version rows both surface as candidates.
        query = (
            f"SELECT pm.provider_item_id AS item_id, "
            f"       pm.provider_instance AS provider_instance, "
            f"       GROUP_CONCAT(possible.aa_provider_domain) AS missing_domains "
            f"FROM {DB_TABLE_PROVIDER_MAPPINGS} pm "
            f"CROSS JOIN ({aa_select_terms}) possible "
            f"WHERE pm.media_type = :media_type "
            f"  AND pm.provider_domain IN ({fs_inline}) "
            f"  AND NOT EXISTS ("
            f"    SELECT 1 FROM {DB_TABLE_AUDIO_ANALYSIS} aa "
            f"    WHERE aa.item_id = pm.provider_item_id "
            f"      AND aa.provider = pm.provider_instance "
            f"      AND aa.aa_provider_domain = possible.aa_provider_domain "
            f"      AND aa.media_type = :media_type "
            f"      AND aa.analysis_version IS NOT NULL "
            f"      AND aa.analysis_version >= possible.current_version"
            f"  ) "
            f"GROUP BY pm.provider_item_id, pm.provider_instance"
        )
        rows = await self.mass.music.database.get_rows_from_query(query, params, limit=limit)
        results: list[dict[str, Any]] = []
        for r in rows:
            missing_raw = r["missing_domains"]
            if not missing_raw:
                continue
            results.append(
                {
                    "item_id": str(r["item_id"]),
                    "provider_instance": str(r["provider_instance"]),
                    "missing_domains": sorted(set(missing_raw.split(","))),
                }
            )
        return results

    async def _count_candidates_missing_analysis(self, aa_domain: str, current_version: int) -> int:
        """Count filesystem candidate tracks needing (re)analysis for aa_domain.

        A track is counted when it has no analysis row for the domain, or when
        its stored analysis_version is NULL or less than current_version.
        """
        filesystem_domains = self._available_filesystem_domains()
        if not filesystem_domains:
            return 0
        fs_inline = ", ".join(f"'{d}'" for d in filesystem_domains)
        query = (
            f"SELECT pm.provider_item_id FROM {DB_TABLE_PROVIDER_MAPPINGS} pm "
            f"WHERE pm.media_type = :media_type "
            f"  AND pm.provider_domain IN ({fs_inline}) "
            f"  AND NOT EXISTS ("
            f"    SELECT 1 FROM {DB_TABLE_AUDIO_ANALYSIS} aa "
            f"    WHERE aa.item_id = pm.provider_item_id "
            f"      AND aa.provider = pm.provider_instance "
            f"      AND aa.aa_provider_domain = :aa_domain "
            f"      AND aa.media_type = :media_type "
            f"      AND aa.analysis_version IS NOT NULL "
            f"      AND aa.analysis_version >= :current_version"
            f"  )"
        )
        return await self.mass.music.database.get_count_from_query(
            query,
            {
                "media_type": MediaType.TRACK.value,
                "aa_domain": aa_domain,
                "current_version": current_version,
            },
        )

    async def _start_analysis_on_providers(
        self,
        session_key: str,
        streamdetails: StreamDetails,
        audio_format: AudioFormat,
        providers: list[AudioAnalysisProvider],
    ) -> set[str]:
        """Call start_analysis on each provider, returning IDs of those that accepted."""
        provider_ids: set[str] = set()
        for provider in providers:
            try:
                if await provider.start_analysis(
                    session_id=session_key,
                    streamdetails=streamdetails,
                    audio_format=audio_format,
                ):
                    provider_ids.add(provider.instance_id)
            except Exception as err:
                self.logger.warning(
                    "Failed to start analysis on provider %s: %s", provider.name, err
                )
        return provider_ids

    def _finalize_providers(self, session_key: str) -> None:
        """Finalize each provider in the session."""
        provider_ids = self._active_sessions.pop(session_key, None)
        if not provider_ids:
            return
        for provider_id in provider_ids:
            provider = self.mass.get_provider(provider_id)
            if provider and isinstance(provider, AudioAnalysisProvider) and provider.available:
                self.mass.create_task(provider.finalize(session_key))

    def _cancel_providers(self, session_key: str) -> None:
        """Cancel each provider in the session."""
        provider_ids = self._active_sessions.pop(session_key, None)
        if not provider_ids:
            return
        for provider_id in provider_ids:
            provider = self.mass.get_provider(provider_id)
            if provider and isinstance(provider, AudioAnalysisProvider) and provider.available:
                self.mass.create_task(provider.cancel(session_key))

    async def _distribute_chunk(
        self,
        session_key: str,
        pcm_data: bytes,
        max_interval: float = REAL_TIME_PACE_INTERVAL_SECONDS_CEILING,
    ) -> None:
        """
        Fan a single PCM chunk to every provider in the session.

        :param session_key: Active-session key for the dispatch.
        :param pcm_data: The 1-second PCM chunk to hand to each provider.
        :param max_interval: Per-provider processing timeout; providers exceeding this are evicted.
        """
        provider_ids = self._active_sessions.get(session_key)
        if not provider_ids:
            return

        async def _process(prov_id: str) -> str | None:
            try:
                provider = self.mass.get_provider(prov_id)
                if not (
                    provider and isinstance(provider, AudioAnalysisProvider) and provider.available
                ):
                    return None
                await asyncio.wait_for(
                    provider.process_pcm_chunk(session_key, pcm_data),
                    timeout=max_interval,
                )
            except TimeoutError:
                self.logger.warning(
                    "Provider %s timed out processing chunk for %s, removing from session",
                    prov_id,
                    session_key,
                )
                return prov_id
            except Exception as err:
                self.logger.warning("Error processing PCM chunk on provider %s: %s", prov_id, err)
                return prov_id
            return None

        results = await asyncio.gather(*[_process(prov_id) for prov_id in provider_ids])
        evicted = {prov_id for prov_id in results if prov_id is not None}
        if evicted:
            for prov_id in evicted:
                provider = self.mass.get_provider(prov_id)
                if provider and isinstance(provider, AudioAnalysisProvider) and provider.available:
                    self.mass.create_task(provider.cancel(session_key))
            provider_ids -= evicted
            if not provider_ids:
                self._active_sessions.pop(session_key, None)

    async def _chunk_worker(
        self,
        session_key: str,
        queue: asyncio.Queue[bytes | None],
        min_interval: float = REAL_TIME_PACE_INTERVAL_SECONDS_FLOOR,
        max_interval: float = REAL_TIME_PACE_INTERVAL_SECONDS_CEILING,
    ) -> None:
        """
        Background worker that processes queued PCM chunks via _distribute_chunk.

        :param session_key: Active-session key for this worker.
        :param queue: Queue receiving raw PCM chunks from the live producer.
        :param min_interval: Floor on wall-seconds between consecutive chunk dispatches.
        :param max_interval: Ceiling on wall-seconds between consecutive chunk dispatches.
        """
        next_allowed = time.monotonic()
        while True:
            chunk = await queue.get()
            if chunk is None:
                break
            if session_key not in self._active_sessions:
                break
            now = time.monotonic()
            if now < next_allowed:
                await asyncio.sleep(next_allowed - now)
            await self._distribute_chunk(session_key, chunk, max_interval=max_interval)
            next_allowed = time.monotonic() + min_interval
            if session_key not in self._active_sessions:
                # all providers evicted by _distribute_chunk
                self._workers.pop(session_key, None)
                break

    def _aa_thread_budget(self) -> int:
        """Return the per-op PyTorch intra-op thread budget for inference (~25% of cpu_count)."""
        return max(1, (os.process_cpu_count() or os.cpu_count() or 4) // 4)

    def _get_scan_concurrency(self) -> int:
        """Read background scan concurrency from config, clamped to [1, 8]."""
        try:
            value = int(
                self.mass.config.get_raw_core_config_value(
                    "streams",
                    CONF_BACKGROUND_SCAN_CONCURRENCY,
                    DEFAULT_BACKGROUND_SCAN_CONCURRENCY,
                )
                or DEFAULT_BACKGROUND_SCAN_CONCURRENCY
            )
        except Exception:
            value = DEFAULT_BACKGROUND_SCAN_CONCURRENCY
        return max(1, min(value, 8))
