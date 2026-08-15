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

# Cache category for the user's derived top genres
CACHE_CATEGORY_TOP_GENRES = 2

# Expiration time for cached resolved items (in seconds)
CACHE_EXPIRATION_SECONDS = 60 * 60 * 24 * 90  # 90 days

# Expiration for the derived top genres; genre identity is stable and the genre rows only
# rotate daily, so a daily recompute is plenty and keeps the per-artist tag fan-out cheap.
TOP_GENRES_CACHE_EXPIRATION_SECONDS = 60 * 60 * 24  # 1 day

# Concurrency limits
# Maximum number of concurrent provider searches to prevent overwhelming APIs
SEARCH_CONCURRENCY_LIMIT = 5

# Item counts and limits
# Target number of items to return in recommendation folders
TARGET_ITEM_COUNT = 10

# Number of items to fetch when we expect some resolution failures (small buffer)
RESOLUTION_BUFFER_SMALL = 15

# Genre chart pool (single fetch); sized so the hourly rotation surfaces more of the genre per day
RESOLUTION_BUFFER_LARGE = 60

# Number of top items to always include when sampling (before random selection)
TOP_ITEMS_TO_TAKE = 3

# Number of library search hits scanned when verifying an item is already in the library.
# A small window so a genuine match ranked behind a fuzzier one is still caught.
LIBRARY_MATCH_SCAN_LIMIT = 5

# Number of similar artists to resolve before filtering to target count. Over-fetched above the
# target to absorb resolution misses and artists excluded because already in the library.
SIMILAR_ITEMS_BUFFER = 18

# Number of similar tracks to resolve before filtering to target count. Larger than the
# artist buffer to absorb this row's extra attrition: artist+title verification and the
# exclusion of tracks that resolve to the user's own library copy.
SIMILAR_TRACKS_BUFFER = 15

# Number of similar items to fetch for each seed artist/track
SIMILAR_ITEMS_PER_SEED = 5

# Number of top artists to use as seeds for personalized recommendations
TOP_ARTISTS_LIMIT = 5

# Number of recent tracks used as similar-track seeds; kept high so a single album playthrough
# doesn't fill the whole seed set, and so enough seeds are recognised by Last.fm
TOP_TRACKS_LIMIT = 20

# Recent-listening window (days) for personalized seeds. Seeds are drawn from plays in this
# window so they track current listening; the playlog is pruned at 365 days.
RECENT_PLAYS_WINDOW_DAYS = 30

# Maximum number of recent play events to scan when ranking seeds.
RECENT_PLAYS_SCAN_LIMIT = 200

# Number of derived genres; the genre rows cycle through them daily
TOP_GENRES_LIMIT = 3

# Genres are derived from the community tags of the user's most played artists. Last.fm has no
# genre data of its own, so an artist's top tags stand in for its genre.
# Number of the user's top artists to analyse, and the time span to rank them over.
GENRE_ARTISTS_LIMIT = 25
GENRE_ARTISTS_PERIOD = "overall"

# Number of top tags to take from each artist. Kept small because an artist's leading tags are
# almost always its genre; folksonomy noise ("seen live", "favourites") sits further down.
TAGS_PER_ARTIST = 5

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
