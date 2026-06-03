"""Constants for the cache controller."""

from __future__ import annotations

import logging
from contextvars import ContextVar
from typing import Any

from music_assistant.constants import MASS_LOGGER_NAME

LOGGER = logging.getLogger(f"{MASS_LOGGER_NAME}.cache")
CONF_CLEAR_CACHE = "clear_cache"
DEFAULT_CACHE_EXPIRATION = 86400 * 30  # 30 days
DB_SCHEMA_VERSION = 8
MAX_CACHE_DB_SIZE_MB = 2048
CACHE_DATABASE_CLEANUP_TASK_ID = "cache_database_cleanup"

BYPASS_CACHE: ContextVar[bool] = ContextVar("BYPASS_CACHE", default=False)

# Type alias for data that can be stored in the cache.
# Only JSON-serializable types are accepted - storing model objects directly
# is not allowed as the cache always serializes/deserializes through JSON.
# Note: tuples are accepted but will be returned as lists after JSON round-trip.
SerializableType = str | int | float | bool | None | list[Any] | tuple[Any, ...] | dict[str, Any]
