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
    from music_assistant.models.audio_analysis import AudioAnalysisData


@dataclass
class AnalysisSessionData:
    """Base session data stored per analysis session."""

    streamdetails: StreamDetails
    audio_format: AudioFormat


class AudioAnalysisProvider(Provider):
    """
    Base representation of an Audio Analysis Provider.

    Receives PCM audio chunks during streaming and produces analysis results
    such as beat tracking, key detection, or loudness. The same hooks drive
    both live playback and background scans; providers do not need to know
    which context they are running in.
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
        """
        Start analysis for a new session.

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
        """
        Provider-specific initialization for a new analysis session.

        Return False to reject the session (e.g. unsupported format).

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
        """
        Process a PCM audio chunk.

        Implementations MUST `await` all chunk-processing work; the controller
        relies on this to backpressure the audio source.

        :param session_id: The analysis session ID.
        :param pcm_chunk: Raw PCM audio data.
        """

    @abstractmethod
    async def _finalize(self, session_id: str) -> AudioAnalysisData | None:
        """
        Compute and return the analysis for this session (or None to skip).

        The base class persists the returned value via set_audio_analysis() and
        then fires post_analysis(). Return None to skip both.

        :param session_id: The analysis session ID.
        """

    async def finalize(self, session_id: str) -> None:
        """Finalize analysis, persist the result, fire post_analysis, then clean up."""
        analysis: AudioAnalysisData | None = None
        try:
            analysis = await self._finalize(session_id)
        except Exception as err:
            self.logger.error("_finalize raised for session %s: %s", session_id, err, exc_info=err)
        session = self._sessions.get(session_id)
        if analysis is not None and session is not None:
            try:
                await self.mass.streams.audio_analysis.set_audio_analysis(
                    item_id=session.streamdetails.item_id,
                    provider_instance_id_or_domain=session.streamdetails.provider,
                    aa_provider_domain=self.domain,
                    analysis=analysis,
                    analysis_version=self.analysis_version,
                    media_type=session.streamdetails.media_type,
                )
            except Exception as err:
                self.logger.warning(
                    "set_audio_analysis raised for %s: %s", self.domain, err, exc_info=err
                )
            else:
                try:
                    await self.post_analysis(session.streamdetails, analysis)
                except Exception as err:
                    self.logger.warning(
                        "post_analysis raised for %s: %s", self.domain, err, exc_info=err
                    )
        self._sessions.pop(session_id, None)

    async def post_analysis(
        self,
        streamdetails: StreamDetails,
        analysis: AudioAnalysisData,
    ) -> None:
        """
        Run side effects after analysis is finalized and persisted.

        Default is a no-op. Implementations MUST self-gate on whether
        `streamdetails.path` is a writable filesystem path, since this hook
        fires for both live and background-scan analyses.

        :param streamdetails: The stream details for the analyzed item.
        :param analysis: The analysis data that was persisted by `_finalize`.
        """
        return

    async def cancel(self, session_id: str) -> None:
        """Cancel an in-progress analysis session."""
        self._sessions.pop(session_id, None)

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload, cancelling any active analysis sessions."""
        for session_id in list(self._sessions):
            await self.cancel(session_id)
        await super().unload(is_removed)
