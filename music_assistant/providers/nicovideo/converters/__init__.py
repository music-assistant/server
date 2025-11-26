"""
Nicovideo converters module.

Converters Layer: Data transformation
Transforms nicovideo objects into Music Assistant media items using an adapter pattern.
Handles metadata mapping, normalization, and cross-references between items.
"""

from __future__ import annotations

from music_assistant.providers.nicovideo.converters.manager import (
    NicovideoConverterManager,
)

__all__ = ["NicovideoConverterManager"]
