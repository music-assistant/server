"""Model/base for an Audio Analysis Provider implementation."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

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

    streamdetails: StreamDetails
    audio_format: AudioFormat


class AudioAnalysisProvider(Provider):
    """Base representation of an Audio Analysis Provider.

    Audio Analysis Provider implementations should inherit from this base model.
    These providers receive PCM audio chunks during streaming and produce analysis
    results such as beat tracking, key detection, phrase boundaries, etc.

    The AudioAnalysisController creates session IDs and passes them to all methods.
    Providers implement _start_analysis and _finalize as hooks — the base class
    manages session lifecycle, version gating, and cleanup.
    """

    # Version of the analysis algorithm. Providers should increment this when
    # their algorithm changes significantly. The base class compares this against
    # the stored version to decide whether to re-analyze a track.
    analysis_version: int = 1

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
        streamdetails: StreamDetails,
        audio_format: AudioFormat,
    ) -> bool:
        """Start analysis for a new session.

        Checks whether analysis is needed (version gating), stores session data,
        and calls _start_analysis for provider-specific initialization.
        Returns True if the provider accepted the session.

        :param session_id: Session ID created by the AudioAnalysisController.
        :param streamdetails: The stream details for the item being analyzed.
        :param audio_format: PCM format of the audio stream.
        """
        stored_version = await self.mass.streams.audio_analysis.get_audio_analysis_version(
            streamdetails.item_id,
            streamdetails.provider,
            self.domain,
            media_type=streamdetails.media_type,
        )
        if stored_version is not None and stored_version >= self.analysis_version:
            return False
        self._sessions[session_id] = AnalysisSessionData(
            streamdetails=streamdetails,
            audio_format=audio_format,
        )
        if not await self._start_analysis(session_id, streamdetails, audio_format):
            self._sessions.pop(session_id, None)
            return False
        return True

    @abstractmethod
    async def _start_analysis(
        self,
        session_id: str,
        streamdetails: StreamDetails,
        audio_format: AudioFormat,
    ) -> bool:
        """Provider-specific initialization for a new analysis session.

        Called by start_analysis after version gating and session storage.
        Return False to reject the session (e.g. unsupported format).
        Session data is available in self._sessions[session_id].

        :param session_id: The analysis session ID.
        :param streamdetails: The stream details for the item being analyzed.
        :param audio_format: PCM format of the audio stream.
        """

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
    async def _finalize(self, session_id: str) -> None:
        """Finalize analysis and store results.

        Called when the track has finished streaming. Providers are responsible
        for storing their results via mass.streams.audio_analysis.set_audio_analysis().

        :param session_id: The analysis session ID.
        """

    async def finalize(self, session_id: str) -> None:
        """Finalize analysis and clean up session state.

        Calls _finalize, then removes the session from _sessions.
        The controller calls this method — providers override _finalize.

        :param session_id: The analysis session ID.
        """
        try:
            await self._finalize(session_id)
        finally:
            self._sessions.pop(session_id, None)

    async def cancel(self, session_id: str) -> None:
        """Cancel an in-progress analysis session."""
        self._sessions.pop(session_id, None)

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload, cancelling any active analysis sessions."""
        for session_id in list(self._sessions):
            await self.cancel(session_id)
        await super().unload(is_removed)
