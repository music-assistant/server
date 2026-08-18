"""Normalized models shared between the Spotify Connect provider and its backends."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum


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
    # the backend lost its Spotify connection (e.g. daemon exit) and will recover
    # on its own; any session/playback state is gone until a new SESSION_ACTIVE
    CONNECTION_LOST = "connection_lost"
    # the backend failed permanently and the provider must unload with an error
    FATAL_ERROR = "fatal_error"
    # any other backend activity; carries at most refreshed context/track uris
    OTHER = "other"


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


@dataclass(slots=True)
class BackendEvent:
    """
    A single normalized event emitted by a backend to the provider.

    ``context_uri`` / ``track_uri`` piggyback on every event type: they carry
    the latest context/track seen by the backend so the provider can take
    playback back after the user moved the active device away. ``position`` is
    the elapsed time in seconds (POSITION events), ``volume`` a 0-100
    percentage (VOLUME events) and ``error`` the failure description
    (FATAL_ERROR events).
    """

    type: BackendEventType
    context_uri: str | None = None
    track_uri: str | None = None
    metadata: BackendTrackMetadata | None = None
    position: int | None = None
    volume: int | None = None
    error: str | None = None


# Awaited by the backend for every normalized event, in emit order.
BackendEventCallback = Callable[[BackendEvent], Awaitable[None]]

# Reads the next chunk of decoded PCM; returns b"" once the audio pipe closes.
AudioChunkReader = Callable[[], Awaitable[bytes]]
