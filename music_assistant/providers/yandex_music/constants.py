"""Constants for the Yandex Music provider."""

from __future__ import annotations

from typing import Final

# Configuration Keys
CONF_TOKEN = "token"
CONF_QUALITY = "quality"

# Actions
CONF_ACTION_AUTH = "auth"
CONF_ACTION_CLEAR_AUTH = "clear_auth"

# Labels
LABEL_TOKEN = "token_label"
LABEL_AUTH_INSTRUCTIONS = "auth_instructions_label"

# API defaults
DEFAULT_LIMIT: Final[int] = 50

# Quality options
QUALITY_HIGH = "high"
QUALITY_LOSSLESS = "lossless"

# Image sizes
IMAGE_SIZE_SMALL = "200x200"
IMAGE_SIZE_MEDIUM = "400x400"
IMAGE_SIZE_LARGE = "1000x1000"

# Cache TTL (in seconds)
CACHE_TTL_ARTIST: Final[int] = 3600 * 24 * 7  # 7 days
CACHE_TTL_ALBUM: Final[int] = 3600 * 24 * 30  # 30 days
CACHE_TTL_PLAYLIST: Final[int] = 3600 * 3  # 3 hours
