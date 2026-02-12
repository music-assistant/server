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

# Configuration keys for My Mix behavior
CONF_MY_MIX_MAX_TRACKS: Final[str] = "my_mix_max_tracks"
CONF_MY_MIX_BATCH_SIZE: Final[str] = "my_mix_batch_size"
CONF_TRACK_BATCH_SIZE: Final[str] = "track_batch_size"
CONF_DISCOVERY_INITIAL_TRACKS: Final[str] = "discovery_initial_tracks"
CONF_BROWSE_INITIAL_TRACKS: Final[str] = "browse_initial_tracks"
CONF_ENABLE_RECOMMENDATIONS: Final[str] = "enable_recommendations"
CONF_ENABLE_MY_MIX_BROWSE: Final[str] = "enable_my_mix_browse"
CONF_ENABLE_MY_MIX_PLAYLIST: Final[str] = "enable_my_mix_playlist"
CONF_ENABLE_MY_MIX_RADIO: Final[str] = "enable_my_mix_radio"

# Configuration keys for Liked Tracks behavior
CONF_LIKED_TRACKS_MAX_TRACKS: Final[str] = "liked_tracks_max_tracks"
CONF_ENABLE_LIKED_TRACKS_BROWSE: Final[str] = "enable_liked_tracks_browse"
CONF_ENABLE_LIKED_TRACKS_PLAYLIST: Final[str] = "enable_liked_tracks_playlist"

# Image sizes
IMAGE_SIZE_SMALL = "200x200"
IMAGE_SIZE_MEDIUM = "400x400"
IMAGE_SIZE_LARGE = "1000x1000"

# ID separators
PLAYLIST_ID_SPLITTER: Final[str] = ":"

# Rotor (radio) station identifiers
ROTOR_STATION_MY_MIX: Final[str] = "user:onyourwave"

# Virtual playlist ID for My Mix (used in get_playlist / get_playlist_tracks; not owner_id:kind)
MY_MIX_PLAYLIST_ID: Final[str] = "my_mix"

# Virtual playlist ID for Liked Tracks
LIKED_TRACKS_PLAYLIST_ID: Final[str] = "liked_tracks"

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
