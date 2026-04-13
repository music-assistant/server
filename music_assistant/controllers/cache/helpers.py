"""Helper utilities for the cache controller."""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable, Coroutine
from typing import TYPE_CHECKING, Any, Concatenate, ParamSpec, TypeVar, cast, get_type_hints

from music_assistant.controllers.cache.constants import DEFAULT_CACHE_EXPIRATION, SerializableType
from music_assistant.helpers.api import parse_value

if TYPE_CHECKING:
    from music_assistant.models.core_controller import CoreController
    from music_assistant.models.provider import Provider


ProviderT = TypeVar("ProviderT", bound="Provider | CoreController")
P = ParamSpec("P")
R = TypeVar("R")


def use_cache(
    expiration: int = DEFAULT_CACHE_EXPIRATION,
    category: int = 0,
    persistent: bool = False,
    cache_checksum: str | None = None,
    allow_bypass: bool | None = None,
    base_class: Any = None,
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
    """
    if allow_bypass is None:
        allow_bypass = not persistent

    def _decorator(
        func: Callable[Concatenate[ProviderT, P], Awaitable[R]],
    ) -> Callable[Concatenate[ProviderT, P], Coroutine[Any, Any, R]]:
        @functools.wraps(func)
        async def wrapper(self: ProviderT, *args: P.args, **kwargs: P.kwargs) -> R:
            cache = self.mass.cache
            provider_id = getattr(self, "instance_id", self.domain)

            # create a cache key dynamically based on the (remaining) args/kwargs
            cache_key_parts = [func.__name__, *args]
            for key in sorted(kwargs.keys()):
                cache_key_parts.append(f"{key}{kwargs[key]}")
            cache_key = ".".join(map(str, cache_key_parts))
            # try to retrieve data from the cache
            cachedata = await cache.get(
                cache_key,
                provider=provider_id,
                checksum=cache_checksum,
                category=category,
                allow_bypass=allow_bypass,
                base_class=base_class,
            )
            if cachedata is not None:
                if base_class is not None:
                    return cast("R", cachedata)
                # fallback: reconstruct using type annotations
                type_hints = get_type_hints(func)
                return cast("R", parse_value(func.__name__, cachedata, type_hints["return"]))
            # get data from method/provider
            result = await func(self, *args, **kwargs)
            # store result in cache (but don't await)
            self.mass.create_task(
                cache.set(
                    key=cache_key,
                    data=cast("SerializableType", result),
                    expiration=expiration,
                    provider=provider_id,
                    category=category,
                    checksum=cache_checksum,
                    persistent=persistent,
                )
            )
            return result

        return wrapper

    return _decorator
