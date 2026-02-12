"""Constants for the Zvuk Music provider."""

from __future__ import annotations

from typing import Final

# Configuration Keys
CONF_TOKEN: Final[str] = "token"
CONF_QUALITY: Final[str] = "quality"

# Actions
CONF_ACTION_CLEAR_AUTH: Final[str] = "clear_auth"

# API defaults
DEFAULT_LIMIT: Final[int] = 50
PLAYLIST_TRACKS_PAGE_SIZE: Final[int] = 50

# Quality options
QUALITY_HIGH: Final[str] = "high"
QUALITY_LOSSLESS: Final[str] = "lossless"

# Image sizes
IMAGE_SIZE_LARGE: Final[int] = 600

# URLs
ZVUK_BASE_URL: Final[str] = "https://zvuk.com"
