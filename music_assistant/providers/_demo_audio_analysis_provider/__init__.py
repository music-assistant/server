"""
DEMO/TEMPLATE Audio Analysis Provider for Music Assistant.

This is a demo audio analysis provider with no actual analysis implementation.
It is meant to get started developing a new audio analysis provider for Music Assistant.

Audio Analysis Providers receive raw PCM audio chunks during track streaming and produce
analysis results such as beat tracking, key detection, energy measurements, etc.
The AudioAnalysisController manages the lifecycle: it calls start_analysis when a track
begins streaming, feeds PCM chunks via process_pcm_chunk, and calls finalize (or cancel)
when the stream ends (or is interrupted).

Please take memory into account when processing PCM chunks. The controller feeds 1-second chunks of
raw PCM audio, which can be large (e.g. 44100 samples/sec * 2 bytes/sample * 2 channels = 176 KB
per second). We recommend processing the chunks as they arrive, or with minimal buffering
(e.g. 10 seconds), and avoiding accumulation of large buffers in memory. Often you can get away
with extracting mel spectrograms or other features on the fly and only storing those, rather than
the raw PCM data.

The AudioAnalysisController will automatically skip re-analysis of tracks if the stored analysis
version matches the provider's current analysis_version. Increment your provider's analysis_version
when your algorithm changes significantly to trigger re-analysis of existing tracks. This will
automatically trigger a re-analysis of all tracks that were previously analyzed with an older
version, ensuring that users get updated analysis results without needing to manually
clear caches or re-stream tracks.

To add a new audio analysis provider to Music Assistant, create a new folder in the
providers folder (e.g. 'my_audio_analysis_provider') with at least an __init__.py file
and a manifest.json file.

IMPORTANT NOTE:
We strongly recommend developing on either macOS or Linux and start your development
environment by running the setup.sh script in the scripts folder of the repository.
This will create a virtual environment and install all dependencies needed for development.
See also our general DEVELOPMENT.md guide in the repository for more information.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from music_assistant_models.enums import MediaType

from music_assistant.models.audio_analysis_provider import AudioAnalysisProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry, ProviderConfig
    from music_assistant_models.media_items import AudioFormat
    from music_assistant_models.provider import ProviderManifest
    from music_assistant_models.streamdetails import StreamDetails

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    # setup is called when the user wants to setup a new provider instance.
    # you are free to do any preflight checks here but you must return
    # an instance of the provider.
    return DemoAudioAnalysisProvider(mass, manifest, config)


class DemoAudioAnalysisProvider(AudioAnalysisProvider):
    """
    Demo Audio Analysis Provider.

    This demo provider logs debug messages at each lifecycle stage instead of
    performing actual analysis. Use it as a reference for implementing your own
    audio analysis provider.

    Key concepts:
    - Implement _start_analysis, process_pcm_chunk, and _finalize.
    - The base class handles version gating and session lifecycle — providers
      only need to implement the hooks.
    - Session data (streamdetails, audio_format) is stored in
      self._sessions[session_id]. Access it in process_pcm_chunk and _finalize.
    - Increment analysis_version when your algorithm changes significantly.
      The base class uses this to skip re-analysis of already-analyzed tracks.
    - If you have other conditions that determine whether to skip an analysis,
      implement them in _start_analysis and return False to reject the session.
    - Return AudioAnalysisData from _finalize; the base class persists it.
    """

    # Increment this when your analysis algorithm changes significantly.
    # The controller compares this against the stored version to decide
    # whether to re-analyze a track.
    analysis_version: int = 1

    # Media types this provider will analyze; add RADIO/SOUND_EFFECT if yours suits them.
    supported_media_types: ClassVar[frozenset[MediaType]] = frozenset({MediaType.TRACK})

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """
        Return the (options) config entries for this (existing) provider instance.

        Return an empty tuple when the provider has no options. Interactive setup
        input (if any) is collected by a ``setup_flow.py`` module; one-shot buttons
        are declared here as ``ConfigEntryType.ACTION`` entries and handled in
        ``handle_config_action``.
        """
        return ()

    async def loaded_in_mass(self) -> None:
        """
        Call when the provider is loaded in the MusicAssistant instance.

        This is an optional callback that is called after the provider is loaded
        into the running MusicAssistant instance. Use it to subscribe to events,
        set up background tasks, or perform other initialization that requires
        the full MA instance to be available.
        """
        self.logger.debug("Demo Audio Analysis Provider loaded in Music Assistant")

    async def _start_analysis(
        self,
        session_id: str,
        streamdetails: StreamDetails,
        audio_format: AudioFormat,
    ) -> bool:
        """
        Provider-specific initialization for a new analysis session.

        Called by the base class after version gating and session storage.
        Return True to accept the session, False to reject.

        :param session_id: Unique session ID created by the controller.
        :param streamdetails: Details about the stream being analyzed.
        :param audio_format: PCM format of the audio (sample rate, bit depth, channels).
        """
        self.logger.debug(
            "Started analysis session %s for %s (sample_rate=%d, bit_depth=%d, channels=%d)",
            session_id,
            streamdetails.uri,
            audio_format.sample_rate,
            audio_format.bit_depth,
            audio_format.channels,
        )
        return True

    async def process_pcm_chunk(
        self,
        session_id: str,
        pcm_chunk: bytes,
    ) -> None:
        """
        Process a PCM audio chunk.

        Called for each 1-second chunk of raw PCM audio during streaming.
        A real provider would feed this data into its analysis algorithm
        (e.g. beat tracker, key detector, loudness analyzer).

        The audio format is available via self._sessions[session_id].audio_format.

        :param session_id: The analysis session ID.
        :param pcm_chunk: Raw PCM audio data (1 second of audio).
        """
        self.logger.debug(
            "Processing chunk for session %s (%d bytes)",
            session_id,
            len(pcm_chunk),
        )

    async def _finalize(self, session_id: str) -> None:
        """
        Finalize analysis and return the result.

        Called when the track has finished buffering and all chunks have been
        processed. A real provider would compute its final result and return it
        as an AudioAnalysisData; the base class then persists it via
        set_audio_analysis() and fires post_analysis(). Return None to skip both.

        Example return (not done in this demo)::

            from music_assistant.models.audio_analysis import AudioAnalysisData

            return AudioAnalysisData(bpm=120.0, duration=180.5)

        Note: The base class's finalize() method calls this, then cleans up
        the session from self._sessions automatically. Do not override finalize()
        directly — override _finalize() instead.

        :param session_id: The analysis session ID.
        """
        self.logger.debug("Finalizing analysis session %s", session_id)

    async def cancel(self, session_id: str) -> None:
        """
        Cancel an in-progress analysis session.

        Called when the stream is interrupted (e.g. user skips the track,
        buffer is cleared). A real provider should discard any partial state
        for this session. The base class removes the session from self._sessions.

        :param session_id: The analysis session ID.
        """
        self.logger.debug("Cancelling analysis session %s", session_id)
        await super().cancel(session_id)

    async def unload(self, is_removed: bool = False) -> None:
        """
        Handle unload/removal of the provider.

        Called when the provider is being unloaded or removed. The base class
        cancels all active sessions automatically.

        :param is_removed: Whether the provider is being permanently removed.
        """
        self.logger.debug("Unloading Demo Audio Analysis Provider (removed=%s)", is_removed)
        await super().unload(is_removed)
