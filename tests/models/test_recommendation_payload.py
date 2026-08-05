"""Tests for the RecommendationPayloadMixin cached-payload helper."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest
from music_assistant_models.enums import ImageType, MediaType, RecommendationFolderType
from music_assistant_models.media_items import ItemMapping, MediaItemImage, RecommendationFolder
from music_assistant_models.unique_list import UniqueList

from music_assistant.models.recommendation_payload import (
    _PAYLOAD_CACHE_KEY,
    RecommendationPayloadMixin,
)
from tests.common import collect_loop_errors

INSTANCE_ID = "test_payload--instance1"


def _payload_dicts(payload: list[RecommendationFolder]) -> list[dict[str, Any]]:
    """
    Serialize folders to dicts for equality checks across distinct instances.

    RecommendationFolder inherits an __eq__ that only accepts MediaItem/ItemMapping
    instances (BrowseFolder, its own base, is neither), so two structurally-identical
    but distinct RecommendationFolder objects always compare unequal - only the exact
    same object survives `==`. Comparing to_dict() output instead asserts the fields
    actually match, which is what a persisted-cache round trip (a fresh reconstruction
    via from_dict) needs.
    """
    return [folder.to_dict() for folder in payload]


class _UnloadableBase:
    """Stands in for the Provider base at the end of the cooperative unload chain."""

    unload_chain_called = False

    async def unload(self, is_removed: bool = False) -> None:
        self.unload_chain_called = True


class _PayloadProvider(RecommendationPayloadMixin, _UnloadableBase):
    """Minimal host implementing the mixin's requirements, with a dict-backed fake cache."""

    # the mixin is typed against the real Provider base; the fake overrides its
    # (final) identity properties with plain values, which is fine at runtime
    domain = "test_payload"  # type: ignore[misc]
    instance_id = INSTANCE_ID  # type: ignore[misc]

    def __init__(self, fetch: AsyncMock) -> None:
        self.logger = logging.getLogger(__name__)
        self._fetch = fetch
        self._cache_store: dict[str, Any] = {}
        # freshness the fake cache reports for a hit; set False to simulate an
        # expired persistent entry (returned as stale data, is_fresh=False)
        self.cache_is_fresh = True
        self.background_tasks: list[asyncio.Future[Any]] = []
        self.mass = Mock()
        self.mass.cache.get_with_freshness = AsyncMock(side_effect=self._cache_get)
        self.mass.cache.set = AsyncMock(side_effect=self._cache_set)
        self.mass.create_task = Mock(side_effect=self._create_task)

    async def _fetch_recommendation_payload(self) -> list[RecommendationFolder]:
        result: list[RecommendationFolder] = await self._fetch()
        return result

    def seed_cache(self, payload: list[RecommendationFolder]) -> None:
        """Pre-fill the persistent cache store, as a previous run's store task would."""
        self._cache_store[_PAYLOAD_CACHE_KEY] = _payload_dicts(payload)

    async def _cache_get(self, key: str, **kwargs: Any) -> tuple[Any, bool, bool]:
        if key not in self._cache_store:
            return None, False, False
        data = self._cache_store[key]
        base_class = kwargs.get("base_class")
        if base_class is not None and data is not None:
            # mirrors production get_with_freshness: a list of dicts reconstructs as a
            # list of base_class instances, a single dict as one base_class instance
            if isinstance(data, list):
                return [base_class.from_dict(item) for item in data], self.cache_is_fresh, True
            return base_class.from_dict(data), self.cache_is_fresh, True
        return data, self.cache_is_fresh, True

    async def _cache_set(self, key: str, data: Any, **kwargs: Any) -> None:
        # mirrors production: entries are stored serialized (to_dict), reconstructed
        # via base_class.from_dict above on a hit
        if isinstance(data, list):
            self._cache_store[key] = [item.to_dict() for item in data]
        elif data is not None:
            self._cache_store[key] = data.to_dict()
        else:
            self._cache_store[key] = None

    def _create_task(self, target: Any, *args: Any, **kwargs: Any) -> asyncio.Future[Any]:
        task: asyncio.Future[Any] = asyncio.ensure_future(target)
        self.background_tasks.append(task)
        return task


async def _drain_background(provider: _PayloadProvider) -> None:
    """Let all background tasks finish, including tasks they spawn while draining."""
    while pending := [task for task in provider.background_tasks if not task.done()]:
        await asyncio.gather(*pending, return_exceptions=True)


