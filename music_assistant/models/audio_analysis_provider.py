"""Model/base for an Audio Analysis Provider implementation."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .provider import Provider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.enums import ProviderFeature
    from music_assistant_models.media_items import AudioFormat
    from music_assistant_models.provider import ProviderManifest
    from music_assistant_models.streamdetails import StreamDetails

    from music_assistant.mass import MusicAssistant


@dataclass
class AnalysisSessionData:
    """Base session data stored per analysis session."""

    stream_details: StreamDetails
    audio_format: AudioFormat


class AudioAnalysisProvider(Provider):
    """Base representation of an Audio Analysis Provider.

    Audio Analysis Provider implementations should inherit from this base model.
    These providers receive PCM audio chunks during streaming and produce analysis
    results such as beat tracking, key detection, phrase boundaries, etc.

    The AudioAnalysisController creates session IDs and passes them to all methods.
    The default start_analysis stores stream_details and audio_format in self._sessions.
    Providers that need richer per-session state can override start_analysis and cancel.
    """

    def __init__(
        self,
        mass: MusicAssistant,
        manifest: ProviderManifest,
        config: ProviderConfig,
        supported_features: set[ProviderFeature] | None = None,
    ) -> None:
        """Initialize AudioAnalysisProvider."""
        super().__init__(mass, manifest, config, supported_features)
        self._sessions: dict[str, AnalysisSessionData] = {}

    async def start_analysis(
        self,
        session_id: str,
        stream_details: StreamDetails,
        audio_format: AudioFormat,
    ) -> None:
        """Start analysis for a new session.

        Called when a new track starts streaming. The default implementation stores
        stream_details and audio_format in self._sessions. Override to initialize
        richer per-session state.

        :param session_id: Session ID created by the AudioAnalysisController.
        :param stream_details: The stream details for the item being analyzed.
        :param audio_format: PCM format of the audio stream.
        """
        self._sessions[session_id] = AnalysisSessionData(
            stream_details=stream_details,
            audio_format=audio_format,
        )

    @abstractmethod
    async def process_pcm_chunk(
        self,
        session_id: str,
        pcm_chunk: bytes,
    ) -> None:
        """Process a PCM audio chunk.

        Called for each chunk of audio data during streaming.

        :param session_id: The analysis session ID.
        :param pcm_chunk: Raw PCM audio data.
        """

    @abstractmethod
    async def finalize(self, session_id: str) -> dict[str, Any]:
        """Finalize analysis and return results.

        Called when the track has finished streaming.

        :param session_id: The analysis session ID.
        :return: Dictionary of analysis results (provider-specific format).
        """

    async def cancel(self, session_id: str) -> None:
        """Cancel an in-progress analysis session.

        Called if streaming is interrupted (skip, stop, error).
        Default implementation removes the session data.

        :param session_id: The analysis session ID to cancel.
        """
        self._sessions.pop(session_id, None)
