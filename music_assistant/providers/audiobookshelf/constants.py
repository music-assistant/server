"""Constants for Audiobookshelf provider."""

from enum import StrEnum

from aioaudiobookshelf.schema.shelf import ShelfId as AbsShelfId
from aiohttp.client import ClientTimeout

# AIOHTTP
# we use twice the default values
AIOHTTP_TIMEOUT = ClientTimeout(total=10 * 60, sock_connect=60)

# CONFIG
CONF_URL = "url"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_OLD_TOKEN = "token"
CONF_API_TOKEN = "api_token"  # with jwt api token (>= v2.26)
CONF_VERIFY_SSL = "verify_ssl"
# optionally hide podcasts with no episodes
CONF_HIDE_EMPTY_PODCASTS = "hide_empty_podcasts"

# CACHE
CACHE_CATEGORY_LIBRARIES = 0
CACHE_KEY_LIBRARIES = "libraries"


# BROWSE
class AbsBrowsePaths(StrEnum):
    """Path prefixes for browse view."""

    LIBRARIES_BOOK = "lb"
    LIBRARIES_PODCAST = "lp"
    AUTHORS = "a"
    NARRATORS = "n"
    SERIES = "s"
    COLLECTIONS = "c"
    AUDIOBOOKS = "b"


class AbsBrowseItemsBookTranslationKey(StrEnum):
    """translation keys in browse view for books."""

    AUTHORS = "authors"
    NARRATORS = "narrators"
    SERIES = "series_plural"
    COLLECTIONS = "collections"
    AUDIOBOOKS = "audiobooks"


class AbsBrowseItemsPodcastTranslationKey(StrEnum):
    """Folder names in browse view for podcasts."""

    PODCASTS = "podcasts"


ABS_BROWSE_ITEMS_TO_PATH: dict[str, str] = {
    AbsBrowseItemsBookTranslationKey.AUTHORS: AbsBrowsePaths.AUTHORS,
    AbsBrowseItemsBookTranslationKey.NARRATORS: AbsBrowsePaths.NARRATORS,
    AbsBrowseItemsBookTranslationKey.SERIES: AbsBrowsePaths.SERIES,
    AbsBrowseItemsBookTranslationKey.COLLECTIONS: AbsBrowsePaths.COLLECTIONS,
    AbsBrowseItemsBookTranslationKey.AUDIOBOOKS: AbsBrowsePaths.AUDIOBOOKS,
}

ABS_SHELF_ID_ICONS: dict[str, str] = {
    AbsShelfId.LISTEN_AGAIN: "mdi-book-refresh-outline",
    AbsShelfId.CONTINUE_LISTENING: "mdi-clock-outline",
    AbsShelfId.CONTINUE_SERIES: "mdi-play-box-multiple-outline",
    AbsShelfId.RECOMMENDED: "mdi-lightbulb-outline",
    AbsShelfId.RECENTLY_ADDED: "mdi-plus-box-multiple-outline",
    AbsShelfId.EPISODES_RECENTLY_ADDED: "mdi-plus-box-multiple-outline",
    AbsShelfId.RECENT_SERIES: "mdi-bookshelf",
    AbsShelfId.NEWEST_AUTHORS: "mdi-plus-box-multiple-outline",
    AbsShelfId.NEWEST_EPISODES: "mdi-plus-box-multiple-outline",
    AbsShelfId.DISCOVER: "mdi-magnify",
}

# for some keys there already is a good MA variant
# note: recommendation keys are in a subdict
ABS_SHELF_ID_TRANSLATION_KEY: dict[str, str] = {
    AbsShelfId.LISTEN_AGAIN: "listen_again",
    AbsShelfId.CONTINUE_LISTENING: "in_progress_items",
    AbsShelfId.CONTINUE_SERIES: "in_progress_series",
    AbsShelfId.RECOMMENDED: "recommended",
    AbsShelfId.RECENTLY_ADDED: "recently_added",
    AbsShelfId.EPISODES_RECENTLY_ADDED: "episodes_recently_added",
    AbsShelfId.RECENT_SERIES: "recent_series",
    AbsShelfId.NEWEST_AUTHORS: "newest_authors",
    AbsShelfId.NEWEST_EPISODES: "newest_episodes",
    AbsShelfId.DISCOVER: "discover",
}