def _make_payload() -> list[RecommendationFolder]:
    """Build a two-folder payload with items and all identity/presentation fields set."""
    return [
        RecommendationFolder(
            item_id=f"{INSTANCE_ID}_editorial",
            provider=INSTANCE_ID,
            name="Editorial",
            translation_key="editorial_picks",
            icon="mdi-star",
            subtitle="Picked for you",
            enabled_by_default=False,
            is_playable=True,
            media_type=MediaType.PLAYLIST,
            type=RecommendationFolderType.TIMELINE,
            image=MediaItemImage(
                type=ImageType.THUMB,
                path="http://backend/editorial.jpg",
                provider=INSTANCE_ID,
                remotely_accessible=True,
            ),
            items=UniqueList(
                [
                    ItemMapping(
                        media_type=MediaType.TRACK,
                        item_id="t1",
                        provider=INSTANCE_ID,
                        name="Track One",
                    )
                ]
            ),
        ),
        RecommendationFolder(
            item_id=f"{INSTANCE_ID}_charts",
            provider=INSTANCE_ID,
            name="Charts",
            items=UniqueList(
                [
                    ItemMapping(
                        media_type=MediaType.PLAYLIST,
                        item_id="p1",
                        provider=INSTANCE_ID,
                        name="Chart Playlist",
                    )
                ]
            ),
        ),
    ]


def _make_fresh_payload() -> list[RecommendationFolder]:
    """Build a single-folder payload distinct from _make_payload, for refresh scenarios."""
    return [
        RecommendationFolder(
            item_id=f"{INSTANCE_ID}_fresh",
            provider=INSTANCE_ID,
            name="Fresh",
            items=UniqueList(
                [
                    ItemMapping(
                        media_type=MediaType.TRACK,
                        item_id="t2",
                        provider=INSTANCE_ID,
                        name="Track Two",
                    )
                ]
            ),
        )
    ]


@pytest.mark.asyncio
async def test_rows_and_items_share_one_payload_fetch() -> None:
    """Rows and two items calls are served from a single backend fetch via memory."""
    payload = _make_payload()
    fetch = AsyncMock(return_value=payload)
    provider = _PayloadProvider(fetch)

    rows = await provider._recommendation_rows_from_payload()
    items_editorial = await provider._recommendation_items_from_payload(f"{INSTANCE_ID}_editorial")
    items_charts = await provider._recommendation_items_from_payload(f"{INSTANCE_ID}_charts")

    fetch.assert_awaited_once()
    assert [row.item_id for row in rows] == [
        f"{INSTANCE_ID}_editorial",
        f"{INSTANCE_ID}_charts",
    ]
    # warm calls serve the payload straight from memory: the very objects the
    # backend fetch returned, with no cache-db round trip / re-deserialization
    assert items_editorial is payload[0].items
    assert items_charts is payload[1].items
    # the fetch also stored the payload persistently, under the legacy cache key
    await _drain_background(provider)
    assert provider._cache_store[_PAYLOAD_CACHE_KEY] == _payload_dicts(payload)


@pytest.mark.asyncio
async def test_cold_start_rows_then_items_before_store_lands_fetches_once() -> None:
    """The cold-start rows->items sequence does one fetch even if the store hasn't landed."""
    payload = _make_payload()
    fetch = AsyncMock(return_value=payload)
    provider = _PayloadProvider(fetch)
    # hold back the persistent store so the items call cannot be served by the cache db
    store_gate = asyncio.Event()
    plain_set = provider._cache_set

    async def _gated_set(key: str, data: Any, **kwargs: Any) -> None:
        await store_gate.wait()
        await plain_set(key, data, **kwargs)

    provider.mass.cache.set = AsyncMock(side_effect=_gated_set)  # type: ignore[method-assign]

    rows = await provider._recommendation_rows_from_payload()
    items = await provider._recommendation_items_from_payload(f"{INSTANCE_ID}_editorial")

    fetch.assert_awaited_once()
    assert len(rows) == 2
    assert items is payload[0].items

    store_gate.set()
    await _drain_background(provider)
    assert provider._cache_store[_PAYLOAD_CACHE_KEY] == _payload_dicts(payload)


