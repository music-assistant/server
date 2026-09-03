"""Constants for Podcast Index provider."""

# Configuration keys
CONF_API_KEY = "api_key"
CONF_API_SECRET = "api_secret"
CONF_STORED_PODCASTS = "stored_podcasts"

# API settings
API_BASE_URL = "https://api.podcastindex.org/api/1.0"

# Browse categories
BROWSE_TRENDING = "trending"
BROWSE_RECENT = "recent"
BROWSE_CATEGORIES = "categories"

HTTP_STATUS_ERROR = 400
HTTP_STATUS_UNAUTHORIZED = 401
# an error body is quoted back to the user, so cap it at a readable length
MAX_ERROR_DETAIL_LENGTH = 200
