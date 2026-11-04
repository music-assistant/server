"""Playback backends for the Spotify music provider."""

from .base import SpotifyPlaybackBackend, StreamSupersededError
from .librespot import LibrespotBackend
from .soloist import SoloistAccountBusyError, SoloistBackend

__all__ = [
    "LibrespotBackend",
    "SoloistAccountBusyError",
    "SoloistBackend",
    "SpotifyPlaybackBackend",
    "StreamSupersededError",
]
