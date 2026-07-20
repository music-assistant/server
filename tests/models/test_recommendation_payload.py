"""Tests for the RecommendationPayloadMixin cached-payload helper."""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import ItemMapping, RecommendationFolder
from music_assistant_models.unique_list import UniqueList

from music_assistant.models.recommendation_payload import RecommendationPayloadMixin

INSTANCE_ID = "test_payload--instance1"


class _PayloadProvider(RecommendationPayloadMixin):
    """Minimal host implementing the mixin's requirements, with a dict-backed fake cache."""

    domain = "test_payload"
    instance_id = INSTANCE_ID

    def __init__(self, fetch: AsyncMock) -> None:
        self.logger = logging.getLogger(__name__)
        self._fetch = fetch
        self._cache_store: dict[str, Any] = {}
        self.background_tasks: list[asyncio.Future[Any]] = []
        self.mass = Mock()
        self.mass.cache.get_with_freshness = AsyncMock(side_effect=self._cache_get)
        self.mass.cache.set = AsyncMock(side_effect=self._cache_set)
        self.mass.create_task = Mock(side_effect=self._create_task)

    async def _fetch_recommendation_payload(self) -> list[RecommendationFolder]:
        result: list[RecommendationFolder] = await self._fetch()
        return result

    async def _cache_get(self, key: str, **kwargs: Any) -> tuple[Any, bool, bool]:
        if key in self._cache_store:
            return self._cache_store[key], True, True
        return None, False, False

    async def _cache_set(self, key: str, data: Any, **kwargs: Any) -> None:
        self._cache_store[key] = data

    def _create_task(self, target: Any, *args: Any, **kwargs: Any) -> asyncio.Future[Any]:
        task: asyncio.Future[Any] = asyncio.ensure_future(target)
        self.background_tasks.append(task)
        return task


def _make_payload() -> list[RecommendationFolder]:
    """Build a two-folder payload with items and all identity fields set."""
    return [
        RecommendationFolder(
            item_id=f"{INSTANCE_ID}_editorial",
            provider=INSTANCE_ID,
            name="Editorial",
            translation_key="editorial_picks",
            icon="mdi-star",
            subtitle="Picked for you",
            enabled_by_default=False,
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


@pytest.mark.asyncio
async def test_rows_and_items_share_one_payload_fetch() -> None:
    """Rows and two items calls are served from a single backend fetch via the cache."""
    payload = _make_payload()
    fetch = AsyncMock(return_value=payload)
    provider = _PayloadProvider(fetch)

    rows = await provider._recommendation_rows_from_payload()
    # let the background cache-store task complete before the next calls
    await asyncio.gather(*provider.background_tasks)
    items_editorial = await provider._recommendation_items_from_payload(f"{INSTANCE_ID}_editorial")
    items_charts = await provider._recommendation_items_from_payload(f"{INSTANCE_ID}_charts")

    fetch.assert_awaited_once()
    assert [row.item_id for row in rows] == [
        f"{INSTANCE_ID}_editorial",
        f"{INSTANCE_ID}_charts",
    ]
    assert items_editorial == payload[0].items
    assert items_charts == payload[1].items


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
async def test_rows_have_empty_items_but_preserve_identity() -> None:
    """Rows are stripped of items while keeping all identity fields of the payload folder."""
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
    # stripping produced fresh copies: the payload folders keep their items
    assert editorial is not payload[0]
    assert len(payload[0].items) == 1


@pytest.mark.asyncio
async def test_refresh_fetches_fresh_and_stores_through_cached_path() -> None:
    """_refresh_recommendation_payload bypasses the cached read and updates the cache entry."""
    stale = _make_payload()
    fetch = AsyncMock(return_value=stale)
    provider = _PayloadProvider(fetch)
    # warm the cache with the stale payload
    await provider._recommendation_payload()
    await asyncio.gather(*provider.background_tasks)
    fresh = [
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
    fetch.return_value = fresh

    result = await provider._refresh_recommendation_payload()

    assert result == fresh
    assert fetch.await_count == 2
    # the refresh stored under the same key the @use_cache decorator maintains,
    # so subsequent cached reads serve the fresh payload without another fetch
    assert set(provider._cache_store) == {"_cached_recommendation_payload"}
    assert await provider._recommendation_payload() == fresh
    assert fetch.await_count == 2


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
    A timed-out caller is shielded from the shared fetch: other waiters still get the payload.

    Regression test: the controller's asyncio.timeout cancels the calling task; without
    asyncio.shield that cancellation would propagate into the shared single-flight task,
    raising CancelledError in every other waiter and preventing the cache from warming.
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
