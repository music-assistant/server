"""Smart Fades - Audio analyzer and mixer."""

from __future__ import annotations

import logging

# Define LOGGER first to avoid circular import issues
LOGGER = logging.getLogger(__name__)

from .analyzer import SmartFadesAnalyzer  # noqa: E402
from .mixer import SmartFadesMixer  # noqa: E402

__all__ = ["LOGGER", "SmartFadesAnalyzer", "SmartFadesMixer"]
