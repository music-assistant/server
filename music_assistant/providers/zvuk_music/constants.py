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
# Max tracks to fetch when reading a playlist for modification (e.g. track removal).
# The Zvuk API has no remove-by-ID endpoint, so we must fetch all tracks and re-upload.
PLAYLIST_TRACK_FETCH_LIMIT: Final[int] = 10_000

# Quality options
QUALITY_HIGH: Final[str] = "high"
QUALITY_LOSSLESS: Final[str] = "lossless"

# Image sizes
IMAGE_SIZE_LARGE: Final[int] = 600

# URLs
ZVUK_BASE_URL: Final[str] = "https://zvuk.com"

# Synthesis (personalized) playlist IDs — «Плейлисты для вас» on the home page.
# These AI-generated playlists are identified by low fixed IDs, stable per account.
SYNTHESIS_PLAYLIST_IDS: Final[tuple[int, ...]] = (3, 4, 6, 11, 12, 13, 14, 15)
