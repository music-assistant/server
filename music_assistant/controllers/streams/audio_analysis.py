"""
Controller for distributing audio analysis to providers.

Analysis rows live in `audio_analysis.db` (attached onto the music connection as
schema `aa`); see `setup_database`.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import os
import sqlite3
import sys
import time
from collections.abc import AsyncGenerator, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from math import isfinite
from typing import TYPE_CHECKING, Any, Final

from music_assistant_models.audio_analysis import AudioAnalysisCoverage
from music_assistant_models.auth import Scope
from music_assistant_models.background_task import TaskSchedule
from music_assistant_models.enums import ContentType, MediaType, ProviderType, StreamType
from music_assistant_models.errors import ProviderUnavailableError
from music_assistant_models.media_items import AudioMetadata

from music_assistant.constants import (
    CONF_BACKGROUND_SCAN_CONCURRENCY,
    DB_TABLE_AUDIO_ANALYSIS,
    DB_TABLE_AUDIO_ANALYSIS_FAILURES,
    DB_TABLE_PROVIDER_MAPPINGS,
    DB_TABLE_SETTINGS,
    DEFAULT_BACKGROUND_SCAN_CONCURRENCY,
    LOUDNESS_MEASUREMENT_MIN_LUFS,
    MASS_LOGGER_NAME,
)
from music_assistant.controllers.streams.audio_analysis_codec import decode, encode
from music_assistant.controllers.streams.audio_buffer import AudioBufferDiscarded, AudioBufferEOF
from music_assistant.helpers.api import api_command
from music_assistant.helpers.datetime import local_clock_time_to_utc, utc_timestamp
from music_assistant.helpers.json import json_loads
from music_assistant.helpers.util import inference_thread_budget, is_arm
from music_assistant.models.audio_analysis import AudioAnalysisData, AudioAnalysisError
from music_assistant.models.audio_analysis_provider import (
    AudioAnalysisProvider,
    InstrumentedSemaphore,
)
from music_assistant.models.music_provider import MusicProvider

LOUDNESS_ANALYSIS_DOMAIN = "loudness_analysis"
# Virtual AA domain for loudness supplied by music providers (tags, ReplayGain). No AA
# provider backs it, so it bypasses the provider-availability gate when merging rows.
PROVIDER_LOUDNESS_DOMAIN = "provider_loudness"
# Playback normalization prefers provider-supplied loudness over the builtin measurement.
LOUDNESS_PROVIDER_PRIORITY = (PROVIDER_LOUDNESS_DOMAIN, LOUDNESS_ANALYSIS_DOMAIN)
SMART_FADES_ANALYSIS_DOMAIN = "smart_fades"
SONIC_ANALYSIS_DOMAIN = "sonic_analysis"
# AA domains trusted for frontend-facing track data (bpm/key/waveform), authoritative first.
TRACK_EXPORT_AA_PRIORITY = (SMART_FADES_ANALYSIS_DOMAIN, SONIC_ANALYSIS_DOMAIN)
BACKGROUND_SCAN_TASK_ID = "audio_analysis_background_scan"
# The analysis tables live in their own SQLite file, attached onto the music connection
# under this schema name so candidate/coverage queries can still join provider_mappings.
AA_DB_SCHEMA: Final[str] = "aa"
AA_DB_FILENAME: Final[str] = "audio_analysis.db"
AA_DB_SCHEMA_VERSION: Final[int] = 2
AA_TABLE_ANALYSIS: Final[str] = f"{AA_DB_SCHEMA}.{DB_TABLE_AUDIO_ANALYSIS}"
# JSON-shaped analysis table of schema v1, renamed aside at startup and converted away
DB_TABLE_AUDIO_ANALYSIS_V1: Final[str] = f"{DB_TABLE_AUDIO_ANALYSIS}_v1"
AA_TABLE_ANALYSIS_V1: Final[str] = f"{AA_DB_SCHEMA}.{DB_TABLE_AUDIO_ANALYSIS_V1}"
AA_TABLE_FAILURES: Final[str] = f"{AA_DB_SCHEMA}.{DB_TABLE_AUDIO_ANALYSIS_FAILURES}"
AA_TABLE_SETTINGS: Final[str] = f"{AA_DB_SCHEMA}.{DB_TABLE_SETTINGS}"
# Legacy failure rows are copied out of library.db in id ranges of this size, one
# transaction each.
RELOCATE_BATCH_SIZE: Final[int] = 5000
# JSON rows are converted to the packed format in cursor batches of this size, one
# transaction each; a fully analysed row is ~230 KB of JSON, so a batch is held in memory
# twice (decoded and packed) while it converts. Progress is logged once per this many rows.
MIGRATE_BATCH_SIZE: Final[int] = 100
MIGRATE_PROGRESS_ROWS: Final[int] = 2000
BACKGROUND_PER_TRACK_TIMEOUT_SECONDS = 300
BACKGROUND_PER_TRACK_TIMEOUT_DURATION_MULTIPLIER = 1.5
# Per-run wall-clock cap; in-flight tracks finish, new ones defer to the next run.
BACKGROUND_SCAN_RUN_BUDGET_SECONDS = 4 * 3600
# Per-chunk processing ceiling for live and background analysis; a provider that exceeds it is
# treated as stuck and evicted. Generous because analysis runs one offload at a time while a
# player streams, so a chunk may wait behind other work before it computes.
CHUNK_HANG_GUARD_SECONDS = 120.0
# Floor on wall-seconds between consecutive background chunk dispatches (one chunk = one
# audio-second), capping each scanned track at ~4x realtime so a background analyse doesn't
# consume all resources. Nice and slow is preferred for nightly background scans.
BACKGROUND_PACE_INTERVAL_SECONDS_FLOOR = 0.250
# OS nice value for analysis worker threads (Linux): keeps analysis below playback so the
# scheduler favors the event loop and ffmpeg under contention.
ANALYSIS_THREAD_NICE = 10
# Cap on concurrent realtime analysis sessions (the playing track plus the preloaded next).
# Rapid track skipping would otherwise spawn an analysis per abandoned track; the oldest is
# evicted to keep the count bounded.
REALTIME_ANALYSIS_MAX_SESSIONS = 2
# Minimum fraction of the expected track duration that must have been received before an
# ended stream is finalized. A source that ends far short of it (e.g. a stream that died
# without raising an error) is discarded instead, so no truncated analysis is persisted.
ANALYSIS_MIN_COMPLETENESS_RATIO = 0.9
# Free the heavy analysis models after this long with no analysis activity; they are reloaded
# on the next track. Long enough that gaps between tracks/sessions don't thrash the reload.
MODEL_IDLE_UNLOAD_SECONDS = 300
MODEL_IDLE_CHECK_INTERVAL_SECONDS = 60
# Background analysis is deliberately limited to the user's own files: pulling a streaming
# service's catalogue for audio nobody asked to hear is not something we do. Keep it that way.
FILESYSTEM_PROVIDER_DOMAINS: tuple[str, ...] = (
    "filesystem_local",
    "filesystem_smb",
    "filesystem_nfs",
)

_FAILURE_COLUMNS: Final[tuple[str, ...]] = (
    "media_type",
    "item_id",
    "provider",
    "aa_provider_domain",
    "reason",
    "analysis_version",
    "next_retry",
    "timestamp_created",
)

LOGGER = logging.getLogger(f"{MASS_LOGGER_NAME}.audio_analysis")

if TYPE_CHECKING:
    from datetime import datetime

    from music_assistant_models.media_items import AudioFormat, Track
    from music_assistant_models.streamdetails import StreamDetails

    from music_assistant.controllers.streams.audio_buffer import AudioBuffer
    from music_assistant.controllers.streams.controller import StreamsController


def _get_row_value(row: Mapping[str, Any], key: str) -> Any:
    """Return a database row value without assuming dict-only helpers."""
    try:
        return row[key]
    except IndexError, KeyError, TypeError:
        return None


def _parse_row(
    row: Mapping[str, Any],
    unparsable_ids: list[Any] | None = None,
) -> AudioAnalysisData | None:
    """
    Parse a single audio_analysis row's packed record, logging and skipping on error.

    :param row: The audio_analysis row to parse.
    :param unparsable_ids: When given, the id of a row that fails to parse is appended.
    """
    try:
        return decode(row["header"], row["payload"])
    except (IndexError, KeyError, TypeError, ValueError) as err:
        row_id = _get_row_value(row, "id")
        # the error itself may embed the full (huge) field value, so log only
        # the error type plus the offending field name when available
        error_detail = type(err).__name__
        if field_name := getattr(err, "field_name", None):
            error_detail = f"{error_detail} in field {field_name}"
        LOGGER.warning(
            "Skipping unparsable audio_analysis row (id=%s, domain=%s, error=%s)",
            row_id,
            _get_row_value(row, "aa_provider_domain"),
            error_detail,
        )
        if unparsable_ids is not None and row_id is not None:
            unparsable_ids.append(row_id)
        return None


def _parse_row_json(
    row: Mapping[str, Any],
    unparsable_ids: list[Any] | None = None,
) -> AudioAnalysisData | None:
    """
    Parse a single JSON-shaped audio_analysis row, logging and skipping on error.

    Only rows still in the pre-v2 format go through this; the packed format is read by
    :func:`_parse_row`.

    :param row: The audio_analysis row to parse.
    :param unparsable_ids: When given, the id of a row that fails to parse is appended.
    """
    try:
        return AudioAnalysisData.from_dict(json_loads(row["analysis_data"]))
    except (IndexError, KeyError, TypeError, ValueError) as err:
        row_id = _get_row_value(row, "id")
        # the error itself may embed the full (huge) field value, so log only
        # the error type plus the offending field name when available
        error_detail = type(err).__name__
        if field_name := getattr(err, "field_name", None):
            error_detail = f"{error_detail} in field {field_name}"
        LOGGER.warning(
            "Skipping unparsable audio_analysis row (id=%s, domain=%s, error=%s)",
            row_id,
            _get_row_value(row, "aa_provider_domain"),
            error_detail,
        )
        if unparsable_ids is not None and row_id is not None:
            unparsable_ids.append(row_id)
        return None


def _pack_rows(rows: list[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Decode JSON rows through the model and pack them; returns (rows, unreadable count)."""
    packed: list[dict[str, Any]] = []
    unreadable = 0
    for row in rows:
        analysis = _parse_row_json(row)
        if analysis is None:
            unreadable += 1
            continue
        try:
            header, payload = encode(analysis)
        except (TypeError, ValueError) as err:
            # one row that cannot be packed must not stall the whole conversion
            LOGGER.warning(
                "Skipping unpackable audio_analysis row (id=%s, domain=%s, error=%s)",
                _get_row_value(row, "id"),
                _get_row_value(row, "aa_provider_domain"),
                type(err).__name__,
            )
            unreadable += 1
            continue
        packed.append(
            {
                "media_type": row["media_type"],
                "item_id": row["item_id"],
                "provider": row["provider"],
                "aa_provider_domain": row["aa_provider_domain"],
                "analysis_version": row["analysis_version"],
                "timestamp_created": row["timestamp_created"],
                "header": header,
                "payload": payload,
            }
        )
    return packed, unreadable


