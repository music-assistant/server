"""Constants for the VK Music provider."""

from __future__ import annotations

from typing import Final

# Configuration Keys
CONF_TOKEN: Final[str] = "token"
CONF_USER_AGENT: Final[str] = "user_agent"

# Actions
CONF_ACTION_CLEAR_AUTH: Final[str] = "clear_auth"

# Labels
LABEL_TOKEN: Final[str] = "token_label"
LABEL_AUTH_INSTRUCTIONS: Final[str] = "auth_instructions_label"

# API defaults
DEFAULT_LIMIT: Final[int] = 50
PLAYLIST_TRACKS_PAGE_SIZE: Final[int] = 50

# ID separators
TRACK_ID_SPLITTER: Final[str] = "_"
PLAYLIST_ID_SPLITTER: Final[str] = "_"

# VK URLs
VK_MUSIC_BASE_URL: Final[str] = "https://vk.com/music"

# Default user agent
DEFAULT_USER_AGENT: Final[str] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
