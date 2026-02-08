"""Model/base for an Audio Analysis Provider implementation."""

from __future__ import annotations

from abc import abstractmethod
from typing import TYPE_CHECKING, Any

from .provider import Provider

if TYPE_CHECKING:
    from music_assistant_models.media_items import AudioFormat
    from music_assistant_models.queue_item import QueueItem


class AudioAnalysisProvider(Provider):
    """Base representation of an Audio Analysis Provider.

    Audio Analysis Provider implementations should inherit from this base model.
    These providers receive PCM audio chunks during streaming and produce analysis
    results such as beat tracking, key detection, phrase boundaries, etc.
    """

    @abstractmethod
    async def start_analysis(
        self,
        queue_item: QueueItem,
        audio_format: AudioFormat,
    ) -> str:
        """Start analysis for a new track.

        Called when a new track starts streaming. The provider should initialize
        any state needed for processing this track.

        :param queue_item: The queue item being analyzed.
        :param audio_format: PCM format of the audio stream.
        :return: A unique analysis session ID for this track.
        """

    @abstractmethod
    async def process_pcm_chunk(
        self,
        session_id: str,
        pcm_chunk: bytes,
    ) -> None:
        """Process a PCM audio chunk.

        Called for each chunk of audio data during streaming.

        :param session_id: The analysis session ID from start_analysis.
        :param pcm_chunk: Raw PCM audio data.
        """

    @abstractmethod
    async def finalize(self, session_id: str) -> dict[str, Any]:
        """Finalize analysis and return results.

        Called when the track has finished streaming.

        :param session_id: The analysis session ID from start_analysis.
        :return: Dictionary of analysis results (provider-specific format).
        """

    async def cancel(self, session_id: str) -> None:
        """Cancel an in-progress analysis session.

        Called if streaming is interrupted (skip, stop, error).

        :param session_id: The analysis session ID to cancel.
        """
        # Default implementation does nothing; providers can override
