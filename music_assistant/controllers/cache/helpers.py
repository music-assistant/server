"""Helper utilities for the cache controller."""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable, Coroutine
from typing import (
    TYPE_CHECKING,
    Any,
    Concatenate,
    ParamSpec,
    Protocol,
    TypeVar,
    cast,
    get_type_hints,
)

from music_assistant.controllers.cache.constants import (
    DEFAULT_CACHE_EXPIRATION,
    LOGGER,
    SerializableType,
)
from music_assistant.helpers.api import parse_value

if TYPE_CHECKING:
    from music_assistant import MusicAssistant


class _Cacheable(Protocol):
    """Protocol for objects that can use the @use_cache decorator."""

    @property
    def domain(self) -> str: ...

    @property
    def mass(self) -> MusicAssistant: ...


ProviderT = TypeVar("ProviderT", bound="_Cacheable")
P = ParamSpec("P")
R = TypeVar("R")


def use_cache(
    expiration: int = DEFAULT_CACHE_EXPIRATION,
    category: int = 0,
    persistent: bool = False,
    cache_checksum: str | None = None,
    allow_bypass: bool | None = None,
    base_class: Any = None,
    allow_expired_cache: bool = False,
) -> Callable[
    [Callable[Concatenate[ProviderT, P], Awaitable[R]]],
    Callable[Concatenate[ProviderT, P], Coroutine[Any, Any, R]],
]:
    """
    Return decorator that can be used to cache a method's result.

    :param expiration: Time in seconds the cache entry should be valid.
    :param category: Category to group cache objects.
    :param persistent: If True, the entry survives cache clears.
    :param cache_checksum: Optional checksum to store with the cache object.
    :param allow_bypass: Whether to respect the BYPASS_CACHE context variable.
    :param base_class: If provided, reconstruct cached data using base_class.from_dict().
        Handles both single dicts and lists of dicts automatically.
        If not provided, falls back to type-annotation based reconstruction.
    :param allow_expired_cache: If True, enable stale-while-revalidate. When the cached
        entry has expired, return it immediately and trigger a background refresh that
        re-runs the wrapped function and updates the cache. Expired entries also
        survive the cache auto-cleanup task so they remain available as fallback data.
    """
    if allow_bypass is None:
        allow_bypass = not persistent

    def _decorator(
        func: Callable[Concatenate[ProviderT, P], Awaitable[R]],
    ) -> Callable[Concatenate[ProviderT, P], Coroutine[Any, Any, R]]:
        def _reconstruct(cachedata: Any) -> R:
            if base_class is not None:
                return cast("R", cachedata)
            # fallback: reconstruct using type annotations
            type_hints = get_type_hints(func)
            return cast("R", parse_value(func.__name__, cachedata, type_hints["return"]))

        @functools.wraps(func)
        async def wrapper(self: ProviderT, *args: P.args, **kwargs: P.kwargs) -> R:
            cache = self.mass.cache
            provider_id = getattr(self, "instance_id", self.domain)

            # create a cache key dynamically based on the (remaining) args/kwargs
            cache_key_parts = [func.__name__, *args]
            for key in sorted(kwargs.keys()):
                cache_key_parts.append(f"{key}{kwargs[key]}")
            cache_key = ".".join(map(str, cache_key_parts))

            def _store_task(result: R) -> Any:
                return cache.set(
                    key=cache_key,
                    data=cast("SerializableType", result),
                    expiration=expiration,
                    provider=provider_id,
                    category=category,
                    checksum=cache_checksum,
                    persistent=persistent,
                    allow_expired_cache=allow_expired_cache,
                )

            # try the fresh-only lookup first
            cachedata = await cache.get(
                cache_key,
                provider=provider_id,
                checksum=cache_checksum,
                category=category,
                allow_bypass=allow_bypass,
                base_class=base_class,
            )
            if cachedata is not None:
                return _reconstruct(cachedata)

            if allow_expired_cache:
                # nothing fresh; try again accepting expired entries
                cachedata = await cache.get(
                    cache_key,
                    provider=provider_id,
                    checksum=cache_checksum,
                    category=category,
                    allow_bypass=allow_bypass,
                    base_class=base_class,
                    allow_expired_cache=True,
                )
                if cachedata is not None:
                    # serve stale data and refresh in the background;
                    # task_id deduplicates concurrent refreshes for the same entry
                    async def _background_refresh() -> None:
                        try:
                            result = await func(self, *args, **kwargs)
                            await _store_task(result)
                        except Exception:
                            LOGGER.exception(
                                "Background cache refresh failed for %s/%s",
                                provider_id,
                                cache_key,
                            )

                    self.mass.create_task(
                        _background_refresh(),
                        task_id=f"cache_refresh.{provider_id}.{cache_key}",
                    )
                    return _reconstruct(cachedata)

            # cache miss: fetch synchronously, store in background
            result = await func(self, *args, **kwargs)
            self.mass.create_task(_store_task(result))
            return result

        return wrapper

    return _decorator
