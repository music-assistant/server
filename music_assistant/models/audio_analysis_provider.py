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
    """Base representation of an Audio Analysis Provider.

    Audio Analysis Provider implementations should inherit from this base model.
    These providers receive PCM audio chunks during streaming and produce analysis
    results such as beat tracking, key detection, phrase boundaries, etc.

    Providers implement four hooks (`_start_analysis`, `process_pcm_chunk`,
    `_finalize`, optionally `post_analysis`) and the base class manages session
    lifecycle, version gating, and cleanup. The same hooks drive both live
    playback (PCM from `AudioBuffer`) and background scans (PCM from ffmpeg
    decoding a local file). Providers do not need to know which context they
    are running in.

    Provider contract (binding):

    1. `process_pcm_chunk` MUST `await` all work that processes the chunk.
       The controller serializes chunks across providers and uses this to
       backpressure the audio source. Fire-and-forget per-chunk work breaks
       backpressure and can pile up unboundedly at flat-out background rates.

    2. Providers MAY spawn background tasks during `process_pcm_chunk` only
       when the total task count is bounded by per-track properties (e.g. a
       fixed number of CLAP target windows configured per track). All such
       tasks MUST be tracked and awaited in `_finalize`.

    3. Providers MUST NOT begin work for session N+1 while session N is
       still active. Per-session state should be keyed on `session_id`.

    4. `_finalize` MUST return the AudioAnalysisData it persisted, or None
       if it chose not to persist (e.g. insufficient audio). The base class
       uses the return value to drive `post_analysis`.

    5. `post_analysis` is optional and called by the base class after
       `_finalize` returns a non-None analysis. It is the place for filesystem
       side effects such as tag-writing. Implementations MUST self-gate on
       whether `streamdetails.path` is a writable filesystem path, because
       this hook fires for both live and background scans.
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
    async def _finalize(self, session_id: str) -> AudioAnalysisData | None:
        """Finalize analysis and return the persisted analysis (or None to skip).

        Called when the track has finished streaming. Providers are responsible
        for storing their results via mass.streams.audio_analysis.set_audio_analysis().
        Return the AudioAnalysisData that was persisted, or None if the provider
        chose not to store a result (e.g. insufficient audio data). The base
        class uses the returned value to drive the post_analysis hook.

        :param session_id: The analysis session ID.
        """

    async def finalize(self, session_id: str) -> None:
        """Finalize analysis, optionally fire post_analysis, and clean up state.

        Calls _finalize, then post_analysis (when _finalize returned a non-None
        analysis), then removes the session from _sessions. Both _finalize and
        post_analysis exceptions are caught and logged — neither is allowed to
        leave session state behind or to propagate to the controller.

        :param session_id: The analysis session ID.
        """
        analysis: AudioAnalysisData | None = None
        try:
            analysis = await self._finalize(session_id)
        except Exception as err:
            self.logger.error("_finalize raised for session %s: %s", session_id, err, exc_info=err)
        session = self._sessions.get(session_id)
        if analysis is not None and session is not None:
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
        """Run side effects after analysis is finalized and persisted.

        Called by the base class `finalize` wrapper after `_finalize` returns
        a non-None analysis. Default is a no-op. Providers override this to
        perform filesystem side effects such as writing tags back to the
        source file. Failures are caught by the base class and logged — they
        must not undo the analysis row.

        Implementations must self-gate on whether `streamdetails.path` is a
        writable filesystem path, since this hook fires for both live and
        background-scan analyses.

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
