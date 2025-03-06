"""Constants for Audiobookshelf provider."""

from enum import StrEnum

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


ABSBROWSEITEMSTOPATH: dict[str, str] = {
    AbsBrowseItemsBook.AUTHORS: AbsBrowsePaths.AUTHORS,
    AbsBrowseItemsBook.NARRATORS: AbsBrowsePaths.NARRATORS,
    AbsBrowseItemsBook.SERIES: AbsBrowsePaths.SERIES,
    AbsBrowseItemsBook.COLLECTIONS: AbsBrowsePaths.COLLECTIONS,
    AbsBrowseItemsBook.AUDIOBOOKS: AbsBrowsePaths.AUDIOBOOKS,
}
