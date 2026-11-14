"""Constants for the BookOrbit provider."""

from enum import StrEnum

from aiohttp import hdrs
from aiohttp.client import ClientTimeout

AIOHTTP_TIMEOUT = ClientTimeout(total=10 * 60, sock_connect=60)

# a file may stream for hours, only a stalled server is an error
PART_STREAM_TIMEOUT = ClientTimeout(total=None, sock_connect=60, sock_read=60)
PART_STREAM_CHUNK_SIZE = 64 * 1024
# what ffmpeg needs to read and seek in a proxied file
PART_STREAM_HEADERS = (
    hdrs.ACCEPT_RANGES,
    hdrs.CONTENT_LENGTH,
    hdrs.CONTENT_RANGE,
    hdrs.CONTENT_TYPE,
)
# expire stream details after 5 min of not using them
STREAMDETAILS_EXPIRATION_S = 300

DOMAIN = "bookorbit"

CONF_URL = "url"

AUDIO_FORMATS = frozenset({"m4b", "mp3", "m4a", "opus", "ogg", "flac"})
CATALOG_TTL_SEC = 15 * 60
# Bound concurrent BookOrbit book-detail fetches during catalog refresh.
CATALOG_DETAIL_CONCURRENCY = 8
# mass.cache category / TTL for raw BookDetail payloads (invalidated via updatedAt checksum).
CACHE_CATEGORY_BOOK = 0
CACHE_EXPIRATION_BOOK = 24 * 3600
# Last-applied audiobook ProviderMapping.details (updatedAt) per book id for library sync.
# Sync-details rows omit mapping.details, so the provider remembers what it last wrote.
CACHE_CATEGORY_LIBRARY_FINGERPRINTS = 1
# v2: bump when sole-provider overwrite is required so stale titles are force-refreshed once.
CACHE_KEY_LIBRARY_FINGERPRINTS = "audiobook_mapping_details_v2"
CACHE_EXPIRATION_LIBRARY_FINGERPRINTS = 86400 * 365
# Last playlog progress applied inbound or sent outbound (avoids re-stamping on restart).
CACHE_CATEGORY_PLAYLOG_PROGRESS = 2
CACHE_KEY_PLAYLOG_PROGRESS = "playlog_progress_v1"
CACHE_EXPIRATION_PLAYLOG_PROGRESS = 86400 * 365
# Batch size for browse-time library lookups by provider item id.
BROWSE_LIBRARY_BATCH_SIZE = 200
# How often to pull BookOrbit progress into the MA playlog between full library syncs.
PLAYLOG_POLL_INTERVAL_SEC = 10 * 60
# Bound concurrent playback-state fetches during playlog sync / poll.
PLAYLOG_SYNC_CONCURRENCY = 8
# BookOrbit dashboard scroller max per request (DASHBOARD_SCROLLER_MAX_LIMIT).
CONTINUE_LISTENING_LIMIT = 50

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
