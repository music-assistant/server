"""Constants for Last.fm Recommendations Provider."""

from __future__ import annotations

# Cache settings
# Cache category for resolved Artist/Album/Track objects
CACHE_CATEGORY_RESOLVED_ITEMS = 1

# Expiration time for cached resolved items (in seconds)
CACHE_EXPIRATION_SECONDS = 60 * 60 * 24 * 90  # 90 days

# Concurrency limits
# Maximum number of concurrent provider searches to prevent overwhelming APIs
SEARCH_CONCURRENCY_LIMIT = 5

# Item counts and limits
# Target number of items to return in recommendation folders
TARGET_ITEM_COUNT = 10

# Number of items to fetch when we expect some resolution failures (small buffer)
RESOLUTION_BUFFER_SMALL = 15

# Number of items to fetch when we expect many resolution failures (large buffer)
RESOLUTION_BUFFER_LARGE = 30

# Number of top items to always include when sampling (before random selection)
TOP_ITEMS_TO_TAKE = 3

# Number of similar items to fetch before filtering to target count
SIMILAR_ITEMS_BUFFER = 12

# Number of similar items to fetch for each seed artist/track
SIMILAR_ITEMS_PER_SEED = 3

# Number of top artists to use as seeds for personalized recommendations
TOP_ARTISTS_LIMIT = 5

# Number of top tracks to use as seeds for personalized recommendations
TOP_TRACKS_LIMIT = 5

# Number of top tags to fetch for genre-based recommendations
TOP_TAGS_LIMIT = 1

# API search settings
# Search limit for provider API calls (workaround for Spotify API bug with limit=1)
PROVIDER_SEARCH_LIMIT = 2

# Image processing
# Priority order for selecting Last.fm images (largest to smallest)
IMAGE_SIZE_PRIORITY = ["mega", "extralarge", "large", "medium", "small"]

# Suffix used to identify Last.fm placeholder images to filter out
IMAGE_PLACEHOLDER_SUFFIX = "/default.png"

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
