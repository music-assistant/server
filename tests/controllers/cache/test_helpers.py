"""Tests for the @use_cache decorator."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from mashumaro import DataClassDictMixin
from music_assistant_models.errors import MediaNotFoundError

from music_assistant.constants import DB_TABLE_CACHE
from music_assistant.controllers.cache import CacheController
from music_assistant.controllers.cache.constants import BYPASS_CACHE
from music_assistant.controllers.cache.helpers import use_cache
from music_assistant.mass import MusicAssistant

_PROVIDER = "test_cache_helpers"


@dataclass
class _Payload(DataClassDictMixin):
    """Minimal model to exercise base_class reconstruction."""

    item_id: str
    played: bool = False


class _Uncopyable:
    """Object that refuses to be copied."""

    def __deepcopy__(self, memo: dict[int, Any]) -> _Uncopyable:
        """Raise to simulate a result holding something uncopyable."""
        raise TypeError("cannot copy this")


class _FakeProvider:
    """Minimal object that satisfies the @use_cache protocol."""

    domain = _PROVIDER

    def __init__(self, mass: MusicAssistant) -> None:
        self.mass = mass
        self.calls = 0
        self.result: str | None = None
        self.error: Exception | None = None
        # released by default, so tests that do not gate a call are unaffected
        self.gate = asyncio.Event()
        self.gate.set()

    @use_cache(3600)
    async def fetch(self, item_id: str) -> str | None:
        """Return the preset result, counting invocations."""
        return await self._result()

    @use_cache(3600, cache_none=False)
    async def fetch_no_none(self, item_id: str) -> str | None:
        """Return the preset result, counting invocations."""
        return await self._result()

    @use_cache(3600, allow_expired_cache=True)
    async def fetch_swr(self, item_id: str) -> str | None:
        """Return the preset result, counting invocations."""
        return await self._result()

    @use_cache(3600, single_flight=False)
    async def fetch_no_flight(self, item_id: str) -> str | None:
        """Return the preset result, counting invocations."""
        return await self._result()

    @use_cache(3600)
    async def fetch_items(self, item_id: str) -> list[dict[str, Any]]:
        """Return a freshly built mutable payload, counting invocations."""
        self.calls += 1
        await self.gate.wait()
        return [{"id": item_id, "played": False}]

    @use_cache(3600, base_class=_Payload)
    async def fetch_models(self, item_id: str) -> list[_Payload]:
        """Return a freshly built list of models, counting invocations."""
        self.calls += 1
        await self.gate.wait()
        return [_Payload(item_id=item_id)]

    @use_cache(3600)
    async def fetch_reentrant(self, item_id: str) -> str | None:
        """Call back into itself once for the same item, counting invocations."""
        self.calls += 1
        if self.calls == 1:
            return await self.fetch_reentrant(item_id)
        return self.result

    @use_cache(3600)
    async def fetch_tuples(self, item_id: str) -> list[tuple[str, str, str | None]]:
        """Return a payload whose shape a serialization round-trip would not survive."""
        self.calls += 1
        await self.gate.wait()
        return [(item_id, "name", None)]

    @use_cache(3600)
    async def fetch_uncopyable(self, item_id: str) -> _Uncopyable:
        """Return a payload that cannot be copied, counting invocations."""
        self.calls += 1
        await self.gate.wait()
        return _Uncopyable()

    async def _result(self) -> str | None:
        """Return the preset result once the gate is open, raising a preset error."""
        self.calls += 1
        await self.gate.wait()
        if self.error is not None:
            raise self.error
        return self.result


@pytest.fixture
def provider(cache_controller: CacheController) -> _FakeProvider:
    """Return a fake provider with @use_cache decorated methods."""
    return _FakeProvider(cache_controller.mass)


async def _get_row(cache_controller: CacheController, key: str) -> Any:
    """Return the raw cache db row for the given key."""
    assert cache_controller.database is not None
    return await cache_controller.database.get_row(
        DB_TABLE_CACHE, {"category": 0, "provider": _PROVIDER, "key": key}
    )


async def _wait_for_stored(cache_controller: CacheController, key: str) -> None:
    """Wait until the background store task has written the cache row."""
    for _ in range(200):
        if await _get_row(cache_controller, key):
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


async def _wait_for_flight(provider: _FakeProvider) -> None:
    """Wait until a gated call is running and every concurrent caller has joined it."""

    async def _started() -> bool:
        return provider.calls > 0

    await _wait_for(_started)
    # let the remaining callers reach the in-progress fetch; a caller arriving too late
    # would start a second one, which every call-count assertion below catches
    await asyncio.sleep(0.05)


# --- result caching (including None results) ---


async def test_result_served_from_cache(
    cache_controller: CacheController, provider: _FakeProvider
) -> None:
    """Test that a second call is served from cache without re-invoking the function."""
    provider.result = "value"
    assert await provider.fetch("a") == "value"
    assert provider.calls == 1
    await _wait_for_stored(cache_controller, "fetch.a")
    provider.result = "changed"
    assert await provider.fetch("a") == "value"
    assert provider.calls == 1


async def test_none_result_cached_and_served(
    cache_controller: CacheController, provider: _FakeProvider
) -> None:
    """Test that a None result is cached and served without re-invoking the function."""
    provider.result = None
    assert await provider.fetch("a") is None
    assert provider.calls == 1
    await _wait_for_stored(cache_controller, "fetch.a")
    assert await provider.fetch("a") is None
    assert provider.calls == 1


async def test_cache_none_false_retries_and_skips_store(
    cache_controller: CacheController, provider: _FakeProvider
) -> None:
    """Test that cache_none=False re-invokes on None and does not store the None result."""
    provider.result = None
    assert await provider.fetch_no_none("a") is None
    assert provider.calls == 1
    # give a (wrongly created) store task time to run, then verify nothing was written
    await asyncio.sleep(0.05)
    assert await _get_row(cache_controller, "fetch_no_none.a") is None
    assert await provider.fetch_no_none("a") is None
    assert provider.calls == 2
    # once the function returns a real value, it is cached again
    provider.result = "found"
    assert await provider.fetch_no_none("a") == "found"
    assert provider.calls == 3
    await _wait_for_stored(cache_controller, "fetch_no_none.a")
    assert await provider.fetch_no_none("a") == "found"
    assert provider.calls == 3


async def test_cache_none_false_ignores_stored_none(
    cache_controller: CacheController, provider: _FakeProvider
) -> None:
    """Test that cache_none=False treats a previously stored None row as a cache miss."""
    await cache_controller.set("fetch_no_none.a", None, provider=_PROVIDER)
    provider.result = "fresh"
    assert await provider.fetch_no_none("a") == "fresh"
    assert provider.calls == 1


# --- stale-while-revalidate ---


async def test_swr_serves_stale_and_refreshes(
    cache_controller: CacheController, provider: _FakeProvider
) -> None:
    """Test that an expired entry is served immediately and refreshed in the background."""
    await cache_controller.set(
        "fetch_swr.a", "stale", provider=_PROVIDER, expiration=-1, allow_expired_cache=True
    )
    provider.result = "fresh"
    assert await provider.fetch_swr("a") == "stale"

    async def _refreshed() -> bool:
        return bool(await cache_controller.get("fetch_swr.a", provider=_PROVIDER) == "fresh")

    await _wait_for(_refreshed)
    assert provider.calls == 1
    assert await provider.fetch_swr("a") == "fresh"
    assert provider.calls == 1


async def test_wrapper_performs_single_row_fetch(
    cache_controller: CacheController, provider: _FakeProvider
) -> None:
    """Test that one wrapper call does exactly one db row fetch (miss, fresh and stale)."""
    assert cache_controller.database is not None
    # cache miss
    provider.result = "value"
    with patch.object(
        cache_controller.database, "get_row", wraps=cache_controller.database.get_row
    ) as spy:
        assert await provider.fetch_swr("a") == "value"
    assert spy.await_count == 1
    # fresh hit
    await _wait_for_stored(cache_controller, "fetch_swr.a")
    with patch.object(
        cache_controller.database, "get_row", wraps=cache_controller.database.get_row
    ) as spy:
        assert await provider.fetch_swr("a") == "value"
    assert spy.await_count == 1
    # stale hit (previously fetched the same row twice)
    await cache_controller.set(
        "fetch_swr.b", "stale", provider=_PROVIDER, expiration=-1, allow_expired_cache=True
    )
    with patch.object(
        cache_controller.database, "get_row", wraps=cache_controller.database.get_row
    ) as spy:
        assert await provider.fetch_swr("b") == "stale"
    assert spy.await_count == 1


# --- single flight ---


async def test_concurrent_misses_share_one_fetch(
    cache_controller: CacheController, provider: _FakeProvider
) -> None:
    """Test that concurrent callers on the same key trigger one fetch and one store."""
    provider.result = "value"
    provider.gate.clear()
    with patch.object(cache_controller, "set", wraps=cache_controller.set) as store:
        tasks = [asyncio.create_task(provider.fetch("a")) for _ in range(3)]
        await _wait_for_flight(provider)
        provider.gate.set()
        assert await asyncio.gather(*tasks) == ["value", "value", "value"]
        assert provider.calls == 1
        await _wait_for_stored(cache_controller, "fetch.a")
    assert store.await_count == 1


async def test_each_caller_gets_its_own_result_object(provider: _FakeProvider) -> None:
    """Test that callers sharing one fetch do not share the result objects."""
    provider.gate.clear()
    tasks = [asyncio.create_task(provider.fetch_items("a")) for _ in range(3)]
    await _wait_for_flight(provider)
    provider.gate.set()
    results = await asyncio.gather(*tasks)
    assert provider.calls == 1
    assert all(result == [{"id": "a", "played": False}] for result in results)
    assert len({id(result) for result in results}) == 3
    assert len({id(result[0]) for result in results}) == 3
    # mutating one caller's payload, as the podcast resume-state code does, must not
    # be visible to the other callers
    results[0][0]["played"] = True
    assert results[1][0]["played"] is False
    assert results[2][0]["played"] is False


async def test_model_results_are_copied_per_caller(provider: _FakeProvider) -> None:
    """Test that model results handed to callers sharing a fetch are copies."""
    provider.gate.clear()
    tasks = [asyncio.create_task(provider.fetch_models("a")) for _ in range(3)]
    await _wait_for_flight(provider)
    provider.gate.set()
    results = await asyncio.gather(*tasks)
    assert provider.calls == 1
    assert all(result == [_Payload(item_id="a")] for result in results)
    assert len({id(result[0]) for result in results}) == 3
    results[0][0].played = True
    assert results[1][0].played is False
    assert results[2][0].played is False


async def test_caller_mutation_does_not_reach_the_other_callers(
    cache_controller: CacheController, provider: _FakeProvider
) -> None:
    """Test that a caller mutating its result right away cannot affect the others."""

    async def _fetch_and_mutate() -> list[dict[str, Any]]:
        result = await provider.fetch_items("a")
        # runs before the other callers resume, so a copy taken later would pick this up
        result[0]["played"] = True
        return result

    provider.gate.clear()
    tasks = [
        asyncio.create_task(_fetch_and_mutate()),
        asyncio.create_task(provider.fetch_items("a")),
        asyncio.create_task(provider.fetch_items("a")),
    ]
    await _wait_for_flight(provider)
    provider.gate.set()
    mutated, *others = await asyncio.gather(*tasks)
    assert provider.calls == 1
    assert mutated[0]["played"] is True
    assert [result[0]["played"] for result in others] == [False, False]
    # the entry is written from the fetched objects, which no caller was handed
    await _wait_for_stored(cache_controller, "fetch_items.a")
    assert await cache_controller.get("fetch_items.a", provider=_PROVIDER) == [
        {"id": "a", "played": False}
    ]


async def test_reentrant_call_on_the_same_key_completes(provider: _FakeProvider) -> None:
    """Test that a body calling back into itself for its own key does not wait on itself."""
    provider.result = "value"
    async with asyncio.timeout(5):
        assert await provider.fetch_reentrant("a") == "value"
    assert provider.calls == 2


async def test_result_shape_survives_being_copied(provider: _FakeProvider) -> None:
    """Test that callers sharing a fetch get the result shape the function returned."""
    provider.gate.clear()
    tasks = [asyncio.create_task(provider.fetch_tuples("a")) for _ in range(3)]
    await _wait_for_flight(provider)
    provider.gate.set()
    results = await asyncio.gather(*tasks)
    assert provider.calls == 1
    assert all(result == [("a", "name", None)] for result in results)


async def test_tuple_result_shape_survives_a_cache_hit(
    cache: CacheController, provider: _FakeProvider
) -> None:
    """Test that a cached tuple is rebuilt with all of its members, including the None ones."""
    assert await provider.fetch_tuples("a") == [("a", "name", None)]
    await _wait_for_stored(cache, "fetch_tuples.a")
    assert await provider.fetch_tuples("a") == [("a", "name", None)]
    assert provider.calls == 1


async def test_uncopyable_result_is_still_returned(
    provider: _FakeProvider, caplog: pytest.LogCaptureFixture
) -> None:
    """Test that callers still get a result when it cannot be copied."""
    provider.gate.clear()
    tasks = [asyncio.create_task(provider.fetch_uncopyable("a")) for _ in range(3)]
    await _wait_for_flight(provider)
    provider.gate.set()
    results = await asyncio.gather(*tasks)
    assert provider.calls == 1
    assert all(isinstance(result, _Uncopyable) for result in results)
    assert "Cannot copy the shared result" in caplog.text


async def test_cancelled_fetch_cancels_every_caller(provider: _FakeProvider) -> None:
    """Test that cancelling the shared fetch itself surfaces to all of its callers."""
    provider.gate.clear()
    tasks = [asyncio.create_task(provider.fetch("a")) for _ in range(3)]
    await _wait_for_flight(provider)
    provider.mass._tracked_tasks[f"cache_flight.{_PROVIDER}.fetch.a"].cancel()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert all(isinstance(result, asyncio.CancelledError) for result in results)


async def test_cancelled_caller_leaves_the_others_untouched(
    cache_controller: CacheController, provider: _FakeProvider
) -> None:
    """Test that one caller giving up neither cancels the fetch nor the other callers."""
    provider.result = "value"
    provider.gate.clear()
    tasks = [asyncio.create_task(provider.fetch("a")) for _ in range(3)]
    await _wait_for_flight(provider)
    tasks[0].cancel()
    provider.gate.set()
    assert await asyncio.gather(*tasks[1:]) == ["value", "value"]
    assert tasks[0].cancelled()
    assert provider.calls == 1
    await _wait_for_stored(cache_controller, "fetch.a")


async def test_sole_cancelled_caller_still_completes_the_fetch(
    cache_controller: CacheController, provider: _FakeProvider
) -> None:
    """Test that a fetch runs to completion and stores after its only caller gave up."""
    provider.result = "value"
    provider.gate.clear()
    task = asyncio.create_task(provider.fetch("a"))
    await _wait_for_flight(provider)
    task.cancel()
    provider.gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await _wait_for_stored(cache_controller, "fetch.a")
    assert provider.calls == 1


async def test_failing_fetch_logs_no_task_warning(
    provider: _FakeProvider, caplog: pytest.LogCaptureFixture
) -> None:
    """Test that a raising fetch stays quiet, even once its only caller has gone."""
    provider.error = MediaNotFoundError("not found")
    provider.gate.clear()
    task = asyncio.create_task(provider.fetch("a"))
    await _wait_for_flight(provider)
    task.cancel()
    caplog.clear()
    provider.gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.05)
    assert [
        record
        for record in caplog.records
        if record.levelno >= logging.WARNING and "Exception in task" in record.getMessage()
    ] == []


async def test_exception_is_shared_and_not_cached(
    cache_controller: CacheController, provider: _FakeProvider
) -> None:
    """Test that every caller gets the raised error and the next call fetches again."""
    provider.error = MediaNotFoundError("not found")
    provider.gate.clear()
    with patch.object(cache_controller, "set", AsyncMock()) as store:
        tasks = [asyncio.create_task(provider.fetch("a")) for _ in range(3)]
        await _wait_for_flight(provider)
        provider.gate.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        assert provider.calls == 1
        assert all(result is provider.error for result in results)
        assert store.await_count == 0
    provider.error = None
    provider.result = "value"
    assert await provider.fetch("a") == "value"
    assert provider.calls == 2


async def test_cache_none_false_shares_one_attempt(
    cache_controller: CacheController, provider: _FakeProvider
) -> None:
    """Test that concurrent callers share one attempt and one None, then retry after."""
    provider.result = None
    provider.gate.clear()
    with patch.object(cache_controller, "set", AsyncMock()) as store:
        tasks = [asyncio.create_task(provider.fetch_no_none("a")) for _ in range(3)]
        await _wait_for_flight(provider)
        provider.gate.set()
        assert await asyncio.gather(*tasks) == [None, None, None]
        assert provider.calls == 1
        assert store.await_count == 0
    assert await provider.fetch_no_none("a") is None
    assert provider.calls == 2


async def test_bypass_cache_does_not_join_a_fetch(provider: _FakeProvider) -> None:
    """Test that a BYPASS_CACHE caller fetches on its own instead of joining."""

    async def _bypassing() -> str | None:
        token = BYPASS_CACHE.set(True)
        try:
            return await provider.fetch("a")
        finally:
            BYPASS_CACHE.reset(token)

    provider.result = "value"
    provider.gate.clear()
    tasks = [asyncio.create_task(provider.fetch("a")), asyncio.create_task(_bypassing())]
    await _wait_for_flight(provider)
    provider.gate.set()
    assert await asyncio.gather(*tasks) == ["value", "value"]
    assert provider.calls == 2


async def test_swr_refreshes_once_for_concurrent_callers(
    cache_controller: CacheController, provider: _FakeProvider
) -> None:
    """Test that concurrent callers on a stale entry trigger one background refresh."""
    await cache_controller.set(
        "fetch_swr.a", "stale", provider=_PROVIDER, expiration=-1, allow_expired_cache=True
    )
    provider.result = "fresh"
    provider.gate.clear()
    tasks = [asyncio.create_task(provider.fetch_swr("a")) for _ in range(3)]
    assert await asyncio.gather(*tasks) == ["stale", "stale", "stale"]
    await _wait_for_flight(provider)
    provider.gate.set()

    async def _refreshed() -> bool:
        return bool(await cache_controller.get("fetch_swr.a", provider=_PROVIDER) == "fresh")

    await _wait_for(_refreshed)
    assert provider.calls == 1


async def test_completed_fetch_is_not_reused(
    cache_controller: CacheController, provider: _FakeProvider
) -> None:
    """Test that a caller is not handed a fetch that already finished."""

    async def _noop() -> None:
        return None

    # a finished flight left under the key must be replaced, not awaited for its outcome
    finished = asyncio.create_task(_noop())
    await finished
    cache_controller.mass._tracked_tasks[f"cache_flight.{_PROVIDER}.fetch.a"] = finished
    provider.result = "value"
    assert await provider.fetch("a") == "value"
    assert provider.calls == 1


async def test_single_flight_disabled_runs_every_caller(provider: _FakeProvider) -> None:
    """Test that single_flight=False lets every concurrent caller run the function."""
    provider.result = "value"
    provider.gate.clear()
    tasks = [asyncio.create_task(provider.fetch_no_flight("a")) for _ in range(3)]
    await _wait_for_flight(provider)
    provider.gate.set()
    assert await asyncio.gather(*tasks) == ["value", "value", "value"]
    assert provider.calls == 3


# --- get_with_freshness ---


async def test_get_with_freshness(cache_controller: CacheController) -> None:
    """Test that get_with_freshness reports the freshness and presence of entries."""
    await cache_controller.set("fresh", "data", provider=_PROVIDER, expiration=3600)
    assert await cache_controller.get_with_freshness("fresh", provider=_PROVIDER) == (
        "data",
        True,
        True,
    )
    await cache_controller.set("expired", "old", provider=_PROVIDER, expiration=-1)
    # an expired entry is reported as not found unless include_expired is set
    assert await cache_controller.get_with_freshness("expired", provider=_PROVIDER) == (
        None,
        False,
        False,
    )
    assert await cache_controller.get_with_freshness(
        "expired", provider=_PROVIDER, include_expired=True
    ) == (
        "old",
        False,
        True,
    )
    data, is_fresh, found = await cache_controller.get_with_freshness("missing", provider=_PROVIDER)
    assert found is False
    assert is_fresh is False
    assert data is None
