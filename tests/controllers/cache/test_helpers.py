"""Tests for the @use_cache decorator."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import patch

import pytest

from music_assistant.constants import DB_TABLE_CACHE
from music_assistant.controllers.cache import CacheController
from music_assistant.controllers.cache.helpers import use_cache
from music_assistant.mass import MusicAssistant

_PROVIDER = "test_cache_helpers"


class _FakeProvider:
    """Minimal object that satisfies the @use_cache protocol."""

    domain = _PROVIDER

    def __init__(self, mass: MusicAssistant) -> None:
        self.mass = mass
        self.calls = 0
        self.result: str | None = None

    @use_cache(3600)
    async def fetch(self, item_id: str) -> str | None:
        """Return the preset result, counting invocations."""
        self.calls += 1
        return self.result

    @use_cache(3600, cache_none=False)
    async def fetch_no_none(self, item_id: str) -> str | None:
        """Return the preset result, counting invocations."""
        self.calls += 1
        return self.result

    @use_cache(3600, allow_expired_cache=True)
    async def fetch_swr(self, item_id: str) -> str | None:
        """Return the preset result, counting invocations."""
        self.calls += 1
        return self.result


@pytest.fixture
async def cache(mass_minimal: MusicAssistant) -> CacheController:
    """Return an initialized cache controller."""
    await mass_minimal.cache._setup_database()
    return mass_minimal.cache


@pytest.fixture
def provider(cache: CacheController) -> _FakeProvider:
    """Return a fake provider with @use_cache decorated methods."""
    return _FakeProvider(cache.mass)


async def _get_row(cache: CacheController, key: str) -> Any:
    """Return the raw cache db row for the given key."""
    assert cache.database is not None
    return await cache.database.get_row(
        DB_TABLE_CACHE, {"category": 0, "provider": _PROVIDER, "key": key}
    )


async def _wait_for_stored(cache: CacheController, key: str) -> None:
    """Wait until the background store task has written the cache row."""
    for _ in range(200):
        if await _get_row(cache, key):
            return
        await asyncio.sleep(0.01)
    pytest.fail(f"cache row for {key} was never written")


async def _wait_for(condition: Callable[[], Awaitable[bool]]) -> None:
    """Wait until the given (async) condition callable returns True."""
    for _ in range(200):
        if await condition():
            return
        await asyncio.sleep(0.01)
    pytest.fail("condition was never met")


# --- result caching (including None results) ---


async def test_result_served_from_cache(cache: CacheController, provider: _FakeProvider) -> None:
    """Test that a second call is served from cache without re-invoking the function."""
    provider.result = "value"
    assert await provider.fetch("a") == "value"
    assert provider.calls == 1
    await _wait_for_stored(cache, "fetch.a")
    provider.result = "changed"
    assert await provider.fetch("a") == "value"
    assert provider.calls == 1


async def test_none_result_cached_and_served(
    cache: CacheController, provider: _FakeProvider
) -> None:
    """Test that a None result is cached and served without re-invoking the function."""
    provider.result = None
    assert await provider.fetch("a") is None
    assert provider.calls == 1
    await _wait_for_stored(cache, "fetch.a")
    assert await provider.fetch("a") is None
    assert provider.calls == 1


async def test_cache_none_false_retries_and_skips_store(
    cache: CacheController, provider: _FakeProvider
) -> None:
    """Test that cache_none=False re-invokes on None and does not store the None result."""
    provider.result = None
    assert await provider.fetch_no_none("a") is None
    assert provider.calls == 1
    # give a (wrongly created) store task time to run, then verify nothing was written
    await asyncio.sleep(0.05)
    assert await _get_row(cache, "fetch_no_none.a") is None
    assert await provider.fetch_no_none("a") is None
    assert provider.calls == 2
    # once the function returns a real value, it is cached again
    provider.result = "found"
    assert await provider.fetch_no_none("a") == "found"
    assert provider.calls == 3
    await _wait_for_stored(cache, "fetch_no_none.a")
    assert await provider.fetch_no_none("a") == "found"
    assert provider.calls == 3


async def test_cache_none_false_ignores_stored_none(
    cache: CacheController, provider: _FakeProvider
) -> None:
    """Test that cache_none=False treats a previously stored None row as a cache miss."""
    await cache.set("fetch_no_none.a", None, provider=_PROVIDER)
    provider.result = "fresh"
    assert await provider.fetch_no_none("a") == "fresh"
    assert provider.calls == 1


# --- stale-while-revalidate ---


async def test_swr_serves_stale_and_refreshes(
    cache: CacheController, provider: _FakeProvider
) -> None:
    """Test that an expired entry is served immediately and refreshed in the background."""
    await cache.set(
        "fetch_swr.a", "stale", provider=_PROVIDER, expiration=-1, allow_expired_cache=True
    )
    provider.result = "fresh"
    assert await provider.fetch_swr("a") == "stale"

    async def _refreshed() -> bool:
        return bool(await cache.get("fetch_swr.a", provider=_PROVIDER) == "fresh")

    await _wait_for(_refreshed)
    assert provider.calls == 1
    assert await provider.fetch_swr("a") == "fresh"
    assert provider.calls == 1


async def test_wrapper_performs_single_row_fetch(
    cache: CacheController, provider: _FakeProvider
) -> None:
    """Test that one wrapper call does exactly one db row fetch (miss, fresh and stale)."""
    assert cache.database is not None
    # cache miss
    provider.result = "value"
    with patch.object(cache.database, "get_row", wraps=cache.database.get_row) as spy:
        assert await provider.fetch_swr("a") == "value"
    assert spy.await_count == 1
    # fresh hit
    await _wait_for_stored(cache, "fetch_swr.a")
    with patch.object(cache.database, "get_row", wraps=cache.database.get_row) as spy:
        assert await provider.fetch_swr("a") == "value"
    assert spy.await_count == 1
    # stale hit (previously fetched the same row twice)
    await cache.set(
        "fetch_swr.b", "stale", provider=_PROVIDER, expiration=-1, allow_expired_cache=True
    )
    with patch.object(cache.database, "get_row", wraps=cache.database.get_row) as spy:
        assert await provider.fetch_swr("b") == "stale"
    assert spy.await_count == 1


# --- get_with_freshness ---


async def test_get_with_freshness(cache: CacheController) -> None:
    """Test that get_with_freshness reports the freshness and presence of entries."""
    await cache.set("fresh", "data", provider=_PROVIDER, expiration=3600)
    assert await cache.get_with_freshness("fresh", provider=_PROVIDER) == ("data", True, True)
    await cache.set("expired", "old", provider=_PROVIDER, expiration=-1)
    # an expired entry is reported as not found unless include_expired is set
    assert await cache.get_with_freshness("expired", provider=_PROVIDER) == (None, False, False)
    assert await cache.get_with_freshness("expired", provider=_PROVIDER, include_expired=True) == (
        "old",
        False,
        True,
    )
    data, is_fresh, found = await cache.get_with_freshness("missing", provider=_PROVIDER)
    assert found is False
    assert is_fresh is False
    assert data is None
