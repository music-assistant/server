"""
nicovideo services package.

Services Layer: API integration and data transformation coordination
Coordinates API calls through niconico.py, manages rate limiting, and delegates data transformation.
"""

from __future__ import annotations

from music_assistant.providers.nicovideo.services.hub import NicovideoServiceHub

__all__ = [
    "NicovideoServiceHub",
]
