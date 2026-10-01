"""Constants for the Global Player music provider."""

import aiohttp

API_BASE_URL = "https://bff-web-guacamole.musicradio.com"
BRANDS_URL = f"{API_BASE_URL}/globalplayer/brands"
PLAYABLE_URL = f"{API_BASE_URL}/playables/{{playable_id}}"

HEADERS = {
    "Accept": "application/vnd.global.8+json",
    "User-Agent": "MusicAssistant/1.0",
}

CACHE_CATEGORY_GLOBAL_PLAYER = "global_player"
CACHE_KEY_STATIONS = "stations"
CACHE_TTL_STATIONS = 86400  # 24 hours
API_TIMEOUT = aiohttp.ClientTimeout(total=10)
