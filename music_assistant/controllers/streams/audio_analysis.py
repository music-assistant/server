"""Controller for distributing audio analysis to providers."""

from __future__ import annotations

import asyncio
import contextlib
import os
from math import inf
from typing import TYPE_CHECKING

import torch
from music_assistant_models.background_task import TaskSchedule
from music_assistant_models.enums import MediaType, ProviderType, StreamType

from music_assistant.constants import (
    DB_TABLE_AUDIO_ANALYSIS,
    DB_TABLE_PROVIDER_MAPPINGS,
    LOUDNESS_MEASUREMENT_MIN_LUFS,
)
from music_assistant.helpers.datetime import local_clock_time_to_utc
from music_assistant.helpers.json import json_dumps, json_loads
from music_assistant.models.audio_analysis import AudioAnalysisData
from music_assistant.models.audio_analysis_provider import AudioAnalysisProvider
from music_assistant.models.music_provider import MusicProvider

CHUNK_PROCESS_TIMEOUT = 1.0
LOUDNESS_ANALYSIS_DOMAIN = "loudness_analysis"
BACKGROUND_SCAN_TASK_ID = "audio_analysis_background_scan"
BACKGROUND_SCAN_BATCH_SIZE = 250
BACKGROUND_SCAN_SLEEP_BETWEEN_ITEMS = 2.0
# providers whose tracks can be analyzed from their local filesystem path
FILESYSTEM_PROVIDER_DOMAINS: tuple[str, ...] = (
    "filesystem_local",
    "filesystem_smb",
    "filesystem_nfs",
)

if TYPE_CHECKING:
    from music_assistant_models.media_items import AudioFormat
    from music_assistant_models.streamdetails import StreamDetails

    from music_assistant.controllers.streams.audio_buffer import AudioBuffer
    from music_assistant.controllers.streams.controller import StreamsController


