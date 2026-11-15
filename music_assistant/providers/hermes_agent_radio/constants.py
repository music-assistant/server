"""Constants for the Hermes Agent Radio provider."""

from typing import Final

import aiohttp

BASE_URL: Final[str] = "https://hermesagentradio.com"
# the open feed listing every station and its mount
STATIONS_URL: Final[str] = f"{BASE_URL}/stations.json"
# the now-playing feed, asked per mount with ?station=<id>
NOW_PLAYING_URL: Final[str] = f"{BASE_URL}/now-playing.json"

# the lineup can gain genre mounts; an hourly refresh is plenty
STATIONS_CACHE_SECONDS: Final[int] = 3600
# the now-playing item is re-read this often while a station plays
STREAM_METADATA_UPDATE_INTERVAL: Final[int] = 60

# every mount broadcasts the same MP3 quality
STREAM_BIT_RATE: Final[int] = 128

# bound each request; the shared session's default timeout is too long for a metadata fetch
API_TIMEOUT: Final[aiohttp.ClientTimeout] = aiohttp.ClientTimeout(total=10)
API_RATE_LIMIT: Final[int] = 1
API_RATE_PERIOD: Final[float] = 5.0
