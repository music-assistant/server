"""Constants for the Tidal music provider."""

# API URLs
from typing import Final

BASE_URL = "https://api.tidal.com/v1"
BASE_URL_V2 = "https://api.tidal.com/v2"
OPEN_API_URL = "https://openapi.tidal.com/v2"
BROWSE_URL = "https://tidal.com/browse"
RESOURCES_URL = "https://resources.tidal.com/images"
# Base host for the /pages/* feed.
WEB_BASE_URL = "https://tidal.com/v1"
SESSIONS_URL = f"{BASE_URL}/sessions"

# Authentication
TOKEN_TYPE = "Bearer"
AUTH_URL = "https://auth.tidal.com/v1/oauth2"
LOGIN_URL = "https://login.tidal.com/authorize"
REDIRECT_URI = "https://tidal.com/android/login/auth"

# API paths (relative to BASE_URL unless used with an explicit base_url)
PLAYLISTS = "playlists"
PAGES_MIX = "pages/mix"
FAVORITES_ARTISTS = "favorites/artists"
FAVORITES_ALBUMS = "favorites/albums"
FAVORITES_TRACKS = "favorites/tracks"
FAVORITES_PLAYLISTS = "favorites/playlists"
FAVORITES_MIXES = "favorites/mixes"

# Setup flow: key of the pasted post-login ("Page Not Found") redirect URL
CONF_OOPS_URL = "oops_url"

# Config keys
CONF_AUTH_TOKEN = "auth_token"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_USER_ID = "user_id"
CONF_EXPIRY_TIME = "expiry_time"
CONF_COUNTRY_CODE = "country_code"
CONF_SESSION_ID = "session_id"
CONF_QUALITY = "quality"

# API defaults
DEFAULT_LIMIT: Final[int] = 50

# Cache keys
CACHE_CATEGORY_DEFAULT: Final[int] = 0
CACHE_CATEGORY_RECOMMENDATIONS: Final[int] = 1
CACHE_CATEGORY_ISRC_MAP: Final[int] = 2

# Virtual playlist IDs
FAVORITE_TRACKS_PLAYLIST_ID: Final[str] = "favorite_tracks"
