"""Constants for the iTunes Podcasts provider."""

import math

CONF_LOCALE = "locale"
CONF_EXPLICIT = "explicit"
CONF_NUM_EPISODES = "num_episodes"

# store to search when the server's language has no matching iTunes storefront
DEFAULT_LOCALE = "us"

CACHE_CATEGORY_RECOMMENDATIONS = 1
CACHE_KEY_TOP_PODCASTS = "top-podcasts-full"
CACHE_KEY_LIBRARY_RECOMMENDATIONS = "library-recommendations"
RECOMMENDATION_ROW_TOP_PODCASTS = "itunes-top-podcasts"
RECOMMENDATION_ROW_FOR_YOU = "itunes-library-recommendations"
RECOMMENDATION_ROW_SIZE = 15

# iTunes root genre "Podcasts", present on every show and useless for similarity
ROOT_GENRE_ID = "26"
# one request per genre
MAX_SEED_GENRES = 4
GENRE_TOP_PODCASTS_LIMIT = 100
# the row is recomputed earlier when the library or the config changes
LIBRARY_RECOMMENDATIONS_CACHE_EXPIRATION = 60 * 60 * 24
# the v2 feed returns at most 100 entries
TOP_PODCASTS_LIMIT = 100
TOP_PODCASTS_CACHE_EXPIRATION = 60 * 60 * 24
# the trending row shows every n-th top podcast (1, 8, 15, ...) and moves to the next
# offset every rotation, so all of them are shown once per cache lifetime
TOP_PODCASTS_NUM_PAGES = math.ceil(TOP_PODCASTS_LIMIT / RECOMMENDATION_ROW_SIZE)
TOP_PODCASTS_ROTATION = TOP_PODCASTS_CACHE_EXPIRATION // TOP_PODCASTS_NUM_PAGES
