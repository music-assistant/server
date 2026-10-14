"""Constants for the BookOrbit provider."""

from enum import StrEnum

from aiohttp.client import ClientTimeout

AIOHTTP_TIMEOUT = ClientTimeout(total=10 * 60, sock_connect=60)

DOMAIN = "bookorbit"

CONF_URL = "url"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"  # noqa: S105
CONF_API_TOKEN = "api_token"  # reserved; unused in v1  # noqa: S105
CONF_VERIFY_SSL = "verify_ssl"

AUDIO_FORMATS = frozenset({"m4b", "mp3", "m4a", "opus", "ogg", "flac"})
CATALOG_TTL_SEC = 60

API_PREFIX = "/api/v1"


class BrowsePaths(StrEnum):
    """Browse path prefixes."""

    LIBRARIES = "lb"
    AUTHORS = "a"
    NARRATORS = "n"
    SERIES = "s"
    AUDIOBOOKS = "b"


class BrowseItemsTranslationKey(StrEnum):
    """Translation keys of the folders inside a library in the browse view."""

    AUTHORS = "authors"
    NARRATORS = "narrators"
    SERIES = "series_plural"
    AUDIOBOOKS = "audiobooks"
    AUDIOBOOKS_LIBRARY = "audiobooks_library"


BROWSE_ITEMS_TO_PATH: dict[str, str] = {
    BrowseItemsTranslationKey.AUTHORS: BrowsePaths.AUTHORS,
    BrowseItemsTranslationKey.NARRATORS: BrowsePaths.NARRATORS,
    BrowseItemsTranslationKey.SERIES: BrowsePaths.SERIES,
    BrowseItemsTranslationKey.AUDIOBOOKS: BrowsePaths.AUDIOBOOKS,
}
