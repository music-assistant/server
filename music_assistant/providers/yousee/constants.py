"""Constants for the YouSee Musik music provider."""

VARIOUS_ARTISTS_ID = "1776"

PAGE_SIZE = 50
# to avoid infinite loops, this effectively limits any album/playlist to
# PAGE_SIZE * MAX_PAGES_PAGINATED items (1000 items with the current settings)
MAX_PAGES_PAGINATED = 20
GET_POPULAR_TRACKS_LIMIT = 25

IMAGE_SIZE = 512

CONF_QUALITY = "yousee_quality"
