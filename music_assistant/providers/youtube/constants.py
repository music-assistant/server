"""Constants for the YouTube provider."""

from music_assistant_models.enums import ProviderFeature

CONF_API_KEY = "api_key"
CONF_COOKIES = "cookies"
CONF_PLAYLIST_LIMIT = "playlist_limit"

DEFAULT_PLAYLIST_LIMIT = 25

YT_DATA_API_BASE = "https://www.googleapis.com/youtube/v3"

YT_DOMAIN = "https://www.youtube.com"

# How long before a signed stream URL expires (in seconds). YouTube stream URLs
# carry an `expire` query parameter; we fall back to this default when it cannot
# be parsed from the URL.
DEFAULT_STREAM_URL_EXPIRATION = 3600

# How long before a live stream URL expires (shorter since HLS manifests refresh).
DEFAULT_LIVE_STREAM_URL_EXPIRATION = 300

# yt-dlp live_status values that indicate an active live stream.
LIVE_STATUSES = {"is_live", "is_upcoming"}

SUPPORTED_FEATURES = {
    ProviderFeature.SEARCH,
    ProviderFeature.ARTIST_ALBUMS,
    ProviderFeature.ARTIST_TOPTRACKS,
}
