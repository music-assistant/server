"""
Warm-cache regression test for the recommendation-items path.

The heavy 'Inspired by recently played' build is cached at the folder level
(``_get_inspired_recommendations``, ``@use_cache(base_class=RecommendationFolder)``);
``get_recommendation_items`` itself stays undecorated. With the decorator placed
directly on ``get_recommendation_items`` (no base_class), the cache hit path
reconstructed via the ``UniqueList[...]`` return annotation, which parse_value
cannot handle — every row served its items exactly once, then failed for the
whole TTL. This test locks in that a second call within the TTL is served from
the stored cache entry without a second backend fetch.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.providers.sonic_similarity.conftest import make_item_mapping, make_track

if TYPE_CHECKING:
    from collections.abc import Callable

ROW_ID = "inspired_by_recently_played"


@pytest.mark.asyncio
async def test_get_recommendation_items_warm_cache_hit_serves_stored_items(
    make_plugin: Callable[..., Any], mock_mass: MagicMock
) -> None:
    """A second items call within the TTL is served from the cache, not the backend."""
    cache_store: dict[str, Any] = {}
    background_tasks: list[asyncio.Future[Any]] = []

    async def _cache_get(key: str, **_kwargs: Any) -> tuple[Any, bool, bool]:
        # production get_with_freshness reconstructs a base_class entry via
        # from_dict before handing it to the decorator; the dict-backed fake
        # returns what cache.set stored, which is the same shape
        if key in cache_store:
            return cache_store[key], True, True
        return None, False, False

    async def _cache_set(key: str, data: Any, **_kwargs: Any) -> None:
        cache_store[key] = data

    def _create_task(target: Any, **_kwargs: Any) -> asyncio.Future[Any]:
        task: asyncio.Future[Any] = asyncio.ensure_future(target)
        background_tasks.append(task)
        return task

    mock_mass.cache.get_with_freshness = AsyncMock(side_effect=_cache_get)
    mock_mass.cache.set = AsyncMock(side_effect=_cache_set)
    mock_mass.create_task = MagicMock(side_effect=_create_task)

    plugin = make_plugin(signatures={("spotify", "seed1"): [0.1] * 18})
    mock_mass.music.recently_played = AsyncMock(return_value=[make_item_mapping("recent1")])
    recent_track = make_track("seed1", provider="spotify")
    resolved = make_track("r1")

    async def _fake_get(item_id: str, _provider: str, **_kwargs: Any) -> MagicMock:
        return recent_track if item_id == "recent1" else resolved

    mock_mass.music.tracks.get = AsyncMock(side_effect=_fake_get)
    plugin._handle_similar = AsyncMock(
        return_value={"items": [{"item_id": "r1", "provider": "spotify", "distance": 0.3}]}
    )

    first = await plugin.get_recommendation_items(ROW_ID)
    # let the background cache-store task complete before the second call
    await asyncio.gather(*background_tasks)
    second = await plugin.get_recommendation_items(ROW_ID)

    # the backend was fetched exactly once: the warm call came from the cache
    assert mock_mass.music.recently_played.await_count == 1
    assert plugin._handle_similar.await_count == 1
    assert list(first) == [resolved]
    assert list(second) == [resolved]
