"""Helper functions for the VBAN receiver provider."""

from __future__ import annotations

import re

from music_assistant_models.enums import ContentType


def get_supported_pcm_formats() -> dict[str, int]:
    """Return supported PCM formats."""
    pcm_formats = {}
    for content_type in ContentType.__members__:
        if match := re.match(r"PCM_([SF](\d{2})LE)", content_type):
            pcm_formats[match.group(1)] = int(match.group(2))
    return pcm_formats
