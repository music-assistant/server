"""Provides a simple global memory cache."""

from __future__ import annotations

import asyncio
from typing import Any

# global cache - we use this on a few places (as limited as possible)
# where we have no other options
_global_cache_lock = asyncio.Lock()
_global_cache: dict[str, Any] = {}


def get_global_cache_value(key: str, default: Any = None) -> Any:
    """Get a value from the global cache."""
    return _global_cache.get(key, default)


async def set_global_cache_values(values: dict[str, Any]) -> Any:
    """Set a value in the global cache (without locking)."""
    async with _global_cache_lock:
        for key, value in values.items():
            _set_global_cache_value(key, value)


def _set_global_cache_value(key: str, value: Any) -> Any:
    """Set a value in the global cache (without locking)."""
    _global_cache[key] = value
