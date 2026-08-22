"""Playback backends for the Spotify music provider."""

from .base import SpotifyPlaybackBackend
from .librespot import LibrespotBackend
from .soloist import SoloistBackend

__all__ = [
    "LibrespotBackend",
    "SoloistBackend",
    "SpotifyPlaybackBackend",
]
