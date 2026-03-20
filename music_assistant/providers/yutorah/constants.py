"""Constants for the YuTorah music provider."""

from music_assistant_models.enums import ProviderFeature

API_BASE = "https://yutorah.org/api/"
YUTORAH_BASE = "https://www.yutorah.org"

# Headers that mirror the official YuTorah Android app
API_HEADERS = {
    "Accept": "application/json",
    "os": "android",
    "app-version": "1.3.4",
    "os-version": "30",
    "User-Agent": "YuTorah/1.3.4 (Android 11)",
}

SUPPORTED_FEATURES = {
    ProviderFeature.BROWSE,
    ProviderFeature.SEARCH,
    ProviderFeature.ARTIST_TOPTRACKS,
}

# search/get returns 30 results per page; start is a 1-based page number
PAGE_SIZE = 30
MAX_EPISODES = 500
