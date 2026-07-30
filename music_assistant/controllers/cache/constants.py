"""Constants for the cache controller."""

from __future__ import annotations

import logging
from contextvars import ContextVar

from music_assistant.constants import MASS_LOGGER_NAME

LOGGER = logging.getLogger(f"{MASS_LOGGER_NAME}.cache")
CONF_CLEAR_CACHE = "clear_cache"
DEFAULT_CACHE_EXPIRATION = 86400 * 30  # 30 days
DB_SCHEMA_VERSION = 9
MAX_CACHE_DB_SIZE_MB = 2048
CACHE_DATABASE_CLEANUP_TASK_ID = "cache_database_cleanup"
# stale-while-revalidate fallback rows are kept past their expiration so they can still be
# served while a refresh runs; the cleanup task removes those not refreshed within this window
# (their key is no longer requested) to keep the cache from growing without bound.
SWR_FALLBACK_MAX_AGE = 86400 * 90  # 90 days

BYPASS_CACHE: ContextVar[bool] = ContextVar("BYPASS_CACHE", default=False)
