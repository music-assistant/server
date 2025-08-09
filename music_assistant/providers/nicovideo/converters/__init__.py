"""
Nicovideo converters module.

This module contains converter classes to transform nicovideo objects
into Music Assistant media items using an adapter pattern.
"""

from __future__ import annotations

from music_assistant.providers.nicovideo.converters.hub import (
    NicovideoConverterHub,
)

__all__ = ["NicovideoConverterHub"]
