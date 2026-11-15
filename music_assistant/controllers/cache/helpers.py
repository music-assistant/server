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
from weakref import WeakKeyDictionary

from music_assistant.controllers.cache.constants import (
    BYPASS_CACHE,
    DEFAULT_CACHE_EXPIRATION,
    LOGGER,
)
from music_assistant.helpers.api import parse_value
from music_assistant.helpers.json import SerializableType
from music_assistant.helpers.throttle_retry import RequestPriority, set_request_priority

if TYPE_CHECKING:
    from types import MethodType

    from music_assistant import MusicAssistant
    from music_assistant.controllers.cache.controller import CacheController


class _Cacheable(Protocol):
    """Protocol for objects that can use the @use_cache decorator."""

    @property
    def domain(self) -> str: ...

    @property
    def mass(self) -> MusicAssistant: ...


ProviderT = TypeVar("ProviderT", bound="_Cacheable")
P = ParamSpec("P")
R = TypeVar("R")

# the attribute that marks a use_cache method with the category it keeps its results in
_CATEGORY_ATTRIBUTE = "_use_cache_category"
# per provider id and method, how often the kept results of each family of calls were
# dropped: a call only keeps its result while that count stood still
_drops: dict[tuple[str, str], dict[str, int]] = {}
# one lock per provider id and method: a store checks the drop count and writes, and a drop
# raises the count and deletes, each holding it. Kept per cache, as a lock serves the event
# loop of one server
_locks: WeakKeyDictionary[CacheController, dict[tuple[str, str], asyncio.Lock]] = (
    WeakKeyDictionary()
)


def use_cache(
    expiration: int = DEFAULT_CACHE_EXPIRATION,
    category: int = 0,
    persistent: bool = False,
    cache_checksum: str | None = None,
    allow_bypass: bool | None = None,
    base_class: Any = None,
    allow_expired_cache: bool = False,
    cache_none: bool = True,
) -> Callable[
    [Callable[Concatenate[ProviderT, P], Awaitable[R]]],
    Callable[Concatenate[ProviderT, P], Coroutine[Any, Any, R]],
]:
    """
    Return decorator that can be used to cache a method's result.

    Concurrent callers that miss the cache on the same key share one execution and each
    get their own copy of the result, or the one object when it cannot be copied.

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
        async def wrapper(self: ProviderT, /, *args: P.args, **kwargs: P.kwargs) -> R:
            cache = self.mass.cache
            provider_id = _owner_id(self)
            cache_key = _cache_key(func.__name__, *args, **kwargs)
            # what this call fetches is only kept while its entry is not dropped meanwhile
            drops = _drop_count(provider_id, func.__name__, cache_key)

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

            async def _store_task(result: R) -> None:
                async with _lock(cache, provider_id, func.__name__):
                    # a result fetched before its entry was dropped may predate the change
                    # that dropped it
                    if _drop_count(provider_id, func.__name__, cache_key) != drops:
                        return
                    await cache.set(
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
                    set_request_priority(RequestPriority.LOW)
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
                    # the cache key carries the looked-up arguments, so the name keeps to
                    # the provider
                    task_name=f"cache_refresh_{provider_id}",
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
            if allow_bypass and BYPASS_CACHE.get():
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

            # task_id folds concurrent callers for this key onto one execution, as long as
            # they started under the same drop count of its entry
            flight = self.mass.create_task(
                _flight(),
                task_id=_flight_id(provider_id, cache_key, drops),
                task_name=f"cache_flight_{provider_id}",
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

        setattr(wrapper, _CATEGORY_ATTRIBUTE, category)
        return wrapper

    return _decorator


async def forget_cached(method: Callable[..., Awaitable[Any]], *args: Any) -> None:
    """
    Drop what use_cache keeps of a method for every call that starts with the given arguments.

    Calls that pass further arguments are dropped too, and what a call that started earlier
    is still fetching is not kept. A method without use_cache keeps nothing to drop.

    :param method: The method, bound to the object it keeps its results for.
    :param args: The leading positional arguments of the calls to drop.
    """
    func = getattr(method, "__func__", None)
    if (category := getattr(func, _CATEGORY_ATTRIBUTE, None)) is None:
        return
    bound = cast("MethodType", method)
    owner = cast("_Cacheable", bound.__self__)
    provider_id = _owner_id(owner)
    family = _cache_key(bound.__name__, *args)
    cache = owner.mass.cache
    async with _lock(cache, provider_id, bound.__name__):
        families = _drops.setdefault((provider_id, bound.__name__), {})
        families[family] = families.get(family, 0) + 1
        await cache.delete(family, category=category, provider=provider_id)
        await cache.delete_prefix(f"{family}.", provider=provider_id, category=category)


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


def _owner_id(owner: _Cacheable) -> str:
    """Return the provider id an object keeps its use_cache results under."""
    return cast("str", getattr(owner, "instance_id", owner.domain))


def _cache_key(func_name: str, *args: Any, **kwargs: Any) -> str:
    """
    Return the key use_cache keeps the result of a call under.

    :param func_name: The name of the cached method.
    :param args: The positional arguments of the call, its owner left out.
    :param kwargs: The keyword arguments of the call.
    """
    parts = [func_name, *args, *(f"{key}{kwargs[key]}" for key in sorted(kwargs))]
    return ".".join(map(str, parts))


def _drop_count(provider_id: str, func_name: str, key: str) -> int:
    """
    Return how often the families of calls a cache key belongs to were dropped.

    :param provider_id: The provider id the key is kept under.
    :param func_name: The name of the cached method.
    :param key: The cache key of the call.
    """
    if (families := _drops.get((provider_id, func_name))) is None:
        return 0
    return sum(
        count for family, count in families.items() if key == family or key.startswith(f"{family}.")
    )


def _lock(cache: CacheController, provider_id: str, func_name: str) -> asyncio.Lock:
    """
    Return the lock a cache holds for the stores and drops of one method of a provider id.

    :param cache: The cache the results are kept in.
    :param provider_id: The provider id the results are kept under.
    :param func_name: The name of the cached method.
    """
    locks = _locks.setdefault(cache, {})
    if (lock := locks.get((provider_id, func_name))) is None:
        lock = locks[(provider_id, func_name)] = asyncio.Lock()
    return lock


def _flight_id(provider_id: str, key: str, drops: int) -> str:
    """
    Return the task id of the shared fetch of a key, one for each drop count of its entry.

    :param provider_id: The provider id the key is kept under.
    :param key: The cache key of the call.
    :param drops: How often the families of calls the key belongs to were dropped.
    """
    # an entry that was never dropped keeps the plain id
    prefix = f"cache_flight#{drops}" if drops else "cache_flight"
    return f"{prefix}.{provider_id}.{key}"
