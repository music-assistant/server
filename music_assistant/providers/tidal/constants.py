"""Constants for the Tidal music provider."""

# API URLs
from typing import Final

BASE_URL = "https://api.tidal.com/v1"
OPEN_API_URL = "https://openapi.tidal.com/v2"
BROWSE_URL = "https://tidal.com/browse"
RESOURCES_URL = "https://resources.tidal.com/images"
# Base host for the /pages/* feed.
WEB_BASE_URL = "https://tidal.com/v1"
SESSIONS_URL = f"{BASE_URL}/sessions"

# Official API (JSON:API)
JSONAPI_CONTENT_TYPE = "application/vnd.api+json"

# Authentication
AUTH_URL = "https://auth.tidal.com/v1/oauth2"
# OAuth scopes requested for the device flow (read, write, subscription).
AUTH_SCOPE = "r_usr w_usr w_sub"

# API paths (relative to BASE_URL unless used with an explicit base_url)
PLAYLISTS = "playlists"
PAGES_MIX = "pages/mix"

# Config keys
CONF_AUTH_TOKEN = "auth_token"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_USER_ID = "user_id"
CONF_EXPIRY_TIME = "expiry_time"
CONF_QUALITY = "quality"

# Cache keys
CACHE_CATEGORY_RECOMMENDATIONS: Final[int] = 1
CACHE_CATEGORY_ISRC_MAP: Final[int] = 2

# Virtual playlist IDs
FAVORITE_TRACKS_PLAYLIST_ID: Final[str] = "favorite_tracks"
