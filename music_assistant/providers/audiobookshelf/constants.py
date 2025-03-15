"""Constants for Audiobookshelf provider."""

from enum import StrEnum

from aioaudiobookshelf.schema.shelf import ShelfId as AbsShelfId

# CONFIG
CONF_URL = "url"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_TOKEN = "token"
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


class AbsBrowseItemsBook(StrEnum):
    """Folder names in browse view for books."""

    AUTHORS = "Authors"
    NARRATORS = "Narrators"
    SERIES = "Series"
    COLLECTIONS = "Collections"
    AUDIOBOOKS = "Audiobooks"


class AbsBrowseItemsPodcast(StrEnum):
    """Folder names in browse view for podcasts."""

    PODCASTS = "Podcasts"


ABS_BROWSE_ITEMS_TO_PATH: dict[str, str] = {
    AbsBrowseItemsBook.AUTHORS: AbsBrowsePaths.AUTHORS,
    AbsBrowseItemsBook.NARRATORS: AbsBrowsePaths.NARRATORS,
    AbsBrowseItemsBook.SERIES: AbsBrowsePaths.SERIES,
    AbsBrowseItemsBook.COLLECTIONS: AbsBrowsePaths.COLLECTIONS,
    AbsBrowseItemsBook.AUDIOBOOKS: AbsBrowsePaths.AUDIOBOOKS,
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