def _merged_from_rows(
    rows: Iterable[Mapping[str, Any]],
    available_aa_domains: set[str],
    priority: tuple[str, ...] | None = None,
    unparsable_ids: list[Any] | None = None,
) -> AudioAnalysisData | None:
    """
    Fold audio_analysis rows into one merged result.

    Rows from AA providers not in available_aa_domains, and rows whose packed record
    is unparsable, are always skipped. Returns None when no usable row remains.

    :param rows: audio_analysis rows ordered oldest-first; each must carry
        aa_provider_domain, header and payload.
    :param available_aa_domains: AA provider domains currently available.
    :param priority: When None, merge all available providers' rows with latest-write-wins
        (non-None fields). When a tuple of AA provider domains is given, only those domains
        are considered and the first-listed domain wins each per-field conflict.
    :param unparsable_ids: When given, ids of rows whose packed record fails to parse
        are appended.
    """
    merged = AudioAnalysisData()
    found = False
    if priority is None:
        for row in rows:
            if row["aa_provider_domain"] not in available_aa_domains:
                continue
            if (row_data := _parse_row(row, unparsable_ids)) is None:
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
        if (row_data := _parse_row(row, unparsable_ids)) is None:
            continue
        by_domain[domain] = row_data
    for domain in reversed(wanted):
        if (row_data := by_domain.get(domain)) is not None:
            merged.update(row_data)
            found = True
    return merged if found else None


def _first_non_finite_field(analysis: AudioAnalysisData) -> str | None:
    """Return the name of the first float field holding a non-finite value, if any."""
    for fld in dataclasses.fields(analysis):
        value = getattr(analysis, fld.name)
        if isinstance(value, float):
            if not isfinite(value):
                return fld.name
        elif isinstance(value, list) and any(
            isinstance(item, float) and not isfinite(item) for item in value
        ):
            return fld.name
    return None


def _nice_analysis_worker() -> None:
    """
    Lower the OS scheduling priority of the calling analysis worker thread.

    Runs once per worker thread (ThreadPoolExecutor initializer). Linux-only, where the nice
    value is per-thread and so affects just this pool; a no-op on other platforms.
    """
    if sys.platform != "linux" or not hasattr(os, "setpriority"):
        return
    with contextlib.suppress(OSError):
        os.setpriority(os.PRIO_PROCESS, 0, ANALYSIS_THREAD_NICE)


