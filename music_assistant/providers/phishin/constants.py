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
PHISH_MUSICBRAINZ_ID: Final[str] = "e01646f2-2a04-450d-8bf2-0d993082e058"
PHISH_DISCOGS_ID: Final[str] = "252354"
PHISH_TADB_ID: Final[str] = "112677"

# Fallback image for albums without artwork
FALLBACK_ALBUM_IMAGE: Final[str] = (
    "https://raw.githubusercontent.com/music-assistant/music-assistant.io/refs/heads/main/docs/assets/icons/phish-logo.png"
)

# API endpoints
ENDPOINTS = {
    "shows": "/shows",
    "show_by_date": "/shows/{date}",
    "shows_day_of_year": "/shows/day_of_year/{date}",
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
    "tags": "/tags",
    "playlists": "/playlists",
    "playlist_by_slug": "/playlists/{slug}",
}
