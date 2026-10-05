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
from music_assistant_models.media_items import ProviderMapping, Track

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

    async def _cache_get(key: str, **kwargs: Any) -> tuple[Any, bool, bool]:
        # mirrors production: entries are stored serialized (to_dict) and, since the
        # caller passes base_class=RecommendationFolder, reconstructed via from_dict
        # before being handed back to the decorator
        if key not in cache_store:
            return None, False, False
        data = cache_store[key]
        base_class = kwargs.get("base_class")
        if base_class is not None and data is not None:
            return base_class.from_dict(data), True, True
        return data, True, True

    async def _cache_set(key: str, data: Any, **_kwargs: Any) -> None:
        cache_store[key] = data.to_dict() if data is not None else None

    def _create_task(target: Any, **_kwargs: Any) -> asyncio.Future[Any]:
        task: asyncio.Future[Any] = asyncio.ensure_future(target)
        background_tasks.append(task)
        return task

    mock_mass.cache.get_with_freshness = AsyncMock(side_effect=_cache_get)
    mock_mass.cache.set = AsyncMock(side_effect=_cache_set)
    mock_mass.create_task = MagicMock(side_effect=_create_task)

    plugin = make_plugin(signatures={("spotify", "seed1"): [0.1] * 18})
    mock_mass.music.recently_played = AsyncMock(return_value=[make_item_mapping("recent1")])
    # the seed track is only walked structurally (never cached), so the make_track
    # MagicMock double is fine here; the resolved candidate ends up in folder.items
    # and must survive a real to_dict/from_dict round trip, so it needs to be a
    # genuine Track instance rather than a MagicMock double
    recent_track = make_track("seed1", provider="spotify")
    resolved = Track(
        item_id="r1",
        provider="spotify",
        name="Track r1",
        provider_mappings={
            ProviderMapping(item_id="r1", provider_domain="spotify", provider_instance="spotify")
        },
    )

    async def _fake_get(item_id: str, _provider: str, **_kwargs: Any) -> MagicMock | Track:
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
