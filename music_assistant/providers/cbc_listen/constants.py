"""Constants for the CBC Listen provider."""

import aiohttp

from music_assistant.constants import APPLICATION_NAME

API_URL = "https://www.cbc.ca/listen/api/v1"
STATIONS_URL = f"{API_URL}/live-radio/getLiveRadioStations"
PROGRAM_QUEUE_URL = f"{API_URL}/program-queue"
SHOWS_URL = f"{API_URL}/shows/all"
SHOW_URL = f"{API_URL}/shows/1"
CLIPS_URL = f"{API_URL}/clips"

# CBC's Akamai WAF drops requests whose User-Agent contains a URL (such as MASS_USER_AGENT),
# so identify with the plain application name instead.
API_HEADERS = {"Accept": "application/json", "User-Agent": APPLICATION_NAME}
IMAGE_HEADERS = {"User-Agent": APPLICATION_NAME}
API_TIMEOUT = aiohttp.ClientTimeout(total=15)
API_RATE_LIMIT = 5
API_RATE_PERIOD = 1.0

PAGE_SIZE = 500
MAX_PAGES = 10
IMAGE_WIDTH = "512"
IMAGE_RATIO = "1x1"
IMAGE_CDN_URL = (
    f"https://images.radio-canada.ca/c_scale,w_{IMAGE_WIDTH},q_auto"
    f"/v1/cbc-music/programs/{IMAGE_RATIO}"
)
DEFAULT_IMAGE_URL = f"{IMAGE_CDN_URL}/default.png"
NETWORK_LOGOS = {
    1: f"{IMAGE_CDN_URL}/radio_one_logo.png",
    2: f"{IMAGE_CDN_URL}/cbc_music_logo.png",
}
FREE_TIER = "free"
PUBLISHER = "CBC"

CHANNELS_CACHE_SECONDS = 12 * 3600
CATALOGUE_CACHE_SECONDS = 3600
PODCAST_CACHE_SECONDS = 15 * 60
STREAM_METADATA_UPDATE_INTERVAL = 60
NOW_PLAYING_CACHE_SECONDS = STREAM_METADATA_UPDATE_INTERVAL
MAX_TRACK_AGE_MS = 30 * 60 * 1000

BROWSE_RADIOS = "radios"
BROWSE_PODCASTS = "podcasts"
BROWSE_RADIO_SHOWS = "radio_shows"
BROWSE_FOLDERS = {
    BROWSE_RADIOS: "Radio",
    BROWSE_PODCASTS: "Podcasts",
    BROWSE_RADIO_SHOWS: "Radio Shows",
}
