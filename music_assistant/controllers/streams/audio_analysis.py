"""Controller for distributing audio analysis to providers."""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

from music_assistant_models.enums import MediaType, ProviderType

from music_assistant.constants import DB_TABLE_AUDIO_ANALYSIS
from music_assistant.helpers.json import json_dumps, json_loads
from music_assistant.models.audio_analysis import AudioAnalysisData
from music_assistant.models.audio_analysis_provider import AudioAnalysisProvider
from music_assistant.models.music_provider import MusicProvider

CHUNK_PROCESS_TIMEOUT = 0.8

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
            await queue.put(pcm_data)

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
