"""Music controller package."""

from __future__ import annotations

from .constants import RADIO_TRACK_MAX_DURATION_SECS
from .controller import MusicController

__all__ = [
    "RADIO_TRACK_MAX_DURATION_SECS",
    "MusicController",
]
