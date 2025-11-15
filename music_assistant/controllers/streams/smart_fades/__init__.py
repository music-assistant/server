"""Smart Fades - Audio analyzer and mixer."""

from __future__ import annotations

# Logger name constant (must be defined before imports to avoid circular dependencies)
SMART_FADES_LOGGER_NAME = "music_assistant.streams.smart_fades"

from .analyzer import SmartFadesAnalyzer  # noqa: E402
from .mixer import SmartFadesMixer  # noqa: E402

__all__ = ["SMART_FADES_LOGGER_NAME", "SmartFadesAnalyzer", "SmartFadesMixer"]
