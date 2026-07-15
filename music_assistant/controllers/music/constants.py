"""Constants for the music controller."""

from __future__ import annotations

from typing import Final

CONF_RESET_DB = "reset_db"
DEFAULT_SYNC_INTERVAL = 12 * 60  # default sync interval in minutes
CONF_SYNC_INTERVAL = "sync_interval"
CONF_DELETED_PROVIDERS = "deleted_providers"

DB_SCHEMA_VERSION: Final[int] = 53

# tracks longer that this will not be included in radio mode
RADIO_TRACK_MAX_DURATION_SECS: Final[int] = 20 * 60
DYNAMIC_RADIO_BASE_SAMPLE_SIZE: Final[int] = 5
DYNAMIC_RADIO_DYNAMIC_TARGET: Final[int] = 50

CACHE_CATEGORY_SEARCH_RESULTS: Final[int] = 10

# max time to wait for a single provider's search results before
# contributing empty results for it, so one slow provider can never block
# the whole search; the provider search itself continues in the background
# so its result is cached and available for a next search request
SEARCH_PROVIDER_SOFT_TIMEOUT: Final[int] = 8
# absolute max time a (background) provider search may run,
# rate limited providers can be very slow to respond
SEARCH_PROVIDER_HARD_TIMEOUT: Final[int] = 120
# how long to cache raw per-provider search results; streaming catalogs barely
# change so they can be cached a lot longer than local providers where the
# user may add or change content at any time
SEARCH_CACHE_EXPIRATION_STREAMING_PROVIDER: Final[int] = 24 * 3600
SEARCH_CACHE_EXPIRATION_LOCAL_PROVIDER: Final[int] = 900
# how long to cache combined search results (fast path for repeated searches)
SEARCH_CACHE_EXPIRATION_COMBINED: Final[int] = 600

# max time to wait for a single provider's recommendations before skipping it,
# so one slow provider can never block the whole discover page
RECOMMENDATIONS_PROVIDER_TIMEOUT: Final[int] = 30

DATABASE_CLEANUP_TASK_ID: Final[str] = "music_database_cleanup"
PROVIDER_MAPPING_CORRECTION_TASK_ID: Final[str] = "music_provider_mapping_correction"
MUSIC_SYNC_COMPLETION_CHECK_TASK_ID: Final[str] = "music_sync_completion_check"
