"""Constants for Last.fm Recommendations Provider."""

from __future__ import annotations

from typing import Final

CONF_API_KEY: Final[str] = "api_key"
CONF_ENABLE_PERSONALIZED: Final[str] = "enable_personalized"
CONF_ENABLE_GLOBAL_CHARTS: Final[str] = "enable_global_charts"
CONF_ENABLE_GENRE: Final[str] = "enable_genre"
CONF_ENABLE_GEO: Final[str] = "enable_geo"
CONF_GEO_COUNTRY: Final[str] = "geo_country"
CONF_ACTION_CLEAR_CACHE: Final[str] = "clear_cache"
REFRESH_TASK_ID: Final[str] = "lastfm_recommendations_refresh"

# Cache settings
# Cache category for resolved Artist/Album/Track objects
CACHE_CATEGORY_RESOLVED_ITEMS = 1

# Expiration time for cached resolved items (in seconds)
CACHE_EXPIRATION_SECONDS = 60 * 60 * 24 * 90  # 90 days

# Concurrency limits
# Maximum number of concurrent provider searches to prevent overwhelming APIs
SEARCH_CONCURRENCY_LIMIT = 5

# Maximum number of concurrent MusicBrainz ISRC lookups. MusicBrainz throttles all
# callers to a shared global budget, so a wide fan-out just queues behind it while
# bursting past the mirror's edge limit; cap it to stay within budget with headroom.
MB_ISRC_CONCURRENCY_LIMIT = 5

# Item counts and limits
# Target number of items to return in recommendation folders
TARGET_ITEM_COUNT = 10

# Number of items to fetch when we expect some resolution failures (small buffer)
RESOLUTION_BUFFER_SMALL = 15

# Genre chart pool (single fetch); sized so the hourly rotation surfaces more of the genre per day
RESOLUTION_BUFFER_LARGE = 60

# Number of top items to always include when sampling (before random selection)
TOP_ITEMS_TO_TAKE = 3

# Number of similar items to fetch before filtering to target count
SIMILAR_ITEMS_BUFFER = 12

# Number of similar items to fetch for each seed artist/track
SIMILAR_ITEMS_PER_SEED = 5

# Number of top artists to use as seeds for personalized recommendations
TOP_ARTISTS_LIMIT = 5

# Number of recently played tracks to scan when ranking top artists by appearances
RECENT_TRACKS_SCAN_LIMIT = 200

# Number of top tracks fetched as seeds; over-fetched so enough are recognised by Last.fm
TOP_TRACKS_LIMIT = 10

# Number of top genre tags fetched; the genre rows cycle through them daily
TOP_TAGS_LIMIT = 3

# API search settings
# Search limit for provider API calls (workaround for Spotify API bug with limit=1)
PROVIDER_SEARCH_LIMIT = 2

# Curated list of popular countries for Last.fm geo charts.
# Last.fm API expects full country names (not ISO codes).
# Covers major music markets and can be expanded based on user requests.
GEO_COUNTRIES = [
    "Argentina",
    "Australia",
    "Austria",
    "Belgium",
    "Brazil",
    "Canada",
    "China",
    "Czech Republic",
    "Denmark",
    "Finland",
    "France",
    "Germany",
    "Greece",
    "Hungary",
    "Iceland",
    "India",
    "Ireland",
    "Israel",
    "Italy",
    "Japan",
    "Lithuania",
    "Mexico",
    "Netherlands",
    "New Zealand",
    "Norway",
    "Philippines",
    "Poland",
    "Portugal",
    "Serbia",
    "Singapore",
    "Slovenia",
    "South Africa",
    "South Korea",
    "Spain",
    "Sweden",
    "Switzerland",
    "Thailand",
    "Turkey",
    "Ukraine",
    "United Arab Emirates",
    "United Kingdom",
    "United States",
]
