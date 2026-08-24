"""Backend contract for the Spotify Connect provider."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Final

from music_assistant_models.config_entries import ConfigValueOption
from music_assistant_models.enums import ContentType
from music_assistant_models.media_items import AudioFormat

if TYPE_CHECKING:
    from music_assistant_models.enums import RepeatMode

    from music_assistant.providers.spotify_connect.models import (
        AudioChunkReader,
        BackendStreamSource,
    )

# Streaming quality tiers, named after the Spotify apps' own vocabulary for
# the same bitrates. They express a ceiling, not a guarantee: Spotify still
# downshifts on a slow connection and falls back when a track (or the account)
# has no file at the requested tier. Each backend maps these onto whatever its
# engine understands, clamping to what that engine can actually deliver.
AUDIO_QUALITY_NORMAL: Final = "normal"
AUDIO_QUALITY_HIGH: Final = "high"
AUDIO_QUALITY_VERY_HIGH: Final = "very_high"
AUDIO_QUALITY_LOSSLESS: Final = "lossless"

# The tiers as a config-entry option list, in ascending order. Shared so the
# Spotify music provider's own soloist playback offers the same choice.
AUDIO_QUALITY_OPTIONS: Final = [
    ConfigValueOption(AUDIO_QUALITY_NORMAL),
    ConfigValueOption(AUDIO_QUALITY_HIGH),
    ConfigValueOption(AUDIO_QUALITY_VERY_HIGH),
    ConfigValueOption(AUDIO_QUALITY_LOSSLESS),
]

# The bitrate each tier maps onto in kbps, matching the Spotify apps' own
# vocabulary. Both the engines' own bitrate setting and the format advertised for
# display come from here, so what we ask for is what we claim. Spoken content is
# never lossless, and neither is go-librespot, so the lossless tier falls back to
# the highest lossy rate for both rather than claiming more than they can deliver.
MAX_LOSSY_BIT_RATE: Final[int] = 320
LOSSY_BIT_RATES: Final[dict[str, int]] = {
    AUDIO_QUALITY_NORMAL: 96,
    AUDIO_QUALITY_HIGH: 160,
    AUDIO_QUALITY_VERY_HIGH: MAX_LOSSY_BIT_RATE,
    AUDIO_QUALITY_LOSSLESS: MAX_LOSSY_BIT_RATE,
}


def spotify_source_audio_format(quality: str, *, lossless: bool) -> AudioFormat:
    """
    Return the format Spotify is asked to serve at a streaming tier.

    No engine reveals what it actually fetched, so this describes the configured
    ceiling — the same thing the Spotify apps show. An engine that decodes on our
    behalf hands over its own PCM, which is what ``decoded_audio_format``
    describes; this stays the source of those samples.

    :param quality: The configured AUDIO_QUALITY_* tier.
    :param lossless: Whether the stream is served losslessly, which needs both the
        lossless tier and an engine and content that can deliver it.
    """
    if lossless:
        return AudioFormat(
            content_type=ContentType.FLAC,
            codec_type=ContentType.FLAC,
            sample_rate=44100,
            bit_depth=24,
            channels=2,
        )
    return AudioFormat(
        content_type=ContentType.OGG,
        codec_type=ContentType.VORBIS,
        sample_rate=44100,
        bit_depth=16,
        channels=2,
        bit_rate=LOSSY_BIT_RATES.get(quality, MAX_LOSSY_BIT_RATE),
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

        Pipe-fed backends never reach one; the provider then stops the player
        actively on the paused state event.
        """
        return True

    @property
    def supports_queue_control(self) -> bool:
        """
        Whether the backend implements the queue-session verbs.

        A backend returning True implements ``add_to_queue``, ``set_shuffle``,
        ``set_repeat`` and ``request_queue`` and emits QUEUE_CHANGED /
        OPTIONS_CHANGED events.
        """
        return False

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
    async def deactivate(self) -> None:
        """
        Release this device as the active Spotify Connect device.

        Ends the current session so the Spotify apps drop the device as their
        playback target; the device stays available for reselection.
        """

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

    async def add_to_queue(self, uri: str) -> None:
        """
        Add a track to the session's play queue.

        Only available on backends with ``supports_queue_control``.

        :param uri: Spotify track URI to queue.
        """
        raise NotImplementedError

    async def set_shuffle(self, enabled: bool) -> None:
        """
        Enable or disable shuffle on the active session.

        Only available on backends with ``supports_queue_control``.

        :param enabled: True to enable shuffle, False to disable it.
        """
        raise NotImplementedError

    async def set_repeat(self, repeat: RepeatMode) -> None:
        """
        Set the repeat mode on the active session.

        Only available on backends with ``supports_queue_control``. May await
        the engine's acknowledgement, so the call can block and raise — never
        call it from the backend event callback (the acknowledgement arrives
        on the same loop and the wait could only time out).

        :param repeat: OFF for no repeat, ONE for the current track, ALL for
            the playing context.
        """
        raise NotImplementedError

    async def request_queue(self, limit: int = 10) -> None:
        """
        Ask the session to (re)emit its queue view.

        Only available on backends with ``supports_queue_control``. There is
        no return value: the snapshot arrives as a QUEUE_CHANGED event.

        :param limit: Maximum number of upcoming entries the snapshot should
            include.
        """
        raise NotImplementedError
