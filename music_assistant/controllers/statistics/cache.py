"""User-scoped, generation-keyed statistics caching."""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar
from uuid import uuid4

from music_assistant.controllers.cache import use_cache
from music_assistant.controllers.webserver.helpers.auth_middleware import get_current_user

# Statistics cache TTL: 5 minutes
STATISTICS_CACHE_TTL = 300
T = TypeVar("T")


def cache_statistics(
    ttl: int = STATISTICS_CACHE_TTL,
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """Cache statistics through the shared cache controller, scoped by user and generation."""

    def decorator(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        # A generation in the cache key keeps an in-flight request from repopulating
        # the current cache with stale results after play history invalidates it.
        @use_cache(expiration=ttl)
        @functools.wraps(func)
        async def cached_method(
            self: Any,
            _user_id: str,
            _cache_generation: str,
            *args: Any,
            **kwargs: Any,
        ) -> T:
            return await func(self, *args, **kwargs)

        @functools.wraps(func)
        async def wrapper(self: Any, *args: Any, **kwargs: Any) -> T:
            user = get_current_user()
            user_id = user.user_id if user else "anonymous"
            generation = self._statistics_user_cache_generations.get(user_id)
            if generation is None:
                generation = uuid4().hex
                self._statistics_user_cache_generations[user_id] = generation
            return await cached_method(
                self,
                user_id,
                generation,
                *args,
                **kwargs,
            )

        return wrapper

    return decorator