class AudioAnalysisController:
    """Controller that distributes PCM chunks to all registered AudioAnalysisProviders."""

    def __init__(self, streams: StreamsController) -> None:
        """Initialize the AudioAnalysisController."""
        self.streams = streams
        self.mass = streams.mass
        self.logger = self.mass.logger.getChild("audio_analysis")
        self._database_ready = False
        self._active_sessions: dict[str, set[str]] = {}
        self._workers: dict[str, asyncio.Task[None]] = {}
        # Realtime session key -> queue id, insertion-ordered, so the session cap is applied
        # per queue (concurrent queues don't evict each other's still-playing analysis).
        self._session_queues: dict[str, str] = {}
        self._inference_runtime_configured = False
        # Bounds how many analysis offloads run concurrently to half the cores; created in
        # ensure_inference_runtime_configured once the core count is known (None until then),
        # and honored by AudioAnalysisProvider._run_offloaded.
        self.analysis_semaphore: InstrumentedSemaphore | None = None
        # Held by an analysis offload while any player streams, capping analysis to one offload
        # at a time; honored by AudioAnalysisProvider._run_offloaded.
        self.analysis_solo_lock: asyncio.Lock | None = None
        # Niced worker pool that runs analysis offloads, so the lower priority applies to
        # analysis threads only; created in ensure_inference_runtime_configured.
        self.analysis_executor: ThreadPoolExecutor | None = None
        # Monotonic time of the last analysis start, and the monitor that unloads idle models.
        self._last_analysis_activity: float = 0.0
        self._idle_unload_task: asyncio.Task[None] | None = None
        # In-flight provider finalizes: their session is already gone, but the models are not.
        self._finalize_tasks: set[asyncio.Task[None]] = set()

    def setup(self) -> None:
        """Register the nightly background scan task."""
        utc_hour, utc_minute = local_clock_time_to_utc(0, 0)
        self.mass.tasks.register_scheduled_task(
            task_id=BACKGROUND_SCAN_TASK_ID,
            name="Audio analysis — background scan of local files",
            handler=self._run_background_scan,
            schedule=TaskSchedule.daily(hour=utc_hour, minute=utc_minute),
            metadata={"task_domain": "audio_analysis"},
            allow_retry=True,
        )

    async def setup_database(self) -> None:
        """
        Attach the audio analysis database and make sure its tables exist.

        Rows left in the JSON format of an earlier version — in this file, or older still
        in library.db — are converted to the packed format on the way. A conversion that
        cannot be verified keeps its source table and is retried on the next start, so the
        schema version is only raised once nothing is left to convert.

        Safe to call more than once. Must run after the music database connection exists
        (it is attached onto that connection) and before any analysis query.
        """
        db = self.mass.music.database
        db_path = os.path.join(self.mass.storage_path, AA_DB_FILENAME)
        self._database_ready = False
        try:
            try:
                await self._attach_and_create(db_path)
            except sqlite3.DatabaseError as err:
                # Extended result codes retain the primary SQLite code in the low byte.
                if getattr(err, "sqlite_errorcode", 0) & 0xFF not in (
                    sqlite3.SQLITE_CORRUPT,
                    sqlite3.SQLITE_NOTADB,
                ):
                    raise
                self.logger.error(
                    "Audio analysis database %s is unusable (%s); moving it aside and starting over",
                    db_path,
                    err,
                )
                await self._quarantine_database(db_path)
                await self._attach_and_create(db_path)
            await self._prepare_analysis_table()
            converted = await self._migrate_json_rows(AA_TABLE_ANALYSIS_V1)
            legacy_converted = await self._migrate_json_rows(f"main.{DB_TABLE_AUDIO_ANALYSIS}")
            moved = await self._relocate_legacy_failures()
            if (
                moved is None
                or await self._table_exists(AA_DB_SCHEMA, DB_TABLE_AUDIO_ANALYSIS_V1)
                or await self._table_exists("main", DB_TABLE_AUDIO_ANALYSIS)
            ):
                raise ProviderUnavailableError("Legacy audio analysis relocation is incomplete")
            await db.insert_or_replace(
                AA_TABLE_SETTINGS,
                {"key": "version", "value": str(AA_DB_SCHEMA_VERSION), "type": "str"},
            )
            await db.commit()
        except (sqlite3.Error, OSError, ValueError, ProviderUnavailableError) as err:
            self.logger.error(
                "Audio analysis unavailable: %s (%s). Playback remains available; "
                "check storage and database compatibility, then restart to retry",
                db_path,
                err,
            )
            return
        self._database_ready = True
        if converted + legacy_converted > 0:
            await self._compact_analysis_database(converted + legacy_converted)
        if moved + legacy_converted > 0:
            self.logger.info(
                "Compacting library.db after moving %s audio analysis rows",
                moved + legacy_converted,
            )
            try:
                await db.vacuum()
            except sqlite3.Error as err:
                self.logger.warning("Compacting library.db failed: %s", err)

    async def close(self) -> None:
        """Drain in-flight sessions and chunk workers on shutdown."""
        tasks = list(self._workers.values())
        self._workers.clear()
        if self._idle_unload_task is not None:
            tasks.append(self._idle_unload_task)
            self._idle_unload_task = None
        for task in tasks:
            if not task.done():
                task.cancel()
        for session_key in list(self._active_sessions):
            self._cancel_providers(session_key)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self.analysis_executor is not None:
            # A running CPU-bound thread can't be cancelled, so shut down without waiting on it.
            self.analysis_executor.shutdown(wait=False, cancel_futures=True)
            self.analysis_executor = None

    def ensure_inference_runtime_configured(self) -> None:
        """
        Configure the on-device inference runtime for analysis (process-wide, applied once).

        Torch-backed analysis providers call this at the start of their handle_async_init,
        before loading their models.
        """
        if self._inference_runtime_configured:
            return
        # Lazy import: only torch-backed providers call this, so a host running no such
        # provider never imports torch. Running before the first model load also lets
        # set_num_interop_threads take effect (only settable before the first op).
        import torch  # noqa: PLC0415

        budget = self._aa_thread_budget()
        torch.set_num_threads(budget)
        with contextlib.suppress(RuntimeError):
            # set_num_interop_threads can only be called before the first torch op
            torch.set_num_interop_threads(1)
        # torch.set_num_threads only governs torch's own ops. The per-block librosa/numpy
        # feature extraction runs through the native BLAS pool (OpenBLAS), which is capped to
        # the same budget from the environment at process start (cap_native_thread_pools);
        # it cannot be capped from here without deadlocking against a concurrent import.
        arm = is_arm()
        if arm:
            # NNPACK frequently fails to initialize on ARM SBCs (e.g. Raspberry Pi); torch
            # then re-logs "Could not initialize NNPACK" to stderr on every conv op. The fp32
            # conv fallback is used on those hosts regardless, so disabling it only removes
            # the log spam.
            with contextlib.suppress(RuntimeError):
                torch.backends.nnpack.set_flags(False)  # type: ignore[no-untyped-call]
        # Cap concurrent analysis offloads to half the cores so analysis (live or background)
        # never occupies the whole box and starves playback/the host — slow and steady on any
        # machine. Applies to every host; honored by AudioAnalysisProvider._run_offloaded.
        concurrency_cap = max(1, self._cpu_count() // 2)
        self.analysis_semaphore = InstrumentedSemaphore(concurrency_cap)
        self.analysis_solo_lock = asyncio.Lock()
        # Niced pool sized to the idle cap plus headroom; the semaphore and solo lock bound
        # how many of its threads run at once.
        self.analysis_executor = ThreadPoolExecutor(
            max_workers=max(2, self._cpu_count()),
            thread_name_prefix="analysis",
            initializer=_nice_analysis_worker,
        )
        self.logger.info(
            "AudioAnalysis runtime: torch intra=%d interop=%d, blas<=%s, "
            "analysis concurrency<=%d (1 while a player streams), nnpack=%s",
            torch.get_num_threads(),
            torch.get_num_interop_threads(),
            os.environ.get("OPENBLAS_NUM_THREADS", "uncapped"),
            concurrency_cap,
            "off" if arm else "on",
        )
        # Only mark done once configuration actually succeeded, so a failure retries.
        self._inference_runtime_configured = True

    @property
    def database_ready(self) -> bool:
        """Return whether the analysis database and its migrations are ready."""
        return self._database_ready

    @property
    def providers(self) -> list[AudioAnalysisProvider]:
        """Return all available audio analysis providers."""
        if not self._database_ready:
            return []
        return [
            prov
            for prov in self.mass.get_providers(ProviderType.AUDIO_ANALYSIS)
            if isinstance(prov, AudioAnalysisProvider) and prov.available
        ]

    @property
    def smart_fades_provider_available(self) -> bool:
        """Return whether the smart fades audio analysis provider is loaded and available."""
        return any(prov.domain == SMART_FADES_ANALYSIS_DOMAIN for prov in self.providers)

    def playback_active(self) -> bool:
        """Return whether a queue stream is actively serving a player right now."""
        return self.streams.output_stream_active()

    async def start_analysis(
        self,
        audio_buffer: AudioBuffer,
        streamdetails: StreamDetails,
    ) -> None:
        """
        Start analysis session for a track across all providers.

        :param audio_buffer: The shared playback AudioBuffer the analysis reads PCM from.
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

        # Bound concurrent realtime sessions per queue, evicting the oldest in this queue (the
        # current track and its preloaded next are the youngest, so they survive a burst of
        # skips). Scoping per queue keeps simultaneous queues from evicting each other.
        queue_id = streamdetails.queue_id or session_key
        in_queue = [key for key, qid in self._session_queues.items() if qid == queue_id]
        for stale_key in in_queue[: max(0, len(in_queue) - REALTIME_ANALYSIS_MAX_SESSIONS + 1)]:
            self._evict_realtime_session(stale_key)

        self._active_sessions[session_key] = provider_ids
        self._session_queues[session_key] = queue_id
        worker = self.mass.create_task(
            self._buffer_reader_worker(session_key, audio_buffer, streamdetails.duration)
        )
        self._workers[session_key] = worker

        def _on_cancel() -> None:
            # Buffer torn down (track skipped / inactivity) — free the session.
            self._evict_realtime_session(session_key)

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
        :raises ValueError: When a float field of the analysis holds a non-finite value.
        """
        self._require_database()
        # non-finite floats serialize to JSON null, which corrupts the stored row;
        # refuse them here so a bad payload can never poison the database
        if (field_name := _first_non_finite_field(analysis)) is not None:
            raise ValueError(
                f"audio analysis for {item_id} contains a non-finite value in {field_name}"
            )
        provider = self.mass.get_provider(provider_instance_id_or_domain)
        if not isinstance(provider, MusicProvider):
            return
        prov_key = provider.domain if provider.is_streaming_provider else provider.instance_id
        header, payload = encode(analysis)
        await self.mass.music.database.insert_or_replace(
            AA_TABLE_ANALYSIS,
            {
                "media_type": media_type.value,
                "item_id": item_id,
                "provider": prov_key,
                "aa_provider_domain": aa_provider_domain,
                "analysis_version": analysis_version,
                "header": header,
                "payload": payload,
            },
        )
        await self.clear_analysis_failure(
            item_id=item_id,
            provider_instance_id_or_domain=provider_instance_id_or_domain,
            aa_provider_domain=aa_provider_domain,
            media_type=media_type,
        )

    async def record_analysis_failure(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        aa_provider_domain: str,
        reason: str,
        retry_at: datetime | None = None,
        analysis_version: int = 1,
        media_type: MediaType = MediaType.TRACK,
    ) -> None:
        """
        Record an analysis failure for a track.

        No-op when the provider does not resolve to a loaded music provider.

        :param item_id: Provider-native item ID from streamdetails.item_id.
        :param provider_instance_id_or_domain: Music provider instance ID or domain.
        :param aa_provider_domain: Domain of the AA provider that failed.
        :param reason: Human-readable failure reason.
        :param retry_at: Timezone-aware datetime when to allow a retry; None (default)
            means never auto-retry.
        :param analysis_version: The AA provider's algorithm version at failure time.
        :param media_type: The media type of the item.
        """
        self._require_database()
        provider = self.mass.get_provider(provider_instance_id_or_domain)
        if not isinstance(provider, MusicProvider):
            self.logger.debug(
                "Skipping failure record for %s: not a loaded music provider",
                provider_instance_id_or_domain,
            )
            return
        prov_key = provider.domain if provider.is_streaming_provider else provider.instance_id
        await self.mass.music.database.insert_or_replace(
            AA_TABLE_FAILURES,
            {
                "media_type": media_type.value,
                "item_id": item_id,
                "provider": prov_key,
                "aa_provider_domain": aa_provider_domain,
                "reason": reason,
                "analysis_version": analysis_version,
                "next_retry": int(retry_at.timestamp()) if retry_at is not None else None,
            },
        )

    async def clear_analysis_failure(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        aa_provider_domain: str,
        media_type: MediaType = MediaType.TRACK,
    ) -> None:
        """
        Delete a recorded analysis failure (e.g. after a later success).

        No-op when the provider does not resolve to a loaded music provider.

        :param item_id: Provider-native item ID from streamdetails.item_id.
        :param provider_instance_id_or_domain: Music provider instance ID or domain.
        :param aa_provider_domain: Domain of the AA provider whose failure to clear.
        :param media_type: The media type of the item.
        """
        self._require_database()
        provider = self.mass.get_provider(provider_instance_id_or_domain)
        if not isinstance(provider, MusicProvider):
            self.logger.debug(
                "Skipping failure clear for %s: not a loaded music provider",
                provider_instance_id_or_domain,
            )
            return
        prov_key = provider.domain if provider.is_streaming_provider else provider.instance_id
        await self.mass.music.database.delete(
            AA_TABLE_FAILURES,
            {
                "item_id": item_id,
                "provider": prov_key,
                "aa_provider_domain": aa_provider_domain,
                "media_type": media_type.value,
            },
        )

    async def delete_audio_analysis(
        self,
        item_id: str,
        provider_key: str,
        media_type: MediaType = MediaType.TRACK,
    ) -> None:
        """
        Delete every AA provider's analysis and failure rows for one provider mapping.

        :param item_id: Provider-native item ID.
        :param provider_key: Stored music-provider key (domain or instance_id).
        :param media_type: The media type of the item.
        """
        self._require_database()
        for table in (AA_TABLE_ANALYSIS, AA_TABLE_FAILURES):
            await self.mass.music.database.delete(
                table,
                {"media_type": media_type.value, "item_id": item_id, "provider": provider_key},
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

        Only rows from currently available AA providers are included; the virtual
        provider_loudness domain is always included. Rows that fail to parse are
        deleted, so the track can be re-analyzed.

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
        if not self._database_ready:
            return None
        provider = self.mass.get_provider(provider_instance_id_or_domain)
        if not isinstance(provider, MusicProvider):
            return None
        prov_key = provider.domain if provider.is_streaming_provider else provider.instance_id
        rows = await self.mass.music.database.get_rows(
            AA_TABLE_ANALYSIS,
            {
                "item_id": item_id,
                "provider": prov_key,
                "media_type": media_type.value,
            },
            order_by="timestamp_created ASC",
        )
        if not rows:
            return None

        available_aa_domains = self._available_aa_domains()
        unparsable_ids: list[Any] = []
        merged = _merged_from_rows(rows, available_aa_domains, priority, unparsable_ids)
        # corrupt rows would otherwise block re-analysis forever (their stored
        # analysis_version still gates new sessions), so drop them right away
        for row_id in unparsable_ids:
            await self.mass.music.database.delete(AA_TABLE_ANALYSIS, {"id": row_id})
        if unparsable_ids:
            self.logger.info(
                "Deleted %d corrupt audio_analysis row(s) for %s/%s; "
                "the item is eligible for re-analysis",
                len(unparsable_ids),
                prov_key,
                item_id,
            )
        return merged

    async def get_track_audio_metadata(self, track: Track) -> AudioMetadata | None:
        """
        Return AudioMetadata (bpm, musical key) for a track, or None when no analysis exists.

        Provider mappings are tried best-quality first; per field the Smart Fades AA
        provider is preferred over other AA providers.

        :param track: The track to look up stored analysis data for.
        """
        priority = TRACK_EXPORT_AA_PRIORITY
        for mapping in sorted(track.provider_mappings, key=lambda m: m.quality, reverse=True):
            analysis = await self.get_audio_analysis(
                mapping.item_id, mapping.provider_instance, priority=priority
            )
            if analysis is None or (analysis.bpm is None and analysis.key is None):
                continue
            musical_key: str | None = None
            if analysis.key is not None:
                musical_key = f"{analysis.key} {analysis.mode}" if analysis.mode else analysis.key
            return AudioMetadata(bpm=analysis.bpm, musical_key=musical_key)
        return None

    @api_command("audio_analysis/wave_form")
    async def get_wave_form(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
    ) -> list[float] | None:
        """
        Return the RMS energy waveform for a track, or None when no analysis exists.

        The waveform is a fixed array of 1800 bins (normalized 0.0-1.0) evenly covering
        the track duration. Values come from the Smart Fades AA provider when available,
        falling back to any other AA provider that stored RMS energy.

        :param item_id: Provider-native item ID.
        :param provider_instance_id_or_domain: Music provider instance ID or domain.
        """
        analysis = await self.get_audio_analysis(
            item_id,
            provider_instance_id_or_domain,
            priority=TRACK_EXPORT_AA_PRIORITY,
        )
        if analysis is None or analysis.rms_energy is None:
            return None
        return [float(value) for value in analysis.rms_energy]

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

        Persists the values under the virtual provider_loudness AA domain; during
        playback they take precedence over the builtin loudness measurement.

        :param item_id: Provider-native item ID.
        :param provider_instance_id_or_domain: Music provider instance ID or domain.
        :param loudness: Integrated track loudness in LUFS.
        :param loudness_album: Optional album-level integrated loudness in LUFS.
        :param media_type: The media type of the item.
        """
        if not self._database_ready:
            return
        if loudness is None or not isfinite(loudness) or loudness <= LOUDNESS_MEASUREMENT_MIN_LUFS:
            return
        if (
            loudness_album is None
            or not isfinite(loudness_album)
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
            aa_provider_domain=PROVIDER_LOUDNESS_DOMAIN,
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
        if not self._database_ready:
            return []
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
            # fetch the header as blob: the sqlite driver raises on corrupt non-UTF-8
            # TEXT, and json_loads takes bytes just as well
            f"SELECT CAST(header AS BLOB) AS header FROM {AA_TABLE_ANALYSIS} "
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
                data = json_loads(row["header"])
            except ValueError, TypeError:
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
        self._require_database()
        provider = self.mass.get_provider(provider_instance_id_or_domain)
        if not isinstance(provider, MusicProvider):
            return None
        prov_key = provider.domain if provider.is_streaming_provider else provider.instance_id
        rows = await self.mass.music.database.get_rows_from_query(
            f"SELECT analysis_version FROM {AA_TABLE_ANALYSIS} "
            "WHERE item_id = :item_id AND provider = :provider "
            "AND aa_provider_domain = :aa_provider_domain AND media_type = :media_type",
            {
                "item_id": item_id,
                "provider": prov_key,
                "aa_provider_domain": aa_provider_domain,
                "media_type": media_type.value,
            },
            limit=1,
        )
        if not rows:
            return None
        return int(rows[0]["analysis_version"])

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
        self._require_database()
        return await self.mass.music.database.get_count_from_query(
            f"SELECT id FROM {AA_TABLE_ANALYSIS} "
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

        Rows carry the stored record as its ``header``/``payload`` pair; use
        :func:`_parse_row` to turn one into an AudioAnalysisData.

        :param aa_provider_domain: Domain of the AA provider whose rows to yield.
        :param media_type: The media type to filter rows by.
        """
        self._require_database()
        # fetch the header as blob: the sqlite driver raises OperationalError on corrupt
        # non-UTF-8 TEXT, which would abort the whole scan; decode() takes str or bytes
        query = (
            f"SELECT id, media_type, item_id, provider, aa_provider_domain, "
            f"CAST(header AS BLOB) AS header, payload, analysis_version, timestamp_created "
            f"FROM {AA_TABLE_ANALYSIS} "
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
        are merged purely from the database, gated on AA-provider availability
        (the virtual provider_loudness domain always passes this gate). Used by
        bulk consumers (e.g. similarity index rebuild).

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
        self._require_database()
        available_aa_domains = self._available_aa_domains()
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
            f"SELECT item_id, provider, aa_provider_domain, "
            # fetch the header as blob: the sqlite driver raises OperationalError on corrupt
            # non-UTF-8 TEXT; bytes let _parse_row skip just the bad row
            f"CAST(aa1.header AS BLOB) AS header, aa1.payload AS payload, id "
            f"FROM {AA_TABLE_ANALYSIS} aa1 "
            f"WHERE aa1.media_type = :media_type "
            f"AND EXISTS ("
            f"    SELECT 1 FROM {AA_TABLE_ANALYSIS} aa2 "
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

    @api_command("audio_analysis/coverage", required_scope=Scope.SYSTEM_MANAGE)
    async def get_coverage(self, aa_domain: str) -> AudioAnalysisCoverage:
        """
        Return analysis-coverage health counts for an AA provider.

        :param aa_domain: AA provider domain to query.
        :returns: Counts where ``pending`` reflects filesystem-source tracks only;
            streaming-provider tracks are never considered for background analysis
            and are excluded.
        """
        self._require_database()
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
            f"SELECT id FROM {AA_TABLE_ANALYSIS} "
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

    @api_command("audio_analysis/failures", required_scope=Scope.SYSTEM_MANAGE)
    async def get_failures(self, aa_domain: str | None = None) -> list[dict[str, Any]]:
        """
        Return recorded analysis failures, optionally filtered by AA provider domain.

        :param aa_domain: When given, only failures for this AA provider domain are returned.
        """
        self._require_database()
        match = {"aa_provider_domain": aa_domain} if aa_domain is not None else None
        rows = await self.mass.music.database.get_rows(AA_TABLE_FAILURES, match, limit=0)
        return [
            {
                "item_id": r["item_id"],
                "provider": r["provider"],
                "aa_provider_domain": r["aa_provider_domain"],
                "reason": r["reason"],
                "next_retry": r["next_retry"],
                "timestamp_created": r["timestamp_created"],
            }
            for r in rows
        ]

    @api_command("audio_analysis/failures/clear", required_scope=Scope.SYSTEM_MANAGE)
    async def clear_failures(
        self,
        item_id: str | None = None,
        provider: str | None = None,
        aa_domain: str | None = None,
    ) -> int:
        """
        Delete recorded failures matching the given filters; returns the number deleted.

        At least one filter is required; a call with all filters None deletes nothing.

        :param item_id: Provider-native item ID to clear.
        :param provider: Stored music-provider key (domain or instance_id) to clear.
        :param aa_domain: AA provider domain to clear.
        """
        self._require_database()
        match: dict[str, Any] = {}
        if item_id is not None:
            match["item_id"] = item_id
        if provider is not None:
            match["provider"] = provider
        if aa_domain is not None:
            match["aa_provider_domain"] = aa_domain
        if not match:
            return 0
        rows = await self.mass.music.database.get_rows(AA_TABLE_FAILURES, match, limit=0)
        count = len(rows)
        if count:
            await self.mass.music.database.delete(AA_TABLE_FAILURES, match)
        return count

    def _require_database(self) -> None:
        """Reject analysis operations until the database and its migrations are ready."""
        if not self._database_ready:
            raise ProviderUnavailableError(
                "Audio analysis database is unavailable; check the server log and restart to retry"
            )

    async def _attach_and_create(self, db_path: str) -> None:
        """
        Attach the analysis database (if not attached yet) and create its version-less tables.

        Creating the settings table is also the probe that the file is readable at all:
        ATTACH opens it lazily, so a corrupt file first fails here and the caller can move
        it aside. Check the version before :meth:`_prepare_analysis_table` creates the
        analysis table or renames a v1 table out of the way.

        :param db_path: Path of the analysis database file to attach.
        """
        db = self.mass.music.database
        attached = await db.get_rows_from_query("PRAGMA database_list", limit=0)
        if not any(row["name"] == AA_DB_SCHEMA for row in attached):
            # ATTACH cannot run inside a transaction
            await db.commit()
            await db.execute(f"ATTACH DATABASE :path AS {AA_DB_SCHEMA}", {"path": db_path})
            # the music connection holds an exclusive lock; the analysis file stays readable
            # by other processes (diagnostics, exports) and gets WAL like the other db files
            await db.execute(f"PRAGMA {AA_DB_SCHEMA}.locking_mode=NORMAL;")
            await db.execute(f"PRAGMA {AA_DB_SCHEMA}.journal_mode=WAL;")
            await db.execute(f"PRAGMA {AA_DB_SCHEMA}.journal_size_limit = 6144000;")
            await db.execute(f"PRAGMA {AA_DB_SCHEMA}.synchronous=normal;")
        await db.execute(
            f"""CREATE TABLE IF NOT EXISTS {AA_TABLE_SETTINGS}(
                    [key] TEXT PRIMARY KEY,
                    [value] TEXT,
                    [type] TEXT
                );"""
        )
        version_row = await db.get_row(AA_TABLE_SETTINGS, {"key": "version"})
        if version_row is not None and version_row["value"] is None:
            raise ProviderUnavailableError(f"{AA_DB_FILENAME} has an invalid schema version")
        if version_row is not None and int(version_row["value"]) > AA_DB_SCHEMA_VERSION:
            raise ProviderUnavailableError(
                f"{AA_DB_FILENAME} schema version {version_row['value']} is newer than "
                f"this build supports ({AA_DB_SCHEMA_VERSION}); upgrade Music Assistant"
            )
        await db.execute(
            f"""CREATE TABLE IF NOT EXISTS {AA_TABLE_FAILURES}(
                    [id] INTEGER PRIMARY KEY AUTOINCREMENT,
                    [media_type] TEXT NOT NULL,
                    [item_id] TEXT NOT NULL,
                    [provider] TEXT NOT NULL,
                    [aa_provider_domain] TEXT NOT NULL,
                    [reason] TEXT NOT NULL,
                    [analysis_version] INTEGER NOT NULL DEFAULT 1,
                    [next_retry] INTEGER,
                    [timestamp_created] INTEGER DEFAULT (cast(strftime('%s','now') as int)),
                    UNIQUE(item_id,provider,aa_provider_domain,media_type));"""
        )
        await db.commit()

    async def _prepare_analysis_table(self) -> None:
        """Rename a v1 table aside and create the packed table."""
        db = self.mass.music.database
        columns = await db.get_rows_from_query(
            f"PRAGMA {AA_DB_SCHEMA}.table_info({DB_TABLE_AUDIO_ANALYSIS})", limit=0
        )
        if any(column["name"] == "analysis_data" for column in columns):
            await db.execute(
                f"ALTER TABLE {AA_TABLE_ANALYSIS} RENAME TO {DB_TABLE_AUDIO_ANALYSIS_V1}"
            )
            await db.commit()
        await db.execute(
            f"""CREATE TABLE IF NOT EXISTS {AA_TABLE_ANALYSIS}(
                    [id] INTEGER PRIMARY KEY AUTOINCREMENT,
                    [media_type] TEXT NOT NULL,
                    [item_id] TEXT NOT NULL,
                    [provider] TEXT NOT NULL,
                    [aa_provider_domain] TEXT NOT NULL,
                    [analysis_version] INTEGER DEFAULT 1,
                    [timestamp_created] INTEGER DEFAULT (cast(strftime('%s','now') as int)),
                    [header] TEXT NOT NULL,
                    [payload] BLOB NOT NULL,
                    UNIQUE(item_id,provider,aa_provider_domain,media_type));"""
        )
        await db.commit()

    async def _migrate_json_rows(self, source_table: str) -> int:
        """
        Convert a JSON-shaped analysis table into packed rows, then drop it.

        :param source_table: Schema-qualified table with an ``analysis_data`` JSON column.
        :returns: Rows converted when the source was dropped; 0 when it did not exist or
            the conversion could not be verified (the source is kept for the next start).
        """
        db = self.mass.music.database
        schema, _, table = source_table.partition(".")
        if not await self._table_exists(schema, table):
            return 0
        total = await db.get_count_from_query(f"SELECT id FROM {source_table}")
        self.logger.info(
            "Converting %s audio analysis rows from %s to the packed format", total, source_table
        )
        converted = 0
        unreadable = 0
        last_id = 0
        try:
            while True:
                rows = await db.get_rows_from_query(
                    "SELECT id, media_type, item_id, provider, aa_provider_domain, "
                    "CAST(analysis_data AS BLOB) AS analysis_data, analysis_version, "
                    f"timestamp_created FROM {source_table} WHERE id > :last ORDER BY id",
                    {"last": last_id},
                    limit=MIGRATE_BATCH_SIZE,
                )
                if not rows:
                    break
                packed, bad = await asyncio.to_thread(_pack_rows, rows)
                unreadable += bad
                for values in packed:
                    await db.execute(
                        f"INSERT OR IGNORE INTO {AA_TABLE_ANALYSIS} (media_type, item_id, "
                        "provider, aa_provider_domain, analysis_version, timestamp_created, "
                        "header, payload) VALUES (:media_type, :item_id, :provider, "
                        ":aa_provider_domain, :analysis_version, :timestamp_created, "
                        ":header, :payload)",
                        values,
                    )
                await db.commit()
                converted += len(packed)
                last_id = int(rows[-1]["id"])
                if (converted + unreadable) % MIGRATE_PROGRESS_ROWS < len(rows):
                    self.logger.info(
                        "Converted %s/%s audio analysis rows from %s",
                        converted + unreadable,
                        total,
                        source_table,
                    )
            # verify by natural key, not row count: a live write can consume the packed
            # table's own AUTOINCREMENT sequence, so its count alone proves nothing
            missing = await db.get_count_from_query(
                f"SELECT s.id FROM {source_table} s WHERE NOT EXISTS ("
                f"SELECT 1 FROM {AA_TABLE_ANALYSIS} a WHERE a.item_id = s.item_id "
                "AND a.provider = s.provider AND a.aa_provider_domain = s.aa_provider_domain "
                "AND a.media_type = s.media_type)"
            )
            if missing > unreadable:
                self.logger.error(
                    "Conversion of %s incomplete (%s rows missing, %s unreadable); keeping it "
                    "and retrying on next start",
                    source_table,
                    missing,
                    unreadable,
                )
                return 0
            await db.execute(f"DROP TABLE {source_table}")
            await db.commit()
        except sqlite3.Error as err:
            self.logger.error(
                "Conversion of %s failed after %s rows: %s: %s; keeping it and retrying on "
                "next start",
                source_table,
                converted,
                type(err).__name__,
                err,
            )
            return 0
        if unreadable:
            self.logger.warning(
                "%s unreadable audio analysis rows in %s were dropped", unreadable, source_table
            )
        self.logger.info(
            "Converted %s rows from %s into the packed format", converted, source_table
        )
        return converted

    async def _compact_analysis_database(self, converted: int) -> None:
        """
        Reclaim the space the converted JSON rows held in the analysis file.

        :param converted: Number of rows converted, for the log line.
        """
        self.logger.info(
            "Compacting %s after converting %s rows to the packed format",
            AA_DB_FILENAME,
            converted,
        )
        try:
            await self.mass.music.database.vacuum(schema=AA_DB_SCHEMA)
        except sqlite3.Error as err:
            self.logger.warning("Compacting %s failed: %s", AA_DB_FILENAME, err)

    async def _table_exists(self, schema: str, table: str) -> bool:
        """
        Return whether a table exists in the given (attached) schema.

        :param schema: Schema name, e.g. ``main`` or ``aa``.
        :param table: Unqualified table name.
        """
        rows = await self.mass.music.database.get_rows_from_query(
            f"SELECT 1 FROM {schema}.sqlite_master WHERE type = 'table' AND name = :name",
            {"name": table},
            limit=1,
        )
        return bool(rows)

    async def _quarantine_database(self, db_path: str) -> None:
        """
        Detach an unusable analysis database and move it (and its sidecars) out of the way.

        :param db_path: Path of the analysis database file to move aside.
        """
        db = self.mass.music.database
        attached = await db.get_rows_from_query("PRAGMA database_list", limit=0)
        if any(row["name"] == AA_DB_SCHEMA for row in attached):
            await db.commit()
            await db.execute(f"DETACH DATABASE {AA_DB_SCHEMA}")
        for suffix in ("", "-wal", "-shm"):
            source = f"{db_path}{suffix}"
            if await asyncio.to_thread(os.path.exists, source):
                # overwrites an older quarantine; we only ever keep the most recent one
                await asyncio.to_thread(os.replace, source, f"{source}.corrupt")

    async def _relocate_legacy_failures(self) -> int | None:
        """
        Copy the legacy main.audio_analysis_failures into the attached db, then drop it.

        Rows are copied without their legacy id: the attached db assigns fresh ids via its
        own AUTOINCREMENT. Conflicts on the natural (item_id, provider, aa_provider_domain,
        media_type) key keep the row with the newer timestamp, preserving newer legacy writes
        after a rollback as well as newer destination writes on a retry. Equal timestamps
        keep the destination row. Completion is verified by natural key before the legacy
        table is dropped, not by comparing row counts.

        :returns: Number of rows in the dropped source (0 if absent), or None on failure.
        """
        db = self.mass.music.database
        table = DB_TABLE_AUDIO_ANALYSIS_FAILURES
        if not await self._table_exists("main", table):
            return 0
        total = await db.get_count_from_query(f"SELECT id FROM main.{table}")
        max_id = 0
        if total:
            row = await db.get_rows_from_query(
                f"SELECT MAX(id) AS max_id FROM main.{table}", limit=1
            )
            max_id = int(row[0]["max_id"])
        self.logger.info(
            "Moving %s rows from library.db table %s to %s", total, table, AA_DB_FILENAME
        )
        cols = ", ".join(_FAILURE_COLUMNS)
        updates = ", ".join(f"{column} = excluded.{column}" for column in _FAILURE_COLUMNS)
        copied = 0
        last_id = 0
        try:
            while last_id < max_id:
                cursor = await db.execute(
                    f"INSERT INTO {AA_DB_SCHEMA}.{table} ({cols}) "
                    f"SELECT {cols} FROM main.{table} "
                    f"WHERE id > :last_id AND id <= :upper ORDER BY id "
                    "ON CONFLICT(item_id, provider, aa_provider_domain, media_type) "
                    f"DO UPDATE SET {updates} "
                    f"WHERE excluded.timestamp_created > {table}.timestamp_created",
                    {"last_id": last_id, "upper": last_id + RELOCATE_BATCH_SIZE},
                )
                await db.commit()
                copied += cursor.rowcount
                last_id += RELOCATE_BATCH_SIZE
                self.logger.debug("Moved %s/%s rows of %s", min(copied, total), total, table)
            # verify by natural key, not row count: a live write can consume aa's own
            # AUTOINCREMENT sequence, so aa's count alone can't prove every legacy row landed
            missing = await db.get_count_from_query(
                f"SELECT m.id FROM main.{table} m WHERE NOT EXISTS ("
                f"SELECT 1 FROM {AA_DB_SCHEMA}.{table} a "
                f"WHERE a.item_id = m.item_id AND a.provider = m.provider "
                f"AND a.aa_provider_domain = m.aa_provider_domain AND a.media_type = m.media_type)"
            )
            if missing:
                self.logger.error(
                    "Relocation of %s incomplete (%s of %s rows still unmigrated in %s); "
                    "keeping the library.db copy and retrying on next start",
                    table,
                    missing,
                    total,
                    AA_DB_FILENAME,
                )
                return None
            await db.execute(f"DROP TABLE main.{table}")
            await db.commit()
        except sqlite3.Error as err:
            self.logger.error(
                "Relocation of %s failed after %s rows: %s; keeping the library.db copy "
                "and retrying on next start",
                table,
                copied,
                err,
            )
            return None
        self.logger.info("Moved %s of %s rows of %s into %s", copied, total, table, AA_DB_FILENAME)
        return total

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
                    # Provider method with an open-ended failure surface; any failure
                    # just skips this scan candidate.
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
    ) -> None:
        """
        Run a single track through the streaming pipeline using ffmpeg as the source.

        :param streamdetails: Stream details for the track being analyzed.
        :param providers: Audio analysis providers to dispatch chunks to.
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
                self._run_background_streaming_inner(session_key, streamdetails, providers),
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
    ) -> None:
        """
        Inner body of _run_background_streaming_for_track, wrapped by wait_for.

        :param session_key: Active-session key for this track.
        :param streamdetails: Stream details for the track being analyzed.
        :param providers: Audio analysis providers to dispatch chunks to.
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
        # aclosing guarantees the source (and any provider stream slot it holds)
        # is released promptly when the loop breaks out early
        async with contextlib.aclosing(audio_source):
            async for chunk in audio_source:
                if session_key not in self._active_sessions:
                    # all providers evicted — bail early
                    break
                now = time.monotonic()
                if now < next_allowed:
                    await asyncio.sleep(next_allowed - now)
                await self._distribute_chunk(
                    session_key, chunk, max_interval=CHUNK_HANG_GUARD_SECONDS
                )
                next_allowed = time.monotonic() + BACKGROUND_PACE_INTERVAL_SECONDS_FLOOR
        if session_key in self._active_sessions:
            self._finalize_providers(session_key)

    def _available_filesystem_domains(self) -> tuple[str, ...]:
        """Return configured filesystem provider domains that are currently available."""
        return tuple(
            domain
            for domain in FILESYSTEM_PROVIDER_DOMAINS
            if any(p.domain == domain and p.available for p in self.mass.providers)
        )

    async def _find_candidates_missing_analysis(
        self,
        aa_provider_versions: Mapping[str, int],
        limit: int,
    ) -> list[dict[str, Any]]:
        """
        Return tracks that need (re)analysis for one or more AA providers.

        A track is a candidate for a given AA provider domain when it has no analysis row for
        that domain, when its stored row predates the provider's current analysis_version (a
        NULL stored version, from pre-versioning rows, is also treated as stale), and when no
        blocking failure row exists (a failure at the current-or-newer analysis_version whose
        retry is NULL or still in the future). The version check mirrors the per-track gate in
        AudioAnalysisProvider.start_analysis so a provider bumping its analysis_version triggers
        a background re-scan.

        :param aa_provider_versions: Mapping of AA provider domain to the provider's current
            analysis_version.
        :param limit: Maximum number of candidate rows to return (0 for no limit).
        :returns: Rows {item_id, provider_instance, missing_domains} where missing_domains
            lists the AA provider domains needing analysis.
        """
        if not aa_provider_versions:
            return []

        filesystem_domains = self._available_filesystem_domains()
        if not filesystem_domains:
            return []

        # CROSS JOIN (track x possible domain), keep pairs with no up-to-date analysis row and
        # no blocking failure row, then GROUP_CONCAT the missing domains per track. An analysis
        # row counts as up-to-date only when its analysis_version is non-NULL and >= the
        # provider's current version, so missing and stale-version rows both surface.
        aa_domains = list(aa_provider_versions)
        fs_inline = ", ".join(f"'{d}'" for d in filesystem_domains)
        aa_select_terms = " UNION ALL ".join(
            f"SELECT :aa_{i} AS aa_provider_domain, :ver_{i} AS current_version"
            for i in range(len(aa_domains))
        )
        params: dict[str, Any] = {
            "media_type": MediaType.TRACK.value,
            "now": int(utc_timestamp()),
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
            f"    SELECT 1 FROM {AA_TABLE_ANALYSIS} an "
            f"    WHERE an.item_id = pm.provider_item_id "
            f"      AND an.provider = pm.provider_instance "
            f"      AND an.aa_provider_domain = possible.aa_provider_domain "
            f"      AND an.media_type = :media_type "
            f"      AND an.analysis_version IS NOT NULL "
            f"      AND an.analysis_version >= possible.current_version"
            f"  ) "
            f"  AND NOT EXISTS ("
            f"    SELECT 1 FROM {AA_TABLE_FAILURES} f "
            f"    WHERE f.item_id = pm.provider_item_id "
            f"      AND f.provider = pm.provider_instance "
            f"      AND f.aa_provider_domain = possible.aa_provider_domain "
            f"      AND f.media_type = :media_type "
            f"      AND f.analysis_version >= possible.current_version "
            f"      AND (f.next_retry IS NULL OR f.next_retry > :now)"
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
        """Count filesystem candidate tracks lacking a current analysis row or blocking failure."""
        filesystem_domains = self._available_filesystem_domains()
        if not filesystem_domains:
            return 0
        fs_inline = ", ".join(f"'{d}'" for d in filesystem_domains)
        query = (
            f"SELECT pm.provider_item_id FROM {DB_TABLE_PROVIDER_MAPPINGS} pm "
            f"WHERE pm.media_type = :media_type "
            f"  AND pm.provider_domain IN ({fs_inline}) "
            f"  AND NOT EXISTS ("
            f"    SELECT 1 FROM {AA_TABLE_ANALYSIS} an "
            f"    WHERE an.item_id = pm.provider_item_id "
            f"      AND an.provider = pm.provider_instance "
            f"      AND an.aa_provider_domain = :aa_domain "
            f"      AND an.media_type = :media_type "
            f"      AND an.analysis_version IS NOT NULL "
            f"      AND an.analysis_version >= :current_version"
            f"  ) "
            f"  AND NOT EXISTS ("
            f"    SELECT 1 FROM {AA_TABLE_FAILURES} f "
            f"    WHERE f.item_id = pm.provider_item_id "
            f"      AND f.provider = pm.provider_instance "
            f"      AND f.aa_provider_domain = :aa_domain "
            f"      AND f.media_type = :media_type "
            f"      AND f.analysis_version >= :current_version "
            f"      AND (f.next_retry IS NULL OR f.next_retry > :now)"
            f"  )"
        )
        return await self.mass.music.database.get_count_from_query(
            query,
            {
                "media_type": MediaType.TRACK.value,
                "aa_domain": aa_domain,
                "current_version": current_version,
                "now": int(utc_timestamp()),
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
        self._mark_analysis_activity()
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
                # provider.start_analysis is provider-implemented; skip the one that
                # fails to start and keep the rest of the session going.
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
                # finalize runs the whole-track inference, long after the session was popped
                # above, so track it: it is what keeps the models in use from here on.
                task = self.mass.create_task(provider.finalize(session_key))
                self._finalize_tasks.add(task)
                task.add_done_callback(self._finalize_tasks.discard)

    def _cancel_providers(self, session_key: str) -> None:
        """Cancel each provider in the session."""
        provider_ids = self._active_sessions.pop(session_key, None)
        if not provider_ids:
            return
        for provider_id in provider_ids:
            provider = self.mass.get_provider(provider_id)
            if provider and isinstance(provider, AudioAnalysisProvider) and provider.available:
                self.mass.create_task(provider.cancel(session_key))

    def _evict_realtime_session(self, session_key: str) -> None:
        """Stop a realtime analysis worker and cancel its providers, freeing the session slot."""
        self._session_queues.pop(session_key, None)
        worker = self._workers.pop(session_key, None)
        if worker is not None and not worker.done():
            worker.cancel()
        # Cancel providers directly: a task cancelled before it first runs has no finally to run.
        self._cancel_providers(session_key)
        self.logger.debug("Stopped realtime analysis session %s", session_key)

    def _mark_analysis_activity(self) -> None:
        """Record analysis activity and ensure the idle-model monitor is running."""
        self._last_analysis_activity = time.monotonic()
        if self._idle_unload_task is None or self._idle_unload_task.done():
            self._idle_unload_task = self.mass.create_task(self._monitor_idle_models())

    async def _monitor_idle_models(self) -> None:
        """Unload heavy models once no analysis has run for MODEL_IDLE_UNLOAD_SECONDS."""
        while True:
            await asyncio.sleep(MODEL_IDLE_CHECK_INTERVAL_SECONDS)
            if self._active_sessions or self._finalize_tasks:
                # Keep the timer fresh while analysis is running.
                self._last_analysis_activity = time.monotonic()
                continue
            if time.monotonic() - self._last_analysis_activity < MODEL_IDLE_UNLOAD_SECONDS:
                continue
            await self._unload_idle_models()
            return  # stop until the next analysis restarts the monitor

    async def _unload_idle_models(self) -> None:
        """Free heavy models on every provider that supports unloading them."""
        for provider in self.providers:
            if not provider.has_unloadable_models:
                continue
            try:
                await provider.unload_idle_models()
            except Exception as err:
                self.logger.warning("Failed to unload models for %s: %s", provider.name, err)

    async def _distribute_chunk(
        self,
        session_key: str,
        pcm_data: bytes,
        max_interval: float = CHUNK_HANG_GUARD_SECONDS,
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

        async def _process(prov_id: str) -> tuple[str, tuple[str, datetime | None] | None] | None:
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
                sem = self.analysis_semaphore
                contention = (
                    f"{sem.in_flight}/{sem.capacity} permits in use, {sem.waiters} queued"
                    if isinstance(sem, InstrumentedSemaphore)
                    else "concurrency gauge unavailable"
                )
                self.logger.warning(
                    "Provider %s timed out after %.1fs processing chunk for %s "
                    "(%s, %d active sessions), removing from session",
                    prov_id,
                    max_interval,
                    session_key,
                    contention,
                    len(self._active_sessions),
                )
                # a timeout tracks server load rather than the audio, so the track stays
                # pending for the next run instead of being recorded against it
                return prov_id, None
            except AudioAnalysisError as err:
                # the provider judged this track unanalyzable, so its own wording is what
                # the user should see in the failures overview
                self.logger.warning(
                    "Provider %s failed analysis for %s: %s", prov_id, session_key, err.reason
                )
                return prov_id, (err.reason, err.retry_at)
            except Exception as err:
                # process_pcm_chunk is provider-implemented (torch/numpy/ffmpeg); evict
                # the provider that fails on a chunk rather than crashing the session.
                self.logger.warning("Error processing PCM chunk on provider %s: %s", prov_id, err)
                return prov_id, (
                    f"audio processing failed ({str(err) or type(err).__name__})",
                    None,
                )
            return None

        results = await asyncio.gather(*[_process(prov_id) for prov_id in provider_ids])
        evicted = dict(result for result in results if result is not None)
        if evicted:
            for prov_id, failure in evicted.items():
                provider = self.mass.get_provider(prov_id)
                if provider and isinstance(provider, AudioAnalysisProvider) and provider.available:
                    if failure is None:
                        self.mass.create_task(provider.cancel(session_key))
                    else:
                        reason, retry_at = failure
                        await provider.abort(session_key, reason, retry_at)
            provider_ids.difference_update(evicted)
            if not provider_ids:
                self._active_sessions.pop(session_key, None)

    async def _buffer_reader_worker(
        self,
        session_key: str,
        audio_buffer: AudioBuffer,
        expected_duration: float | None,
    ) -> None:
        """
        Read PCM straight from the shared playback buffer and distribute it to providers.

        Reads at its own pace from the buffer's retained window. On clean end-of-stream the
        providers are finalized, unless the source ended far short of the expected duration.
        If the reader falls a full window behind playback (the chunk it needs has been
        evicted) or the buffer is torn down first, the session is dropped.

        :param session_key: Active-session key for this worker.
        :param audio_buffer: The shared playback buffer to read PCM from.
        :param expected_duration: Expected track duration in seconds (None when unknown,
            e.g. radio), used to discard sessions of streams that ended prematurely.
        """
        start_chunk = audio_buffer.first_buffered_chunk
        cursor = start_chunk
        completed = False
        try:
            while session_key in self._active_sessions:
                try:
                    chunk = await audio_buffer.read_chunk_for_analysis(cursor)
                except AudioBufferEOF:
                    completed = True
                    break
                except AudioBufferDiscarded:
                    self.logger.debug(
                        "Analysis fell behind the playback buffer for %s (chunk %d evicted); "
                        "dropping session",
                        session_key,
                        cursor,
                    )
                    break
                except Exception as err:
                    self.logger.debug("Analysis read failed for %s: %s", session_key, err)
                    break
                await self._distribute_chunk(
                    session_key, chunk, max_interval=CHUNK_HANG_GUARD_SECONDS
                )
                cursor += 1
        finally:
            self._workers.pop(session_key, None)
            self._session_queues.pop(session_key, None)
            # one chunk equals one second of audio
            received_seconds = cursor - start_chunk
            if (
                completed
                and expected_duration
                and received_seconds < expected_duration * ANALYSIS_MIN_COMPLETENESS_RATIO
            ):
                self.logger.debug(
                    "Analysis received only %ds of the expected %ds for %s; "
                    "discarding incomplete session",
                    received_seconds,
                    expected_duration,
                    session_key,
                )
                completed = False
            if completed:
                self._finalize_providers(session_key)
            else:
                self._cancel_providers(session_key)

    def _cpu_count(self) -> int:
        """Return the CPU core count available to this process (fallback 4 when unknown)."""
        return os.process_cpu_count() or os.cpu_count() or 4

    def _aa_thread_budget(self) -> int:
        """Return the per-op PyTorch intra-op thread budget for inference (~25% of cpu_count)."""
        # Shared with the native BLAS cap applied at process start, so torch and BLAS agree.
        return inference_thread_budget()

    def _get_scan_concurrency(self) -> int:
        """Read background scan concurrency from config, clamped to [1, 16]."""
        try:
            value = int(
                self.mass.config.get_raw_core_config_value(
                    "streams",
                    CONF_BACKGROUND_SCAN_CONCURRENCY,
                    DEFAULT_BACKGROUND_SCAN_CONCURRENCY,
                )
                or DEFAULT_BACKGROUND_SCAN_CONCURRENCY
            )
        except ValueError, TypeError:
            value = DEFAULT_BACKGROUND_SCAN_CONCURRENCY
        return max(1, min(value, 16))

    def _available_aa_domains(self) -> set[str]:
        """Return available AA provider domains plus the virtual provider-loudness domain."""
        return {
            p.domain for p in self.mass.get_providers(ProviderType.AUDIO_ANALYSIS) if p.available
        } | {PROVIDER_LOUDNESS_DOMAIN}
