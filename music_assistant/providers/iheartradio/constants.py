"""Constants for the iHeartRadio provider."""

from __future__ import annotations

from typing import Final

import aiohttp

# -- Config keys --

CONF_COUNTRY: Final[str] = "country"

# -- Regions --

# The hostname shard decides which catalogue is served: a station only resolves on the
# shard of the country it broadcasts in.
DEFAULT_COUNTRY: Final[str] = "us"
API_BASE_URLS: Final[dict[str, str]] = {
    "us": "https://api.iheart.com",
    "ca": "https://ca.api.iheart.com",
    "au": "https://au.api.iheart.com",
    "nz": "https://nz.api.iheart.com",
    "mx": "https://mx.api.iheart.com",
}

# The API validates this header against its own client list and answers 400 for a value
# it does not know, so the country must match the shard being addressed.
HEADER_HOST_NAME: Final[str] = "X-hostName"
HEADER_LOCALE: Final[str] = "X-Locale"
API_LOCALE: Final[str] = "en-US"
# Per-request timeout for API calls.
API_TIMEOUT: Final[aiohttp.ClientTimeout] = aiohttp.ClientTimeout(total=20)

# -- Endpoints --

PATH_LIVE_STATIONS: Final[str] = "/api/v2/content/liveStations"
PATH_LIVE_STATION: Final[str] = "/api/v2/content/liveStations/{station_id}"
PATH_MARKETS: Final[str] = "/api/v2/content/markets"
PATH_GENRES: Final[str] = "/api/v3/catalog/genres"
PATH_NOW_PLAYING: Final[str] = "/api/v3/live-meta/stream/{station_id}/currentTrackMeta"
PATH_PODCAST_CATEGORIES: Final[str] = "/api/v3/podcast/categories"
PATH_PODCAST_CATEGORY: Final[str] = "/api/v3/podcast/categories/{category_id}"
PATH_PODCAST: Final[str] = "/api/v3/podcast/podcasts/{podcast_id}"
PATH_PODCAST_EPISODES: Final[str] = "/api/v3/podcast/podcasts/{podcast_id}/episodes"
PATH_PODCAST_EPISODE: Final[str] = "/api/v3/podcast/episodes/{episode_id}"
PATH_SEARCH: Final[str] = "/api/v3/search/all"

# -- Streaming --

# Preference order for the station's stream variants: HLS is what iHeart's own web player
# uses, Shoutcast is the plain fallback every station has, and a .pls playlist (US only)
# is unwrapped by the streams controller.
STREAM_PREFERENCE: Final[tuple[str, ...]] = (
    "secure_hls_stream",
    "secure_shoutcast_stream",
    "secure_pls_stream",
    "hls_stream",
    "shoutcast_stream",
    "pls_stream",
)

# How often MA asks us for fresh now-playing data while a station is playing.
STREAM_METADATA_UPDATE_INTERVAL: Final[int] = 15

# -- Item ids --

# A podcast episode is looked up on its own endpoint, but a PodcastEpisode must name its
# parent podcast, so the episode's MA id carries both.
ID_SEPARATOR: Final[str] = ":"

# -- Paging --

# The largest page the API serves; a country's full market list fits in one.
STATION_PAGE_LIMIT: Final[int] = 500
MARKET_PAGE_LIMIT: Final[int] = 500
EPISODE_PAGE_LIMIT: Final[int] = 100
# Podcasts with a decade of episodes exist; cap the walk so listing one cannot fire an
# unbounded number of requests. The newest episodes are listed first, so a capped listing
# drops the oldest.
MAX_EPISODE_PAGES: Final[int] = 5

# -- Caching --

CACHE_CATEGORY_STATIONS: Final[int] = 0
CACHE_CATEGORY_PODCASTS: Final[int] = 1
CACHE_CATEGORY_CATALOG: Final[int] = 2
CACHE_CATEGORY_SEARCH: Final[int] = 3

CACHE_TTL_STATION: Final[int] = 3600 * 6
# Markets, genres and podcast categories change a few times a year at most.
CACHE_TTL_CATALOG: Final[int] = 3600 * 24 * 7
CACHE_TTL_PODCAST: Final[int] = 3600 * 24
CACHE_TTL_EPISODES: Final[int] = 3600
CACHE_TTL_SEARCH: Final[int] = 3600

# -- Browse paths --

BROWSE_LIVE: Final[str] = "live"
BROWSE_MARKETS: Final[str] = "markets"
BROWSE_GENRES: Final[str] = "genres"
BROWSE_PODCASTS: Final[str] = "podcasts"
