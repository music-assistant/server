"""Normalized models shared between the Spotify Connect provider and its backends."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from music_assistant_models.enums import RepeatMode

if TYPE_CHECKING:
    from music_assistant_models.enums import StreamType


class BackendEventType(StrEnum):
    """Type discriminator for the normalized events a backend emits to the provider."""

    # session lifecycle: this device became / stopped being the active Spotify device
    SESSION_ACTIVE = "session_active"
    SESSION_INACTIVE = "session_inactive"
    # playback state reported by the backend (BUFFERING is informational: reserved
    # for backends that report it, the provider does not act on it)
    PLAYING = "playing"
    PAUSED = "paused"
    STOPPED = "stopped"
    BUFFERING = "buffering"
    # track metadata and playback position updates
    METADATA = "metadata"
    POSITION = "position"
    # Spotify-side volume change (normalized to a 0-100 percentage)
    VOLUME = "volume"
    # queue listing / playback options reported by the session (only emitted
    # by backends with ``supports_queue_control``)
    QUEUE_CHANGED = "queue_changed"
    OPTIONS_CHANGED = "options_changed"
    # the backend lost its Spotify connection (e.g. daemon exit) and will recover
    # on its own; any session/playback state is gone until a new SESSION_ACTIVE
    CONNECTION_LOST = "connection_lost"
    # a non-fatal backend error worth surfacing (message in the ``error`` field)
    ERROR = "error"
    # the backend failed permanently and the provider must unload with an error
    FATAL_ERROR = "fatal_error"
    # any other backend activity; carries at most refreshed context/track uris
    OTHER = "other"


@dataclass(slots=True, frozen=True)
class BackendStreamSource:
    """
    How a backend delivers its audio to the streams controller.

    ``path`` is only set for path-based stream types (e.g. NAMED_PIPE); CUSTOM
    sources deliver their audio through the backend's audio reader instead.
    ``extra_input_args`` are passed to ffmpeg for the audio input.
    """

    stream_type: StreamType
    path: str | None = None
    extra_input_args: list[str] = field(default_factory=list)


@dataclass(slots=True)
class BackendTrackMetadata:
    """
    Normalized track metadata carried by a METADATA event.

    ``duration`` and ``position`` are in seconds. A None ``title`` means the
    backend did not report one (the provider keeps the previous title).
    """

    track_uri: str | None = None
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    image_url: str | None = None
    duration: int | None = None
    position: int = 0


class QueueEntrySource(StrEnum):
    """Where an entry in the session's queue listing comes from."""

    # part of the playing context (album/playlist/artist)
    CONTEXT = "context"
    # explicitly queued by the user
    QUEUE = "queue"
    # session autoplay continuation
    AUTOPLAY = "autoplay"
    UNKNOWN = "unknown"

    @classmethod
    def _missing_(cls, value: object) -> QueueEntrySource:  # noqa: ARG003
        """Return UNKNOWN if an unknown value is provided."""
        return cls.UNKNOWN


@dataclass(slots=True)
class BackendQueueEntry:
    """
    One entry in the session's queue listing.

    ``uid`` is the session's stable handle for this entry (two occurrences of
    the same track carry distinct uids). ``name`` is the display title when
    the backend reported one.
    """

    uid: str
    uri: str
    source: QueueEntrySource
    name: str | None = None


@dataclass(slots=True)
class BackendQueueState:
    """The session's queue view: recently played and upcoming entries, in play order."""

    previous: list[BackendQueueEntry] = field(default_factory=list)
    upcoming: list[BackendQueueEntry] = field(default_factory=list)


@dataclass(slots=True)
class BackendPlaybackOptions:
    """Playback options of the session."""

    shuffle: bool = False
    repeat: RepeatMode = RepeatMode.OFF


@dataclass(slots=True)
class BackendEvent:
    """
    A single normalized event emitted by a backend to the provider.

    ``context_uri`` / ``track_uri`` piggyback on every event type: they carry
    the latest context/track seen by the backend so the provider can take
    playback back after the user moved the active device away. ``position`` is
    the elapsed time in seconds (POSITION events), ``volume`` a 0-100
    percentage (VOLUME events), ``error`` the failure description (ERROR and
    FATAL_ERROR events), ``queue`` the session's queue view (QUEUE_CHANGED
    events) and ``options`` the session's playback options (OPTIONS_CHANGED
    events).
    """

    type: BackendEventType
    context_uri: str | None = None
    track_uri: str | None = None
    metadata: BackendTrackMetadata | None = None
    position: int | None = None
    volume: int | None = None
    error: str | None = None
    queue: BackendQueueState | None = None
    options: BackendPlaybackOptions | None = None


# Awaited by the backend for every normalized event, in emit order.
BackendEventCallback = Callable[[BackendEvent], Awaitable[None]]

# Reads the next chunk of decoded PCM; returns b"" once the audio pipe closes.
AudioChunkReader = Callable[[], Awaitable[bytes]]
