"""Controller for distributing audio analysis to providers."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from music_assistant_models.enums import ProviderType

from music_assistant.constants import MASS_LOGGER_NAME
from music_assistant.models.audio_analysis_provider import AudioAnalysisProvider

if TYPE_CHECKING:
    from music_assistant_models.media_items import AudioFormat
    from music_assistant_models.streamdetails import StreamDetails

    from music_assistant.controllers.streams.streams_controller import StreamsController


class AudioAnalysisController:
    """Controller that distributes PCM chunks to all registered AudioAnalysisProviders.

    This is a child controller owned by StreamsController. It coordinates analysis
    across multiple providers, each of which can process audio independently.
    Providers are responsible for storing their own results.

    Each session gets an asyncio.Queue with a background worker that processes
    chunks sequentially, keeping the streaming pipeline non-blocking while
    preserving chunk ordering for stateful providers.
    """

    def __init__(self, streams: StreamsController) -> None:
        """Initialize the AudioAnalysisController.

        :param streams: Parent StreamsController instance.
        """
        self.streams = streams
        self.mass = streams.mass
        self.logger = logging.getLogger(MASS_LOGGER_NAME).getChild("audio_analysis")
        # session_key -> set of provider instance IDs that accepted this session
        self._active_sessions: dict[str, set[str]] = {}
        self._queues: dict[str, asyncio.Queue[bytes | None]] = {}
        self._workers: dict[str, asyncio.Task[None]] = {}

    @property
    def providers(self) -> list[AudioAnalysisProvider]:
        """Return all available audio analysis providers."""
        return [
            p
            for p in self.mass.get_providers(ProviderType.AUDIO_ANALYSIS)
            if isinstance(p, AudioAnalysisProvider) and p.available
        ]

    async def start_analysis(
        self,
        stream_details: StreamDetails,
        audio_format: AudioFormat,
    ) -> str | None:
        """Start analysis session for a track across all providers.

        :param stream_details: The stream details for the item being analyzed.
        :param audio_format: PCM format of the audio stream.
        :return: Session key if analysis started, None if no providers available.
        """
        providers = self.providers
        if not providers:
            self.logger.debug("No audio analysis providers available")
            return None

        prefix = stream_details.queue_id or "bg"
        session_key = f"{prefix}:{stream_details.provider}:{stream_details.item_id}"

        provider_ids: set[str] = set()
        for provider in providers:
            try:
                await provider.start_analysis(
                    session_id=session_key,
                    stream_details=stream_details,
                    audio_format=audio_format,
                )
                provider_ids.add(provider.instance_id)
                self.logger.debug(
                    "Started analysis session %s on provider %s for %s",
                    session_key,
                    provider.name,
                    stream_details.uri,
                )
            except Exception as err:
                self.logger.warning(
                    "Failed to start analysis on provider %s: %s",
                    provider.name,
                    err,
                )

        if not provider_ids:
            self.logger.debug("No providers accepted analysis for %s", stream_details.uri)
            return None

        self._active_sessions[session_key] = provider_ids
        queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._queues[session_key] = queue
        self._workers[session_key] = self.mass.create_task(self._chunk_worker(session_key, queue))
        return session_key

    async def process_pcm_chunk(self, session_key: str, pcm_chunk: bytes) -> None:
        """Queue a PCM chunk for processing by all providers in a session.

        Returns immediately — the background worker processes chunks sequentially.

        :param session_key: The session key from start_analysis.
        :param pcm_chunk: Raw PCM audio data.
        """
        queue = self._queues.get(session_key)
        if queue is not None:
            queue.put_nowait(pcm_chunk)

    async def finalize(self, session_key: str) -> None:
        """Finalize analysis on all providers in a session.

        Waits for all queued chunks to be processed, then dispatches
        finalize to providers as fire-and-forget tasks.

        :param session_key: The session key from start_analysis.
        """
        # Send sentinel to stop the worker and wait for it to drain all chunks
        queue = self._queues.pop(session_key, None)
        if queue is not None:
            queue.put_nowait(None)
        worker = self._workers.pop(session_key, None)
        if worker is not None:
            await worker

        provider_ids = self._active_sessions.pop(session_key, None)
        if not provider_ids:
            return

        for provider_id in provider_ids:
            provider = self.mass.get_provider(provider_id)
            if provider and isinstance(provider, AudioAnalysisProvider) and provider.available:
                self.mass.create_task(provider.finalize(session_key))

    async def cancel(self, session_key: str) -> None:
        """Cancel an in-progress analysis session.

        :param session_key: The session key from start_analysis.
        """
        self._queues.pop(session_key, None)
        worker = self._workers.pop(session_key, None)
        if worker is not None:
            worker.cancel()

        provider_ids = self._active_sessions.pop(session_key, None)
        if not provider_ids:
            return

        for provider_id in provider_ids:
            provider = self.mass.get_provider(provider_id)
            if provider and isinstance(provider, AudioAnalysisProvider) and provider.available:
                self.mass.create_task(provider.cancel(session_key))

    async def _chunk_worker(self, session_key: str, queue: asyncio.Queue[bytes | None]) -> None:
        """Background worker that processes queued PCM chunks sequentially.

        :param session_key: The session key for this worker.
        :param queue: Queue of PCM chunks (None sentinel signals shutdown).
        """
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
