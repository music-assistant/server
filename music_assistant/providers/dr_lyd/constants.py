"""Constants for the DR LYD provider."""

import aiohttp

API_URL = "https://api.dr.dk/radio/v5"
CHANNELS_URL = f"{API_URL}/channels"
PROGRAMMES_URL = f"{API_URL}/schedules/all/now"
SERIES_URL = f"{API_URL}/series"
SEARCH_URL = f"{API_URL}/search/series"
EPISODES_URL = f"{API_URL}/episodes"
TRACKS_URL = f"{API_URL}/indexpoints/live"

# Square picture of 512 pixels for the id of an image asset.
IMAGE_URL = (
    "https://asset.dr.dk/drlyd/images/{}"
    "?im=AspectCrop%3D%28512%2C512%29%2CxPosition%3D.5%2CyPosition%3D.5%3BResize%3D%28512%2C512%29"
)

# The API key that the web app of DR LYD sends itself, it is public.
API_KEY = "6Wkh8s98Afx1ZAaTT4FuWODTmvWGDPpR"
API_HEADERS = {"Accept": "application/json", "x-apikey": API_KEY}
API_TIMEOUT = aiohttp.ClientTimeout(total=15)
API_RATE_LIMIT = 5
API_RATE_PERIOD = 1.0

# Lists are paged, a page holds at most PAGE_SIZE items and MAX_PAGES bounds the pages followed.
PAGE_SIZE = 500
MAX_PAGES = 100

CATALOGUE_CACHE_SECONDS = 3600
PODCAST_CACHE_SECONDS = 15 * 60
CHANNELS_CACHE_SECONDS = 12 * 3600
# DR lists a track well after it started and the stream plays a few segments behind its live
# edge, so what a listener hears is looked up this many seconds back in time.
STREAM_DELAY_SECONDS = 30
STREAM_METADATA_UPDATE_INTERVAL = 10
NOW_PLAYING_CACHE_SECONDS = STREAM_METADATA_UPDATE_INTERVAL

PUBLISHER = "DR"
# The ids of the browse folders are also their translation keys, podcasts and radios are shared.
BROWSE_RECENT = "recently_updated"
BROWSE_PODCASTS = "podcasts"
BROWSE_RADIOS = "radios"
BROWSE_FOLDERS = {
    BROWSE_RECENT: "Recently updated",
    BROWSE_PODCASTS: "Podcasts",
    BROWSE_RADIOS: "Radio",
}
BROWSE_RECENT_COUNT = 50
