"""Helper utilities for cached properties with various expiration strategies."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable


class TimedCachedProperty:
    """
    Cached property decorator with time-based expiration.

    Similar to cached_property but the cached value expires after a specified duration.
    The property value is recalculated when accessed after expiration.

    The cached values are stored in the instance's `_cache` dictionary, which means:
    - Calling `_cache.clear()` will clear all cached values and timestamps
    - This integrates seamlessly with the Player class's `update_state()` method
    - Both automatic (time-based) and manual cache clearing are supported

    :param ttl: Time-to-live in seconds (default: 5 seconds)

    Example:
        >>> class MyClass:
        ...     def __init__(self):
        ...         self._cache = {}
        ...
        ...     # Usage with default TTL (5 seconds)
        ...     @timed_cached_property
        ...     def property1(self) -> str:
        ...         return "computed value"
        ...
        ...     # Usage with custom TTL
        ...     @timed_cached_property(ttl=10.0)
        ...     def property2(self) -> str:
        ...         return "computed value"
    """

    def __init__(self, ttl: float | Callable[..., Any] = 5.0) -> None:
        """Initialize the timed cached property decorator."""
        # Support both @timed_cached_property and @timed_cached_property()
        if callable(ttl):
            # Used without parentheses: @timed_cached_property
            self.func: Callable[..., Any] | None = ttl
            self.ttl: float = 5.0
            self.attrname: str | None = None
        else:
            # Used with parentheses: @timed_cached_property() or @timed_cached_property(ttl=10)
            self.func = None
            self.ttl = ttl
            self.attrname = None

    def __set_name__(self, owner: type, name: str) -> None:
        """Store the attribute name when the descriptor is assigned to a class attribute."""
        self.attrname = name

    def __call__(self, func: Callable[..., Any]) -> TimedCachedProperty:
        """Allow the decorator to be used with or without arguments."""
        # If func is already set, this is being used as @timed_cached_property
        # without parentheses, so just return self
        if self.func is not None:
            return self

        # Otherwise, this is being used as @timed_cached_property()
        # with parentheses, so set the func and return self
        self.func = func
        self.attrname = func.__name__
        return self

    def __get__(self, instance: Any, owner: type | None = None) -> Any:
        """Get the cached value or compute it if expired or not cached."""
        if instance is None:
            return self

        # Use the instance's _cache dict to store values and timestamps
        cache: dict[str, Any] = instance._cache
        cache_key = self.attrname or (self.func.__name__ if self.func else "unknown")
        timestamp_key = f"{cache_key}_timestamp"

        # Check if we have a cached value and if it's still valid
        current_time = time.time()
        if cache_key in cache and timestamp_key in cache:
            if current_time - cache[timestamp_key] < self.ttl:
                # Cache is still valid
                return cache[cache_key]

        # Cache miss or expired - compute new value
        if self.func is None:
            msg = "Function is not set"
            raise RuntimeError(msg)
        value = self.func(instance)
        cache[cache_key] = value
        cache[timestamp_key] = current_time

        return value


# Convenience alias for backward compatibility with lowercase naming
timed_cached_property = TimedCachedProperty


__all__ = ["TimedCachedProperty", "timed_cached_property"]