@pytest.mark.asyncio
async def test_single_flight_concurrent_cold_calls_fetch_once() -> None:
    """N concurrent cold callers share one in-flight backend fetch."""
    payload = _make_payload()
    gate = asyncio.Event()

    async def _gated_fetch() -> list[RecommendationFolder]:
        await gate.wait()
        return payload

    fetch = AsyncMock(side_effect=_gated_fetch)
    provider = _PayloadProvider(fetch)

    tasks = [asyncio.create_task(provider._recommendation_payload()) for _ in range(5)]
    await asyncio.sleep(0)
    gate.set()
    results = await asyncio.gather(*tasks)

    assert fetch.await_count == 1
    assert all(result == payload for result in results)


@pytest.mark.asyncio
async def test_unknown_item_id_returns_empty() -> None:
    """An item_id not present in the payload yields an empty UniqueList."""
    fetch = AsyncMock(return_value=_make_payload())
    provider = _PayloadProvider(fetch)

    result = await provider._recommendation_items_from_payload("bogus_row")

    assert isinstance(result, UniqueList)
    assert len(result) == 0


@pytest.mark.asyncio
async def test_rows_have_empty_items_but_preserve_all_fields() -> None:
    """Rows are stripped of items while keeping every other field of the payload folder."""
    payload = _make_payload()
    fetch = AsyncMock(return_value=payload)
    provider = _PayloadProvider(fetch)

    rows = await provider._recommendation_rows_from_payload()

    editorial = rows[0]
    assert len(editorial.items) == 0
    assert editorial.item_id == f"{INSTANCE_ID}_editorial"
    assert editorial.provider == INSTANCE_ID
    assert editorial.name == "Editorial"
    assert editorial.translation_key == "editorial_picks"
    assert editorial.icon == "mdi-star"
    assert editorial.subtitle == "Picked for you"
    assert editorial.enabled_by_default is False
    # presentation/behavior fields survive the copy too (regression: an explicit
    # field-by-field copy silently dropped these)
    assert editorial.is_playable is True
    assert editorial.media_type == MediaType.PLAYLIST
    assert editorial.type == RecommendationFolderType.TIMELINE
    assert editorial.image is not None
    assert editorial.image.path == "http://backend/editorial.jpg"
    # stripping produced fresh copies: the payload folders keep their items
    assert editorial is not payload[0]
    assert len(payload[0].items) == 1


@pytest.mark.asyncio
async def test_stale_persistent_entry_served_with_single_background_refresh() -> None:
    """A stale persisted payload is served immediately while exactly one refresh runs."""
    stale = _make_payload()
    fresh = _make_fresh_payload()
    gate = asyncio.Event()

    async def _gated_fetch() -> list[RecommendationFolder]:
        await gate.wait()
        return fresh

    fetch = AsyncMock(side_effect=_gated_fetch)
    provider = _PayloadProvider(fetch)
    provider.seed_cache(stale)
    provider.cache_is_fresh = False

    # the stale payload is served without waiting for the backend
    rows = await provider._recommendation_rows_from_payload()
    assert [row.item_id for row in rows] == [folder.item_id for folder in stale]
    # exactly one background refresh was scheduled and is now blocked in the backend call
    await asyncio.sleep(0)
    assert fetch.await_count == 1
    # a second call while the refresh runs serves stale data and schedules no duplicate
    items = await provider._recommendation_items_from_payload(f"{INSTANCE_ID}_editorial")
    assert [item.item_id for item in items] == ["t1"]
    await asyncio.sleep(0)
    assert fetch.await_count == 1

    gate.set()
    await _drain_background(provider)

    # the refreshed payload replaced both the in-memory payload and the cache entry
    assert await provider._recommendation_payload() == fresh
    assert provider._cache_store[_PAYLOAD_CACHE_KEY] == _payload_dicts(fresh)
    assert fetch.await_count == 1


@pytest.mark.asyncio
async def test_fresh_persistent_entry_serves_cold_instance_without_fetch() -> None:
    """A fresh persisted payload warms a cold instance without any backend fetch."""
    payload = _make_payload()
    fetch = AsyncMock(return_value=payload)
    provider = _PayloadProvider(fetch)
    provider.seed_cache(payload)

    rows = await provider._recommendation_rows_from_payload()
    items = await provider._recommendation_items_from_payload(f"{INSTANCE_ID}_editorial")

    fetch.assert_not_awaited()
    # the cache db was read exactly once; the items call was served from memory
    cast("AsyncMock", provider.mass.cache.get_with_freshness).assert_awaited_once()
    assert [row.item_id for row in rows] == [folder.item_id for folder in payload]
    assert [item.item_id for item in items] == ["t1"]


