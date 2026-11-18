"""Constants for the Global Player music provider."""

import aiohttp

API_BASE_URL = "https://bff-web-guacamole.musicradio.com"
BRANDS_URL = f"{API_BASE_URL}/globalplayer/brands"
PLAYABLE_URL = f"{API_BASE_URL}/playables/{{playable_id}}"

HEADERS = {
    "Accept": "application/vnd.global.8+json",
}

CACHE_TTL_STATIONS = 86400  # 24 hours
CACHE_TTL_PLAYABLE = 1800  # 30 minutes
API_TIMEOUT = aiohttp.ClientTimeout(total=10)
