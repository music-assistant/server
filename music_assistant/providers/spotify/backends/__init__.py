"""Playback backends for the Spotify music provider."""

from .base import SpotifyPlaybackBackend
from .librespot import LibrespotBackend
from .soloist import SoloistSingleTrackBackend

__all__ = [
    "LibrespotBackend",
    "SoloistSingleTrackBackend",
    "SpotifyPlaybackBackend",
]
