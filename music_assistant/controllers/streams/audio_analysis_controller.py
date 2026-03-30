"""Controller for distributing audio analysis to providers."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING

from music_assistant_models.enums import ProviderType

from music_assistant.constants import MASS_LOGGER_NAME
from music_assistant.models.audio_analysis_provider import AudioAnalysisProvider

if TYPE_CHECKING:
    from music_assistant_models.media_items import AudioFormat
    from music_assistant_models.streamdetails import StreamDetails

    from music_assistant.controllers.streams.audio_buffer import AudioBuffer
    from music_assistant.controllers.streams.controller import StreamsController


class AudioAnalysisController:
    """Controller that distributes PCM chunks to all registered AudioAnalysisProviders.

    This is a child controller owned by StreamsController. It coordinates analysis
    across multiple providers, each of which can process audio independently.

    Lifecycle is managed via closures registered on the AudioBuffer's chunk and
    cancel callbacks. The controller does not expose process_pcm_chunk, finalize,
    or cancel methods — those concerns live inside the closures built by start_analysis.
    """

    def __init__(self, streams: StreamsController) -> None:
        """Initialize the AudioAnalysisController.

        :param streams: Parent StreamsController instance.
        """
        self.streams = streams
        self.mass = streams.mass
        self.logger = logging.getLogger(MASS_LOGGER_NAME).getChild("audio_analysis")
        self._active_sessions: dict[str, set[str]] = {}
        self._queues: dict[str, asyncio.Queue[bytes | None]] = {}
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
        stream_details: StreamDetails,
    ) -> None:
        """Start analysis session for a track across all providers.

        Builds closures that capture session state and registers them on the
        audio buffer's chunk and cancel callbacks. The closures manage the full
        lifecycle: feeding chunks to a background worker, finalizing providers
        on EOF, and cancelling on buffer clear.

        :param audio_buffer: The AudioBuffer to observe for PCM chunks.
        :param stream_details: The stream details for the item being analyzed.
        """
        providers = self.providers
        if not providers:
            self.logger.debug("No audio analysis providers available")
            return

        session_key = (
            f"{stream_details.provider}:{stream_details.media_type}:{stream_details.item_id}"
        )

        # Skip if another queue already has an analysis running for the same item
        if session_key in self._active_sessions:
            self.logger.debug(
                "Analysis session already active for %s, ignoring start request",
                stream_details.uri,
            )
            return

        provider_ids = await self._start_analysis_on_providers(
            session_key, stream_details, audio_buffer.pcm_format, providers
        )
        if not provider_ids:
            self.logger.debug("No providers accepted analysis for %s", stream_details.uri)
            return

        self._active_sessions[session_key] = provider_ids
        queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._queues[session_key] = queue
        self._workers[session_key] = self.mass.create_task(self._chunk_worker(session_key, queue))

        # Build and register closures for callbacks on the audio buffer
        finalized = False

        def _on_chunk(position_seconds: int, pcm_data: bytes, is_last_chunk: bool) -> None:  # noqa: ARG001
            nonlocal finalized
            if finalized:
                return
            if is_last_chunk:
                finalized = True
                queue.put_nowait(None)
                self.mass.create_task(_finalize_session())
                return
            queue.put_nowait(pcm_data)

        async def _finalize_session() -> None:
            """Await the worker, then dispatch finalize to each provider."""
            worker = self._workers.pop(session_key, None)
            if worker is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await worker
            self._queues.pop(session_key, None)
            self._finalize_providers(session_key)

        def _on_cancel() -> None:
            self.logger.debug("Cancelling analysis session %s", session_key)
            self._queues.pop(session_key, None)
            worker = self._workers.pop(session_key, None)
            if worker is not None:
                worker.cancel()
            self._cancel_providers(session_key)

        audio_buffer.register_chunk_callback(_on_chunk)
        audio_buffer.register_cancel_callback(_on_cancel)

    async def _start_analysis_on_providers(
        self,
        session_key: str,
        stream_details: StreamDetails,
        audio_format: AudioFormat,
        providers: list[AudioAnalysisProvider],
    ) -> set[str]:
        """Call start_analysis on each provider, returning IDs of those that accepted.

        :param session_key: The session key for this analysis.
        :param stream_details: The stream details for the item being analyzed.
        :param audio_format: PCM format of the audio stream.
        :param providers: List of available analysis providers.
        """
        provider_ids: set[str] = set()
        for provider in providers:
            stored_version = await self.mass.music.get_audio_analysis_version(
                stream_details.item_id,
                stream_details.provider,
                provider.domain,
            )
            if stored_version is not None and stored_version >= provider.analysis_version:
                self.logger.debug(
                    "Analysis already exists for provider %s (version %d >= %d), skipping",
                    provider.name,
                    stored_version,
                    provider.analysis_version,
                )
                continue

            try:
                await provider.start_analysis(
                    session_id=session_key,
                    stream_details=stream_details,
                    audio_format=audio_format,
                )
            except Exception as err:
                self.logger.warning(
                    "Failed to start analysis on provider %s: %s",
                    provider.name,
                    err,
                )
            else:
                provider_ids.add(provider.instance_id)
        return provider_ids

    def _finalize_providers(self, session_key: str) -> None:
        """Fire-and-forget finalize to each provider in the session."""
        provider_ids = self._active_sessions.pop(session_key, None)
        if not provider_ids:
            return
        for provider_id in provider_ids:
            provider = self.mass.get_provider(provider_id)
            if provider and isinstance(provider, AudioAnalysisProvider) and provider.available:
                self.mass.create_task(provider.finalize(session_key))

    def _cancel_providers(self, session_key: str) -> None:
        """Fire-and-forget cancel to each provider in the session."""
        provider_ids = self._active_sessions.pop(session_key, None)
        if not provider_ids:
            return
        for provider_id in provider_ids:
            provider = self.mass.get_provider(provider_id)
            if provider and isinstance(provider, AudioAnalysisProvider) and provider.available:
                self.mass.create_task(provider.cancel(session_key))

    async def _chunk_worker(self, session_key: str, queue: asyncio.Queue[bytes | None]) -> None:
        """Background worker that processes queued PCM chunks sequentially."""
        while True:
            chunk = await queue.get()
            if chunk is None:
                break

            provider_ids = self._active_sessions.get(session_key)
            if not provider_ids:
                break

            for provider_id in provider_ids:
                provider = self.mass.get_provider(provider_id)
                if not (
                    provider and isinstance(provider, AudioAnalysisProvider) and provider.available
                ):
                    continue
                try:
                    await provider.process_pcm_chunk(session_key, chunk)
                except Exception as err:
                    self.logger.warning(
                        "Error processing PCM chunk on provider %s: %s",
                        provider_id,
                        err,
                    )
