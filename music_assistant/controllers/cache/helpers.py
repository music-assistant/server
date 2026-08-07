"""Helper utilities for the cache controller."""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Awaitable, Callable, Coroutine
from copy import deepcopy
from dataclasses import dataclass
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
    BYPASS_CACHE,
    DEFAULT_CACHE_EXPIRATION,
    LOGGER,
)
from music_assistant.helpers.api import parse_value
from music_assistant.helpers.json import SerializableType

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
    cache_none: bool = True,
    single_flight: bool = True,
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
    :param cache_none: Whether a None result is cached and served like any other value
        (a negative hit, e.g. "no lyrics exist for this track"). Set to False for
        methods where None signals a (transient) failure, so the call is retried on
        the next invocation instead of serving a cached None.
    :param single_flight: Whether concurrent callers that miss the cache on the same key
        share one execution of the wrapped function instead of each running it. Callers of
        a shared fetch each get their own copy of the result, or share the one object when
        it cannot be copied. Set to False when sharing a fetch would be wrong, e.g. because
        the call advances a cursor on the provider side or sends a one-shot event, so
        concurrent callers must each run their own fetch instead of sharing one. This does
        not apply while a stale entry is served under allow_expired_cache: that refresh is
        always a single background call, whichever way single_flight is set.
    """
    if allow_bypass is None:
        allow_bypass = not persistent

    def _decorator(
        func: Callable[Concatenate[ProviderT, P], Awaitable[R]],
    ) -> Callable[Concatenate[ProviderT, P], Coroutine[Any, Any, R]]:
        def _reconstruct(cachedata: Any) -> R:
            if base_class is not None:
                return cast("R", cachedata)
            # fallback: reconstruct using the (memoized) return-type annotation
            return cast("R", parse_value(func.__name__, cachedata, _resolve_return_hint(func)))

        @functools.wraps(func)
        async def wrapper(self: ProviderT, *args: P.args, **kwargs: P.kwargs) -> R:
            cache = self.mass.cache
            provider_id = getattr(self, "instance_id", self.domain)

            # create a cache key dynamically based on the (remaining) args/kwargs
            cache_key_parts = [func.__name__, *args]
            for key in sorted(kwargs.keys()):
                cache_key_parts.append(f"{key}{kwargs[key]}")
            cache_key = ".".join(map(str, cache_key_parts))

            # single lookup that returns the entry, its freshness and whether it was found;
            # found distinguishes a stored None (a negative hit) from a cache miss. expired
            # entries are only read (and deserialized) when this call serves stale data
            cachedata, is_fresh, found = await cache.get_with_freshness(
                cache_key,
                provider=provider_id,
                checksum=cache_checksum,
                category=category,
                allow_bypass=allow_bypass,
                base_class=base_class,
                include_expired=allow_expired_cache,
            )
            # a found value counts as a hit, except a None that this call opted out of
            # caching (cache_none=False) which is treated as a miss and re-fetched
            cache_hit = found and (cache_none or cachedata is not None)

            if cache_hit and is_fresh:
                return _reconstruct(cachedata)

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

            if cache_hit and allow_expired_cache:
                # serve stale data and refresh in the background;
                # task_id deduplicates concurrent refreshes for the same entry
                async def _background_refresh() -> None:
                    try:
                        result = await func(self, *args, **kwargs)
                        if cache_none or result is not None:
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

            # cache miss (or expired entry without stale-while-revalidate):
            # fetch synchronously, store in background
            async def _fetch_and_store() -> R:
                result = await func(self, *args, **kwargs)
                if cache_none or result is not None:
                    self.mass.create_task(_store_task(result))
                return result

            # a caller that bypasses this method's cache asked for the backend, so it
            # fetches alone rather than joining or publishing a flight
            if not single_flight or (allow_bypass and BYPASS_CACHE.get()):
                return await _fetch_and_store()

            async def _flight() -> _FlightOutcome[R]:
                # the outcome is returned rather than raised, to keep a routine failure quiet:
                # a MediaNotFoundError out of a cached lookup is an ordinary result that
                # callers deal with themselves, while a raising task would both draw a warning
                # from create_task and, once every caller has gone, have asyncio report it
                # against the shielded future
                outcome: _FlightOutcome[R] = _FlightOutcome()
                try:
                    outcome.result = await _fetch_and_store()
                except Exception as err:
                    outcome.error = err
                return outcome

            # task_id folds concurrent callers for this key onto one execution
            flight = self.mass.create_task(
                _flight(), task_id=f"cache_flight.{provider_id}.{cache_key}"
            )
            if flight is asyncio.current_task():
                # a body that calls back into itself for the same key is handed the very
                # fetch it is running in, and awaiting that would wait on itself forever
                return await _fetch_and_store()
            # the shield keeps the shared task out of every caller's cancellation scope: a
            # caller giving up cancels neither the fetch nor the callers still waiting
            outcome = await asyncio.shield(flight)
            if outcome.error is not None:
                raise outcome.error
            # the fetched objects stay behind with the flight, which keeps them pristine for
            # the cache write; callers get a clone each because they do mutate results in
            # place (per-user podcast resume state, for one)
            try:
                return deepcopy(cast("R", outcome.result))
            except Exception as err:
                LOGGER.warning(
                    "Cannot copy the shared result for %s/%s, callers share one object: %s",
                    provider_id,
                    cache_key,
                    err,
                )
                return cast("R", outcome.result)

        return wrapper

    return _decorator


@dataclass(slots=True)
class _FlightOutcome[ResultT]:
    """Outcome of one shared fetch, handed to every caller awaiting that fetch."""

    result: ResultT | None = None
    error: Exception | None = None


@functools.cache
def _resolve_return_hint(func: Callable[..., Any]) -> Any:
    """
    Return the resolved return-type annotation of func, memoized per function.

    A function's return annotation is invariant for the process lifetime, so it is resolved
    once and cached instead of re-running get_type_hints() — which re-evaluates the PEP-563
    string annotations — on every cache hit. Resolution stays lazy (it happens on the first
    hit, not at decoration time), so forward-reference handling is unchanged.

    :param func: The decorated function whose return-type annotation to resolve.
    """
    return get_type_hints(func)["return"]
