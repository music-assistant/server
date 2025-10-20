"""Constants for the Tidal music provider."""

# API URLs
from typing import Final

BASE_URL = "https://api.tidal.com/v1"
BASE_URL_V2 = "https://api.tidal.com/v2"
OPEN_API_URL = "https://openapi.tidal.com/v2"
BROWSE_URL = "https://tidal.com/browse"
RESOURCES_URL = "https://resources.tidal.com/images"

# Authentication
TOKEN_TYPE = "Bearer"

# Actions
CONF_ACTION_START_PKCE_LOGIN = "start_pkce_login"
CONF_ACTION_COMPLETE_PKCE_LOGIN = "auth"
CONF_ACTION_CLEAR_AUTH = "clear_auth"

# Intermediate steps
CONF_TEMP_SESSION = "temp_session"
CONF_OOPS_URL = "oops_url"

# Config keys
CONF_AUTH_TOKEN = "auth_token"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_USER_ID = "user_id"
CONF_EXPIRY_TIME = "expiry_time"
CONF_COUNTRY_CODE = "country_code"
CONF_SESSION_ID = "session_id"
CONF_QUALITY = "quality"

# Labels
LABEL_START_PKCE_LOGIN = "start_pkce_login_label"
LABEL_OOPS_URL = "oops_url_label"
LABEL_COMPLETE_PKCE_LOGIN = "complete_pkce_login_label"

# API defaults
DEFAULT_LIMIT: Final[int] = 50

# Cache keys
CACHE_CATEGORY_DEFAULT: Final[int] = 0
CACHE_CATEGORY_RECOMMENDATIONS: Final[int] = 1
CACHE_CATEGORY_ISRC_MAP: Final[int] = 2
