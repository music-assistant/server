"""Playback backend contract for the Spotify music provider."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from music_assistant_models.media_items import AudioFormat
    from music_assistant_models.streamdetails import StreamDetails

    from music_assistant.helpers.json import SerializableType
    from music_assistant.providers.spotify.provider import SpotifyProvider


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

    @property
    @abstractmethod
    def audio_format(self) -> AudioFormat:
        """Return the audio format this backend delivers, for use in StreamDetails."""

    @property
    @abstractmethod
    def max_concurrent_streams(self) -> int:
        """Return how many items this backend can fetch concurrently."""

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
        """

    async def get_diagnostics(self) -> dict[str, SerializableType]:
        """Return diagnostic details about the backend (never any secret)."""
        return {}
