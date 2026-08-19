"""Backend contract for the Spotify Connect provider."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from music_assistant_models.media_items import AudioFormat

    from music_assistant.providers.spotify_connect.models import (
        AudioChunkReader,
        BackendStreamSource,
    )


class SpotifyConnectBackend(ABC):
    """
    Contract between the SpotifyConnectProvider and a Spotify Connect implementation.

    A backend owns everything specific to one way of talking to Spotify
    (daemon lifecycle, credentials, wire protocol, audio delivery) and reports
    state changes as normalized ``BackendEvent``s (see ``models.py``) through
    the single async callback supplied at construction time. The provider
    drives the backend exclusively through the methods below, so it never
    needs to know which backend it is talking to.
    """

    @property
    @abstractmethod
    def audio_format(self) -> AudioFormat:
        """Return the source audio format (advertised to clients for display)."""

    @property
    @abstractmethod
    def decoded_audio_format(self) -> AudioFormat:
        """Return the decoded PCM format the audio reader actually delivers."""

    @property
    def stream_ends_on_pause(self) -> bool:
        """
        Whether the audio stream reaches a clean end when playback pauses.

        Pipe-fed backends deliver silence on pause instead; the provider then
        stops the player actively on the paused state event.
        """
        return True

    @abstractmethod
    async def start(self) -> None:
        """Start the backend and its supervised Spotify Connect implementation."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop the backend and release all its resources."""

    @abstractmethod
    async def get_stream_source(self) -> BackendStreamSource:
        """
        Return how the streams controller should consume this backend's audio.

        Called on every stream request — including queue preload, so this must
        be side-effect-free. The result describes the live audio delivery
        (stream type, optional pipe path and extra ffmpeg input arguments).
        The delivered PCM is in ``decoded_audio_format``.
        """

    @abstractmethod
    def get_audio_reader(self) -> AudioChunkReader | None:
        """
        Return a PCM chunk reader bound to the currently live audio pipe.

        The reader yields raw PCM in ``decoded_audio_format`` and returns an
        empty bytes object once that pipe closes (it does not follow a backend
        restart). None is returned when no audio pipe is available.
        """

    @abstractmethod
    async def play(self, uri: str, *, skip_to_uri: str | None = None) -> None:
        """
        Start playing a Spotify URI/context, making this device the active one.

        :param uri: Spotify URI (track, album, playlist, ...) — typically a context.
        :param skip_to_uri: Optional track URI within the context to start at.
        """

    @abstractmethod
    async def resume(self) -> None:
        """Resume playback on the active session."""

    @abstractmethod
    async def pause(self) -> None:
        """Pause playback on the active session."""

    @abstractmethod
    async def next(self) -> None:
        """Skip to the next track."""

    @abstractmethod
    async def previous(self) -> None:
        """Skip to the previous track (or rewind the current one)."""

    @abstractmethod
    async def seek(self, position_ms: int) -> None:
        """
        Seek to an absolute position in the current track.

        :param position_ms: Target position in milliseconds.
        """

    @abstractmethod
    async def set_volume(self, volume: int) -> None:
        """
        Set the Spotify-side playback volume.

        :param volume: Absolute volume as a 0-100 percentage.
        """
