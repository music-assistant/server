"""Constants for the Yandex Music provider."""

from __future__ import annotations

from typing import Final

# Configuration Keys
CONF_TOKEN = "token"
CONF_QUALITY = "quality"
CONF_BASE_URL = "base_url"

# Streaming mode config
CONF_STREAMING_MODE: Final[str] = "streaming_mode"
STREAMING_MODE_DIRECT = "direct"
STREAMING_MODE_BUFFERED = "buffered"
STREAMING_MODE_PRELOAD = "preload"

# Preload buffer limit (MB) before switching to tempfile
CONF_PRELOAD_BUFFER_MB: Final[str] = "preload_buffer_mb"

# Actions
CONF_ACTION_AUTH = "auth"
CONF_ACTION_CLEAR_AUTH = "clear_auth"

# Labels
LABEL_TOKEN = "token_label"
LABEL_AUTH_INSTRUCTIONS = "auth_instructions_label"

# API defaults
DEFAULT_LIMIT: Final[int] = 50
DEFAULT_BASE_URL: Final[str] = "https://api.music.yandex.net"

# Quality options (matching reference implementation)
QUALITY_EFFICIENT = "efficient"  # Low quality, efficient bandwidth (~64kbps AAC)
QUALITY_BALANCED = "balanced"  # Medium quality, balanced performance (~192kbps AAC)
QUALITY_HIGH = "high"  # High quality, lossy (~320kbps MP3)
QUALITY_SUPERB = "superb"  # Highest quality, lossless (FLAC)

# Configuration keys for My Wave behavior
CONF_MY_WAVE_MAX_TRACKS: Final[str] = "my_wave_max_tracks"
CONF_MY_WAVE_BATCH_SIZE: Final[str] = "my_wave_batch_size"
CONF_TRACK_BATCH_SIZE: Final[str] = "track_batch_size"
CONF_DISCOVERY_INITIAL_TRACKS: Final[str] = "discovery_initial_tracks"
CONF_BROWSE_INITIAL_TRACKS: Final[str] = "browse_initial_tracks"
CONF_ENABLE_RECOMMENDATIONS: Final[str] = "enable_recommendations"
CONF_ENABLE_MY_WAVE_BROWSE: Final[str] = "enable_my_wave_browse"
CONF_ENABLE_MY_WAVE_PLAYLIST: Final[str] = "enable_my_wave_playlist"
CONF_ENABLE_MY_WAVE_RADIO: Final[str] = "enable_my_wave_radio"

# Configuration keys for Liked Tracks behavior
CONF_LIKED_TRACKS_MAX_TRACKS: Final[str] = "liked_tracks_max_tracks"
CONF_ENABLE_LIKED_TRACKS_BROWSE: Final[str] = "enable_liked_tracks_browse"
CONF_ENABLE_LIKED_TRACKS_PLAYLIST: Final[str] = "enable_liked_tracks_playlist"

# Configuration keys for Discovery recommendations
CONF_ENABLE_FEED_RECOMMENDATIONS: Final[str] = "enable_feed_recommendations"
CONF_ENABLE_CHART: Final[str] = "enable_chart"
CONF_ENABLE_NEW_RELEASES: Final[str] = "enable_new_releases"
CONF_ENABLE_NEW_PLAYLISTS: Final[str] = "enable_new_playlists"

# Image sizes
IMAGE_SIZE_SMALL = "200x200"
IMAGE_SIZE_MEDIUM = "400x400"
IMAGE_SIZE_LARGE = "1000x1000"

# ID separators
PLAYLIST_ID_SPLITTER: Final[str] = ":"

# Rotor (radio) station identifiers
ROTOR_STATION_MY_WAVE: Final[str] = "user:onyourwave"

# Virtual playlist ID for My Wave (used in get_playlist / get_playlist_tracks; not owner_id:kind)
MY_WAVE_PLAYLIST_ID: Final[str] = "my_wave"

# Virtual playlist ID for Liked Tracks
LIKED_TRACKS_PLAYLIST_ID: Final[str] = "liked_tracks"

# Composite item_id for My Wave tracks: track_id + separator + station_id (for rotor feedback)
RADIO_TRACK_ID_SEP: Final[str] = "@"

# Browse folder names by locale (item_id -> display name)
BROWSE_NAMES_RU: Final[dict[str, str]] = {
    "my_wave": "Моя волна",
    "artists": "Мои исполнители",
    "albums": "Мои альбомы",
    "tracks": "Мне нравится",
    "playlists": "Мои плейлисты",
    "feed": "Для вас",
    "chart": "Чарт",
    "new_releases": "Новинки",
    "new_playlists": "Новые плейлисты",
}
BROWSE_NAMES_EN: Final[dict[str, str]] = {
    "my_wave": "My Wave",
    "artists": "My Artists",
    "albums": "My Albums",
    "tracks": "My Favorites",
    "playlists": "My Playlists",
    "feed": "Made for You",
    "chart": "Chart",
    "new_releases": "New Releases",
    "new_playlists": "New Playlists",
}
