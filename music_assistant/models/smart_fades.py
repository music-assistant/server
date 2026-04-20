"""Data models for Smart Fades configuration."""

from enum import StrEnum


class SmartFadesMode(StrEnum):
    """Smart fades modes."""

    SMART_CROSSFADE = "smart_crossfade"  # Use smart crossfade with beat matching and EQ filters
    STANDARD_CROSSFADE = "standard_crossfade"  # Use standard crossfade only
    DISABLED = "disabled"  # No crossfade
