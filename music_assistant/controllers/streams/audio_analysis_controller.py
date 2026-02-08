"""Controller for distributing audio analysis to providers."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import ProviderType
from music_assistant_models.media_items import AudioFormat

from music_assistant.constants import MASS_LOGGER_NAME
from music_assistant.models.audio_analysis_provider import AudioAnalysisProvider

if TYPE_CHECKING:
    from music_assistant_models.queue_item import QueueItem

    from music_assistant.controllers.streams.streams_controller import StreamsController


@dataclass
class AnalysisSession:
    """Tracks an active analysis session across providers."""

    queue_item_id: str
    item_id: str
    provider: str
    audio_format: AudioFormat
    provider_sessions: dict[str, str] = field(default_factory=dict)


class AudioAnalysisController:
    """Controller that distributes PCM chunks to all registered AudioAnalysisProviders.

    This is a child controller owned by StreamsController. It coordinates analysis
    across multiple providers, each of which can process audio independently.
    Providers are responsible for storing their own results.
    """

    def __init__(self, streams: StreamsController) -> None:
        """Initialize the AudioAnalysisController.

        :param streams: Parent StreamsController instance.
        """
        self.streams = streams
        self.mass = streams.mass
        self.logger = logging.getLogger(MASS_LOGGER_NAME).getChild("audio_analysis")
        self._active_sessions: dict[str, AnalysisSession] = {}

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
        queue_item: QueueItem,
        queue_id: str,
        audio_format: AudioFormat,
    ) -> str | None:
        """Start analysis session for a track across all providers.

        :param queue_item: The queue item being analyzed.
        :param queue_id: The player queue ID.
        :param audio_format: PCM format of the audio stream.
        :return: Session key if analysis started, None if no providers available.
        """
        if not queue_item.streamdetails:
            return None

        providers = self.providers
        if not providers:
            self.logger.debug("No audio analysis providers available")
            return None

        session_key = f"{queue_id}:{queue_item.queue_item_id}"

        session = AnalysisSession(
            queue_item_id=queue_item.queue_item_id,
            item_id=queue_item.streamdetails.item_id,
            provider=queue_item.streamdetails.provider,
            audio_format=audio_format,
        )

        # Start analysis on each provider
        for provider in providers:
            try:
                provider_session_id = await provider.start_analysis(
                    queue_item=queue_item,
                    audio_format=audio_format,
                )
                session.provider_sessions[provider.instance_id] = provider_session_id
                self.logger.debug(
                    "Started analysis session %s on provider %s for %s",
                    provider_session_id,
                    provider.name,
                    queue_item.name,
                )
            except Exception as err:
                self.logger.warning(
                    "Failed to start analysis on provider %s: %s",
                    provider.name,
                    err,
                )

        if not session.provider_sessions:
            self.logger.debug("No providers accepted analysis for %s", queue_item.name)
            return None

        self._active_sessions[session_key] = session
        return session_key

    async def process_pcm_chunk(self, session_key: str, pcm_chunk: bytes) -> None:
        """Distribute a PCM chunk to all providers in a session.

        :param session_key: The session key from start_analysis.
        :param pcm_chunk: Raw PCM audio data.
        """
        session = self._active_sessions.get(session_key)
        if not session:
            return

        # Process chunk on all providers concurrently
        tasks = []
        for provider_instance_id, provider_session_id in session.provider_sessions.items():
            provider = self.mass.get_provider(provider_instance_id)
            if provider and isinstance(provider, AudioAnalysisProvider) and provider.available:
                tasks.append(provider.process_pcm_chunk(provider_session_id, pcm_chunk))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def finalize(self, session_key: str) -> None:
        """Finalize analysis and collect results from all providers.

        Providers are responsible for storing their own results.

        :param session_key: The session key from start_analysis.
        :return: Dictionary mapping provider instance_id to results.
        """
        session = self._active_sessions.pop(session_key, None)
        if not session:
            return

        results: dict[str, dict[str, Any]] = {}

        async def finalize_provider(
            provider_instance_id: str, provider_session_id: str
        ) -> tuple[str, dict[str, Any] | None]:
            provider = self.mass.get_provider(provider_instance_id)
            if (
                not provider
                or not isinstance(provider, AudioAnalysisProvider)
                or not provider.available
            ):
                return provider_instance_id, None
            try:
                result = await provider.finalize(provider_session_id)
                return provider_instance_id, result
            except Exception as err:
                self.logger.warning(
                    "Failed to finalize analysis on provider %s: %s",
                    provider_instance_id,
                    err,
                )
                return provider_instance_id, None

        tasks = [
            finalize_provider(prov_id, sess_id)
            for prov_id, sess_id in session.provider_sessions.items()
        ]

        for provider_id, result in await asyncio.gather(*tasks):
            if result:
                results[provider_id] = result

        return

    async def cancel(self, session_key: str) -> None:
        """Cancel an in-progress analysis session.

        :param session_key: The session key from start_analysis.
        """
        session = self._active_sessions.pop(session_key, None)
        if not session:
            return

        # Cancel on all providers
        for provider_instance_id, provider_session_id in session.provider_sessions.items():
            provider = self.mass.get_provider(provider_instance_id)
            if provider and isinstance(provider, AudioAnalysisProvider) and provider.available:
                try:
                    await provider.cancel(provider_session_id)
                except Exception as err:
                    self.logger.debug(
                        "Error cancelling analysis on provider %s: %s",
                        provider_instance_id,
                        err,
                    )