@pytest.mark.asyncio
async def test_refresh_fetches_fresh_and_stores() -> None:
    """_refresh_recommendation_payload bypasses cached data and updates memory + cache."""
    stale = _make_payload()
    fetch = AsyncMock(return_value=stale)
    provider = _PayloadProvider(fetch)
    # warm memory and the persistent cache with the stale payload
    await provider._recommendation_payload()
    await _drain_background(provider)
    fresh = _make_fresh_payload()
    fetch.return_value = fresh

    result = await provider._refresh_recommendation_payload()

    assert result == fresh
    assert fetch.await_count == 2
    await _drain_background(provider)
    # the refresh stored under the same (legacy) key, replacing the stale entry
    assert set(provider._cache_store) == {_PAYLOAD_CACHE_KEY}
    assert provider._cache_store[_PAYLOAD_CACHE_KEY] == _payload_dicts(fresh)
    # subsequent payload calls serve the refreshed payload from memory, no new fetch
    assert await provider._recommendation_payload() == fresh
    assert fetch.await_count == 2


@pytest.mark.asyncio
async def test_refresh_raising_fetch_does_not_store_and_does_not_poison_retry() -> None:
    """A raising refresh propagates without storing; cached data still serves, and retries."""
    stale = _make_payload()
    fetch = AsyncMock(return_value=stale)
    provider = _PayloadProvider(fetch)
    # warm memory and the persistent cache with the stale payload
    await provider._recommendation_payload()
    await _drain_background(provider)
    cast("AsyncMock", provider.mass.cache.set).reset_mock()

    fetch.side_effect = RuntimeError("backend down")

    with pytest.raises(RuntimeError, match="backend down"):
        await provider._refresh_recommendation_payload()

    cast("AsyncMock", provider.mass.cache.set).assert_not_awaited()
    # the previously fetched payload still serves from memory
    assert await provider._recommendation_payload() == stale
    assert fetch.await_count == 2

    fresh = _make_fresh_payload()
    fetch.side_effect = None
    fetch.return_value = fresh

    result = await provider._refresh_recommendation_payload()

    assert result == fresh
    await _drain_background(provider)
    cast("AsyncMock", provider.mass.cache.set).assert_awaited_once()
    assert await provider._recommendation_payload() == fresh
    assert fetch.await_count == 3


@pytest.mark.asyncio
async def test_refresh_single_flight_concurrent_calls_fetch_once() -> None:
    """Concurrent refresh callers share one in-flight backend fetch."""
    payload = _make_payload()
    gate = asyncio.Event()

    async def _gated_fetch() -> list[RecommendationFolder]:
        await gate.wait()
        return payload

    fetch = AsyncMock(side_effect=_gated_fetch)
    provider = _PayloadProvider(fetch)

    tasks = [asyncio.create_task(provider._refresh_recommendation_payload()) for _ in range(5)]
    await asyncio.sleep(0)
    gate.set()
    results = await asyncio.gather(*tasks)

    assert fetch.await_count == 1
    assert all(result == payload for result in results)


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_cancel_shared_fetch() -> None:
    """
    A timed-out caller leaves the shared fetch alone: other waiters still get the payload.

    Regression test: the controller's asyncio.timeout cancels the calling task; if that
    cancellation propagated into the shared single-flight task it would raise
    CancelledError in every other waiter and prevent the cache from warming.
    """
    payload = _make_payload()
    gate = asyncio.Event()

    async def _gated_fetch() -> list[RecommendationFolder]:
        await gate.wait()
        return payload

    fetch = AsyncMock(side_effect=_gated_fetch)
    provider = _PayloadProvider(fetch)

    fast_caller = asyncio.create_task(provider._recommendation_payload())
    slow_caller = asyncio.create_task(provider._recommendation_payload())
    await asyncio.sleep(0)
    # simulate the rows call timing out while the fetch is still in flight
    fast_caller.cancel()
    gate.set()

    assert await slow_caller == payload
    with pytest.raises(asyncio.CancelledError):
        await fast_caller
    # the shared fetch completed exactly once despite the cancelled waiter
    fetch.assert_awaited_once()


