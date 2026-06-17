"""Constants for the music controller."""

from __future__ import annotations

from typing import Final

CONF_RESET_DB = "reset_db"
DEFAULT_SYNC_INTERVAL = 12 * 60  # default sync interval in minutes
CONF_SYNC_INTERVAL = "sync_interval"
CONF_DELETED_PROVIDERS = "deleted_providers"

DB_SCHEMA_VERSION: Final[int] = 42

# tracks longer that this will not be included in radio mode
RADIO_TRACK_MAX_DURATION_SECS: Final[int] = 20 * 60
DYNAMIC_RADIO_BASE_SAMPLE_SIZE: Final[int] = 5
DYNAMIC_RADIO_DYNAMIC_TARGET: Final[int] = 50

CACHE_CATEGORY_SEARCH_RESULTS: Final[int] = 10

DATABASE_CLEANUP_TASK_ID: Final[str] = "music_database_cleanup"
PROVIDER_MAPPING_CORRECTION_TASK_ID: Final[str] = "music_provider_mapping_correction"
MUSIC_SYNC_COMPLETION_CHECK_TASK_ID: Final[str] = "music_sync_completion_check"
