"""Playback backend contract for the Spotify music provider."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from music_assistant_models.errors import AudioError

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from music_assistant_models.enums import MediaType
    from music_assistant_models.media_items import AudioFormat
    from music_assistant_models.streamdetails import StreamDetails

    from music_assistant.helpers.json import SerializableType
    from music_assistant.providers.spotify.provider import SpotifyProvider


class StreamSupersededError(AudioError):
    """
    Raised when Music Assistant replaced the stream that was delivering an item.

    A backend that serves a queue from one session cuts the stream of an item it
    is asked to serve from a new one - a seek - so nothing beyond the cut belongs
    to the stream that was replaced.
    """


class SpotifyPlaybackBackend(ABC):
    """
    One way to fetch Spotify audio, item by item.

    The SpotifyProvider owns everything catalog/Web API related; a backend owns
    the playback session and the per-item audio fetch. All URIs passed to a
    backend use Spotify's canonical form (``spotify:track:<id>`` /
    ``spotify:episode:<id>``).
    """

    def __init__(self, provider: SpotifyProvider) -> None:
        """
        Initialize the backend (cheap; real setup happens in ``setup``).

        :param provider: The owning Spotify provider instance.
        """
        self.provider = provider
        self.mass = provider.mass
        self.logger = provider.logger

    @abstractmethod
    def source_audio_format(self, media_type: MediaType) -> AudioFormat:
        """
        Return the format of the Spotify source, for StreamDetails and display.

        :param media_type: What is being streamed; Spotify serves music and
            spoken content at different qualities.
        """

    @property
    def handoff_audio_format(self) -> AudioFormat | None:
        """
        Return the format this backend actually hands over, when it differs.

        None means the source arrives untouched, so the source format describes
        the bytes as well.
        """
        return None

    @property
    def is_realtime(self) -> bool:
        """Return whether this backend delivers audio at playback pace (no read-ahead)."""
        return False

    @abstractmethod
    async def setup(self) -> None:
        """
        Validate availability and prepare the playback session.

        :raises LoginFailed: When the stored playback authorization is missing or
            unusable, requiring the user to re-run the setup flow.
        """

    async def unload(self) -> None:  # noqa: B027
        """Release any resources held by the backend."""

    @abstractmethod
    def stream_spotify_uri(
        self,
        spotify_uri: str,
        seek_position: int = 0,
        *,
        streamdetails: StreamDetails | None = None,
    ) -> AsyncGenerator[bytes]:
        """
        Yield the audio for one Spotify URI in this backend's audio format.

        :param spotify_uri: Canonical Spotify URI (``spotify:track:<id>`` or
            ``spotify:episode:<id>``).
        :param seek_position: Position in seconds to start from.
        :param streamdetails: The StreamDetails the audio is requested for.
            Backends that fetch each item on its own ignore these; a backend
            that keeps one session needs them to know which queue and which
            item of it this audio belongs to.
        :raises StreamSupersededError: When Music Assistant replaced this stream
            with a new one for the same item, leaving it nothing to deliver.
        """

    async def get_diagnostics(self) -> dict[str, SerializableType]:
        """Return diagnostic details about the backend (never any secret)."""
        return {}
