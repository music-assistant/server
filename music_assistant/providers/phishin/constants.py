"""Constants for Phish.in provider."""

from typing import Final

# API Configuration
API_BASE_URL: Final[str] = "https://phish.in/api/v2"
REQUEST_TIMEOUT: Final[int] = 30
DEFAULT_LIMIT: Final[int] = 100
MAX_SEARCH_RESULTS: Final[int] = 50

# Provider metadata
PROVIDER_DOMAIN: Final[str] = "phishin"
PROVIDER_NAME: Final[str] = "Phish.in"

# Phish artist information
PHISH_ARTIST_NAME: Final[str] = "Phish"
PHISH_ARTIST_ID: Final[str] = "phish"

# API endpoints
ENDPOINTS = {
    "shows": "/shows",
    "show_by_date": "/shows/{date}",
    "random_show": "/shows/random",
    "songs": "/songs",
    "song_by_slug": "/songs/{slug}",
    "tracks": "/tracks",
    "track_by_id": "/tracks/{id}",
    "tours": "/tours",
    "tour_by_slug": "/tours/{slug}",
    "venues": "/venues",
    "venue_by_slug": "/venues/{slug}",
    "years": "/years",
    "search": "/search/{term}",
}