@pytest.mark.asyncio
async def test_sole_cancelled_waiter_fetch_still_warms_memory_and_cache() -> None:
    """A cold fetch whose only waiter is cancelled still completes and warms memory + cache."""
    payload = _make_payload()
    gate = asyncio.Event()

    async def _gated_fetch() -> list[RecommendationFolder]:
        await gate.wait()
        return payload

    fetch = AsyncMock(side_effect=_gated_fetch)
    provider = _PayloadProvider(fetch)

    sole_caller = asyncio.create_task(provider._recommendation_payload())
    await asyncio.sleep(0)
    # the rows timeout case: the only waiter is cancelled mid-fetch
    sole_caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await sole_caller

    gate.set()
    await _drain_background(provider)

    # the fetch completed anyway: memory and cache are warm, no second fetch
    assert await provider._recommendation_payload() == payload
    fetch.assert_awaited_once()
    assert provider._cache_store[_PAYLOAD_CACHE_KEY] == _payload_dicts(payload)


@pytest.mark.asyncio
async def test_failed_fetch_propagates_to_all_waiters_and_retries() -> None:
    """A failing fetch raises in every concurrent waiter and does not poison later calls."""
    payload = _make_payload()
    gate = asyncio.Event()

    async def _failing_fetch() -> list[RecommendationFolder]:
        await gate.wait()
        raise RuntimeError("backend down")

    fetch = AsyncMock(side_effect=_failing_fetch)
    provider = _PayloadProvider(fetch)

    tasks = [asyncio.create_task(provider._recommendation_payload()) for _ in range(3)]
    await asyncio.sleep(0)
    gate.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)

    assert len(results) == 3
    assert all(isinstance(result, RuntimeError) for result in results)

    fetch.side_effect = None
    fetch.return_value = payload
    assert await provider._recommendation_payload() == payload
    assert fetch.await_count == 2


@pytest.mark.asyncio
async def test_cancelled_waiter_of_a_failing_fetch_logs_no_loop_error() -> None:
    """A fetch failing after a caller timed out is not reported to the loop handler."""
    gate = asyncio.Event()

    async def _failing_fetch() -> list[RecommendationFolder]:
        await gate.wait()
        raise RuntimeError("backend down")

    provider = _PayloadProvider(AsyncMock(side_effect=_failing_fetch))

    with collect_loop_errors() as reported:
        fast_caller = asyncio.create_task(provider._recommendation_payload())
        slow_caller = asyncio.create_task(provider._recommendation_payload())
        await asyncio.sleep(0)
        # simulate the rows call timing out before the fetch fails
        fast_caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await fast_caller
        gate.set()
        with pytest.raises(RuntimeError, match="backend down"):
            await slow_caller

    assert reported == []


@pytest.mark.asyncio
async def test_unload_cancels_inflight_cold_fetch_and_continues_chain() -> None:
    """unload() cancels a hanging cold fetch and still runs the provider unload chain."""
    started = asyncio.Event()

    async def _hanging_fetch() -> list[RecommendationFolder]:
        started.set()
        await asyncio.Event().wait()
        return []

    provider = _PayloadProvider(AsyncMock(side_effect=_hanging_fetch))
    caller = asyncio.create_task(provider._recommendation_payload())
    await started.wait()
    task = provider._recommendation_payload_task
    assert task is not None

    await provider.unload()

    assert provider.unload_chain_called is True
    # the fetch was cancelled, awaited and its handle cleared before the chain continued
    assert task.cancelled()
    assert provider._recommendation_payload_task is None
    with pytest.raises(asyncio.CancelledError):
        await caller


@pytest.mark.asyncio
async def test_unload_cancels_background_refresh() -> None:
    """unload() cancels an in-flight stale-payload background refresh."""
    stale = _make_payload()
    started = asyncio.Event()

    async def _hanging_fetch() -> list[RecommendationFolder]:
        started.set()
        await asyncio.Event().wait()
        return []

    provider = _PayloadProvider(AsyncMock(side_effect=_hanging_fetch))
    provider.seed_cache(stale)
    provider.cache_is_fresh = False

    # serves the stale payload and schedules the (hanging) background refresh
    assert _payload_dicts(await provider._recommendation_payload()) == _payload_dicts(stale)
    await started.wait()
    task = provider._recommendation_refresh_task
    assert task is not None

    await provider.unload()

    assert provider.unload_chain_called is True
    assert task.cancelled()
    assert provider._recommendation_refresh_task is None


@pytest.mark.asyncio
async def test_unload_without_inflight_tasks_is_a_noop() -> None:
    """unload() on an idle provider just continues the unload chain."""
    provider = _PayloadProvider(AsyncMock(return_value=[]))

    await provider.unload()

    assert provider.unload_chain_called is True