class AudioAnalysisController:
    """Controller that distributes PCM chunks to all registered AudioAnalysisProviders."""

    def __init__(self, streams: StreamsController) -> None:
        """Initialize the AudioAnalysisController.

        :param streams: Parent StreamsController instance.
        """
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
        )

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

        Starts an analysis session for the given track on all available
        Audio Analysis providers.

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
        queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=10)
        self._workers[session_key] = self.mass.create_task(self._chunk_worker(session_key, queue))

        # Build and register closures for callbacks on the audio buffer
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
                await asyncio.wait_for(queue.put(pcm_data), timeout=CHUNK_PROCESS_TIMEOUT)
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
        if not (
            provider := self.mass.get_provider(
                provider_instance_id_or_domain, provider_type=MusicProvider
            )
        ):
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
    ) -> AudioAnalysisData | None:
        """
        Get merged audio analysis data from all enabled AA providers for a track.

        Only rows from currently available AA providers are included.
        Multiple providers' results are merged using latest-write-wins.

        :param item_id: Provider-native item ID from streamdetails.item_id.
        :param provider_instance_id_or_domain: Music provider instance ID or domain.
        :param media_type: The media type of the item.
        """
        if not (
            provider := self.mass.get_provider(
                provider_instance_id_or_domain, provider_type=MusicProvider
            )
        ):
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

        merged = AudioAnalysisData()
        found = False
        for row in rows:
            if row["aa_provider_domain"] not in available_aa_domains:
                continue
            row_data = AudioAnalysisData.from_dict(json_loads(row["analysis_data"]))
            merged.update(row_data)
            found = True
        return merged if found else None

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
        if not (
            provider := self.mass.get_provider(
                provider_instance_id_or_domain, provider_type=MusicProvider
            )
        ):
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

    async def _run_background_scan(self) -> None:
        """
        Run the nightly background scan across all audio analysis providers.

        Iterates each available provider, queries tracks from local-filesystem
        music providers that do not yet have analysis for that provider, and
        hands each one to the provider's `analyze_file` hook. Results are
        persisted via `set_audio_analysis`. The batch aborts for a given
        provider if its storage backend goes offline.
        """
        providers = self.providers
        if not providers:
            return

        for provider in providers:
            candidates = await self._find_tracks_missing_analysis(
                provider.domain, BACKGROUND_SCAN_BATCH_SIZE
            )
            if not candidates:
                continue

            self.logger.info(
                "Background %s analysis: %d track(s) pending",
                provider.domain,
                len(candidates),
            )
            processed = 0
            for row in candidates:
                if not provider.available:
                    # provider was disabled mid-run
                    break
                item_id = str(row["item_id"])
                provider_instance = str(row["provider_instance"])
                music_prov = self.mass.get_provider(provider_instance, provider_type=MusicProvider)
                if music_prov is None or not music_prov.available:
                    # storage may be offline right now (e.g. NAS asleep) — stop the
                    # batch rather than churning through failures for the remaining
                    # tracks
                    self.logger.debug(
                        "Background %s analysis: provider %s unavailable, aborting batch",
                        provider.domain,
                        provider_instance,
                    )
                    break

                try:
                    streamdetails = await music_prov.get_stream_details(item_id, MediaType.TRACK)
                except Exception as err:
                    self.logger.debug(
                        "Background %s analysis: skipping %s (stream details failed: %s)",
                        provider.domain,
                        item_id,
                        err,
                    )
                    continue

                if streamdetails.stream_type != StreamType.LOCAL_FILE:
                    continue
                if not isinstance(streamdetails.path, str) or not streamdetails.path:
                    continue

                try:
                    result = await provider.analyze_file(streamdetails)
                except Exception as err:
                    self.logger.warning(
                        "Background %s analysis failed for %s: %s",
                        provider.domain,
                        item_id,
                        err,
                    )
                    result = None

                if result is not None:
                    await self.set_audio_analysis(
                        item_id=item_id,
                        provider_instance_id_or_domain=music_prov.instance_id,
                        aa_provider_domain=provider.domain,
                        analysis=result,
                        analysis_version=provider.analysis_version,
                    )
                    processed += 1

                await asyncio.sleep(BACKGROUND_SCAN_SLEEP_BETWEEN_ITEMS)

            self.logger.info(
                "Background %s analysis: analyzed %d/%d track(s)",
                provider.domain,
                processed,
                len(candidates),
            )

    async def _find_tracks_missing_analysis(
        self, aa_provider_domain: str, limit: int
    ) -> list[dict[str, object]]:
        """Return up to N local-filesystem tracks without analysis for the given AA provider."""
        filesystem_domains = tuple(
            domain
            for domain in FILESYSTEM_PROVIDER_DOMAINS
            if any(
                p.domain == domain and p.available
                for p in self.mass.get_providers(ProviderType.MUSIC)
            )
        )
        if not filesystem_domains:
            return []

        domains_sql = ", ".join(f"'{d}'" for d in filesystem_domains)
        track_media_type = MediaType.TRACK.value
        # audio_analysis.item_id holds the provider-native item id,
        # so join against provider_mappings.provider_item_id (not pm.item_id,
        # which is the integer library-row id)
        query = (
            f"SELECT pm.provider_item_id AS item_id, "
            f"       pm.provider_instance AS provider_instance "
            f"FROM {DB_TABLE_PROVIDER_MAPPINGS} pm "
            f"LEFT JOIN {DB_TABLE_AUDIO_ANALYSIS} aa "
            f"  ON aa.item_id = pm.provider_item_id "
            f"  AND aa.provider = pm.provider_instance "
            f"  AND aa.aa_provider_domain = '{aa_provider_domain}' "
            f"  AND aa.media_type = '{track_media_type}' "
            f"WHERE pm.media_type = '{track_media_type}' "
            f"  AND pm.provider_domain IN ({domains_sql}) "
            f"  AND aa.id IS NULL"
        )
        rows = await self.mass.music.database.get_rows_from_query(query, limit=limit)
        return [dict(r) for r in rows]

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

    async def _chunk_worker(self, session_key: str, queue: asyncio.Queue[bytes | None]) -> None:
        """Background worker that processes queued PCM chunks concurrently across providers."""
        while True:
            chunk = await queue.get()
            if chunk is None:
                break

            provider_ids = self._active_sessions.get(session_key)
            if not provider_ids:
                break

            pcm_data = chunk  # bind for closure (chunk is narrowed to bytes here)

            async def _process(prov_id: str, pcm_data: bytes = pcm_data) -> str | None:
                try:
                    provider = self.mass.get_provider(prov_id)
                    if not (
                        provider
                        and isinstance(provider, AudioAnalysisProvider)
                        and provider.available
                    ):
                        return None
                    await asyncio.wait_for(
                        provider.process_pcm_chunk(session_key, pcm_data),
                        timeout=CHUNK_PROCESS_TIMEOUT,
                    )
                except TimeoutError:
                    self.logger.warning(
                        "Provider %s timed out processing chunk for %s, removing from session",
                        prov_id,
                        session_key,
                    )
                    return prov_id
                except Exception as err:
                    self.logger.warning(
                        "Error processing PCM chunk on provider %s: %s", prov_id, err
                    )
                return None

            results = await asyncio.gather(*[_process(prov_id) for prov_id in provider_ids])
            timed_out = {prov_id for prov_id in results if prov_id is not None}
            if timed_out:
                for prov_id in timed_out:
                    provider = self.mass.get_provider(prov_id)
                    if (
                        provider
                        and isinstance(provider, AudioAnalysisProvider)
                        and provider.available
                    ):
                        self.mass.create_task(provider.cancel(session_key))
                provider_ids -= timed_out
                if not provider_ids:
                    self._active_sessions.pop(session_key, None)
                    self._workers.pop(session_key, None)
                    break

    def _aa_thread_budget(self) -> int:
        """Return the per-op PyTorch intra-op thread budget for inference (~25% of cpu_count)."""
        return max(1, (os.process_cpu_count() or os.cpu_count() or 4) // 4)
