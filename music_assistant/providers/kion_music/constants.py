"""Constants for the KION Music provider."""

from __future__ import annotations

from typing import Final

# Configuration Keys
CONF_TOKEN = "token"
CONF_QUALITY = "quality"
CONF_BASE_URL = "base_url"

# Actions
CONF_ACTION_AUTH = "auth"
CONF_ACTION_CLEAR_AUTH = "clear_auth"

# Labels
LABEL_TOKEN = "token_label"
LABEL_AUTH_INSTRUCTIONS = "auth_instructions_label"

# API defaults
DEFAULT_LIMIT: Final[int] = 50
DEFAULT_BASE_URL: Final[str] = "https://music.mts.ru/ya_proxy_api"

# Quality options
QUALITY_HIGH = "high"
QUALITY_LOSSLESS = "lossless"

# Default tuning values for My Mix / browse / discovery behaviour
MY_MIX_MAX_TRACKS: Final[int] = 150
MY_MIX_BATCH_SIZE: Final[int] = 3
TRACK_BATCH_SIZE: Final[int] = 50
DISCOVERY_INITIAL_TRACKS: Final[int] = 20
BROWSE_INITIAL_TRACKS: Final[int] = 15

# Image sizes
IMAGE_SIZE_SMALL = "200x200"
IMAGE_SIZE_MEDIUM = "400x400"
IMAGE_SIZE_LARGE = "1000x1000"

# ID separators
PLAYLIST_ID_SPLITTER: Final[str] = ":"

# Rotor (radio) station identifiers
ROTOR_STATION_MY_MIX: Final[str] = "user:onyourwave"

# Client identifier for rotor radioStarted feedback.
# The API expects a "from" field identifying the client; the desktop app
# identifier ensures the rotor API returns proper recommendations.
ROTOR_FEEDBACK_FROM: Final[str] = "YandexMusicDesktopAppWindows"

# Virtual playlist ID for My Mix (used in get_playlist / get_playlist_tracks; not owner_id:kind)
MY_MIX_PLAYLIST_ID: Final[str] = "my_mix"

# Composite item_id for My Mix tracks: track_id + separator + station_id (for rotor feedback)
RADIO_TRACK_ID_SEP: Final[str] = "@"

# Browse folder names by locale (item_id -> display name)
BROWSE_NAMES_RU: Final[dict[str, str]] = {
    "my_mix": "Мой Микс",
    "artists": "Мои исполнители",
    "albums": "Мои альбомы",
    "tracks": "Мне нравится",
    "playlists": "Мои плейлисты",
}
BROWSE_NAMES_EN: Final[dict[str, str]] = {
    "my_mix": "My Mix",
    "artists": "My Artists",
    "albums": "My Albums",
    "tracks": "My Favorites",
    "playlists": "My Playlists",
}
