"""Constants for Storytel Provider."""

# API URL TEMPLATES
URL_LOGIN = "https://www.storytel.com/api/login.action?m=1&uid={UID}&pwd={PASSWORD}"
URL_REVALIDATE = "https://www.storytel.com/api/v2/account/revalidation"
URL_LIBRARY_MANAGEMENT = "https://api.storytel.net/libraries/bookshelf"
URL_CONSUMABLE_DOWNLOAD_NO_RANGE = (
    "https://api.storytel.net/assets/v2/consumables/{CONSUMABLE_ID}/url"
)
URL_BOOKMARK_GET = (
    "https://api.storytel.net/bookmarks/positional?consumableIds={CONSUMABLE_ID}&version=22.43.0"
)
URL_BOOKMARK_SET = "https://api.storytel.net/bookmarks/positional?version=22.43.0"
URL_CONSUMABLE_DETAILS = "https://api.storytel.net/book-details/consumables/{CONSUMABLE_ID}"
URL_PLAYBACK_BOOK_DETAILS = "https://api.storytel.net/playback-metadata/consumable/{CONSUMABLE_ID}"
URL_SEARCH = "https://api.storytel.net/search/client"
URL_FRONTPAGE = "https://api.storytel.net/explore/frontpage/chips"
URL_PODCAST_DETAILS = "https://api.storytel.net/explore/lists/series/{CONSUMABLE_ID}"

# API ENCRYPTION KEYS
# Source: https://github.com/MauritsWilke/storytel-api/blob/v1_archive/src/utils/encryptPassword.ts
API_ENCRYPTION_KEY = b"VQZBJ6TD8M9WBUWT"  # 16 bytes, AES-128-CBC key
API_ENCRYPTION_IV = b"joiwef08u23j341a"  # 16 bytes, AES-128-CBC initialization vector

# API CONTENT-TYPE HEADERS
API_HEADER_CONTENT_TYPE_LIBRARY_DELTA = "application/vnd.storytel.library-delta+json;v=1.4"
API_HEADER_CONTENT_TYPE_BOOK_DETAILS = "application/vnd.storytel.bookdetails-v5+json"
API_HEADER_CONTENT_TYPE_EXPLORE = "application/vnd.storytel.explore-v21+json"
API_HEADER_CONTENT_TYPE_SEARCH = "application/vnd.storytel.search-v10+json"

# API STREAM HEADERS
API_HEADER_STORYTEL_MEDIA_ACCEPT = "storytel-media-accept"
API_HEADER_STORYTEL_MEDIA_FORMATS = "audio/mpeg,audio/mp4;codecs=mp4a.40.2,audio/mp4;codecs=ec-3"

# API RESOURCE VERSION
# Default resource version for library management requests
API_DEFAULT_RESOURCE_VERSION = (
    "AAUAAAaaaaaaaaaaaaaAAAAaaaaaaaAaAAaAAAaaaaaaaaAAAAaaaaaaAAAAAAaaaaa="
)


# Provider configuration keys
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_LANGUAGES = "languages"
CONF_KIDS_MODE = "kids_mode"


# LANGUAGES - Available languages for discovery features (recommendations, search)
# Format: {name: isoValue}
DEFAULT_LANGUAGES = {
    "English": "en",
}

ALL_LANGUAGES = {
    "Danish": "da",
    "English": "en",
    "Arabic": "ar",
    "Swedish": "sv",
    "Faroese": "fo",  # codespell:ignore fo
    "German": "de",
    "French": "fr",
    "Italian": "it",
    "Spanish": "es",
    "Dutch": "nl",
}

# Provider cache keys

CACHE_DOMAIN = "storytel"
CACHE_CATEGORY_API = 0
CACHE_CATEGORY_AUDIOBOOK = 1
CACHE_CATEGORY_CHAPTERS = 2
CACHE_CATEGORY_PODCAST = 3
CACHE_CATEGORY_PODCAST_EPISODES = 4
CACHE_CATEGORY_PODCAST_EPISODE = 5
