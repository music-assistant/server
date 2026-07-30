"""Tests for the Soundcloud two-method recommendations contract."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from music_assistant_models.unique_list import UniqueList

from music_assistant.providers.soundcloud import SoundcloudMusicProvider

MIXED_DATA = {
    "collection": [
        {
            "id": "trending-music",
            "title": "Trending",
            "items": {
                "collection": [
                    {"kind": "system-playlist", "id": "sp1", "title": "Daily Mix"},
                ]
            },
        },
    ]
}
FEED_DATA = {
    "collection": [
        {
            "type": "track",
            "track": {
                "id": 111,
                "title": "Feed Track",
                "duration": 180000,
                "permalink_url": "https://soundcloud.com/artist/feed-track",
                "user": {"id": 42},
            },
        },
    ]
}
USER_DATA = {"id": 42, "username": "Artist", "permalink": "artist"}


def _stub_api(provider: SoundcloudMusicProvider) -> Mock:
    """Attach a stubbed soundcloud client with canned responses."""
    api = Mock()
    api.get_mixed_selection = AsyncMock(return_value=MIXED_DATA)
    api.get_subscribe_feed = AsyncMock(return_value=FEED_DATA)
    api.get_user_details = AsyncMock(return_value=USER_DATA)
    provider._soundcloud = api
    return api


def _install_fake_cache(provider: SoundcloudMusicProvider) -> list[asyncio.Future[Any]]:
    """Back @use_cache with a dict store; return the background store tasks."""
    store: dict[str, Any] = {}
    background_tasks: list[asyncio.Future[Any]] = []

    async def _cache_get(key: str, **_kwargs: Any) -> tuple[Any, bool, bool]:
        if key in store:
            return store[key], True, True
        return None, False, False

    async def _cache_set(key: str, data: Any, **_kwargs: Any) -> None:
        store[key] = data

    def _create_task(target: Any, *_args: Any, **_kwargs: Any) -> asyncio.Future[Any]:
        task: asyncio.Future[Any] = asyncio.ensure_future(target)
        background_tasks.append(task)
        return task

    provider.mass.cache.get_with_freshness = AsyncMock(  # type: ignore[method-assign]
        side_effect=_cache_get
    )
    provider.mass.cache.set = AsyncMock(side_effect=_cache_set)  # type: ignore[method-assign]
    provider.mass.create_task = Mock(side_effect=_create_task)  # type: ignore[method-assign]
    return background_tasks


@pytest.mark.asyncio
async def test_get_recommendations_returns_rows_without_items(
    provider: SoundcloudMusicProvider,
) -> None:
    """get_recommendations returns payload rows plus the static feed row, all without items."""
    background_tasks = _install_fake_cache(provider)
    api = _stub_api(provider)

    rows = await provider.get_recommendations()

    api.get_mixed_selection.assert_awaited_once_with(40)
    api.get_subscribe_feed.assert_not_awaited()
    assert [row.item_id for row in rows] == [
        f"{provider.instance_id}_trending-music",
        f"{provider.instance_id}_sc_subscribed_feed",
    ]
    assert all(len(row.items) == 0 for row in rows)
    trending, feed = rows
    assert trending.name == "Trending"
    assert trending.icon == "mdi-playlist-music"
    assert trending.provider == provider.instance_id
    assert feed.name == "SoundCloud Feed"
    assert feed.translation_key == "soundcloud_feed"
    assert feed.icon == "mdi-rss"
    assert feed.provider == provider.instance_id
    await asyncio.gather(*background_tasks)


@pytest.mark.asyncio
async def test_rows_and_items_share_one_payload_fetch(
    provider: SoundcloudMusicProvider,
) -> None:
    """A rows call followed by an items call is served from one cached payload fetch."""
    background_tasks = _install_fake_cache(provider)
    api = _stub_api(provider)

    await provider.get_recommendations()
    # let the background cache-store task complete before the items call
    await asyncio.gather(*background_tasks)
    items = await provider.get_recommendation_items(f"{provider.instance_id}_trending-music")

    api.get_mixed_selection.assert_awaited_once_with(40)
    assert [item.item_id for item in items] == ["sp1"]


@pytest.mark.asyncio
async def test_get_recommendation_items_collection_row(
    provider: SoundcloudMusicProvider,
) -> None:
    """Items for a mixed-selection row trigger only the payload fetch, not the feed."""
    background_tasks = _install_fake_cache(provider)
    api = _stub_api(provider)

    items = await provider.get_recommendation_items(f"{provider.instance_id}_trending-music")

    api.get_mixed_selection.assert_awaited_once_with(40)
    api.get_subscribe_feed.assert_not_awaited()
    assert [item.item_id for item in items] == ["sp1"]
    assert items[0].name == "Daily Mix"
    await asyncio.gather(*background_tasks)


@pytest.mark.asyncio
async def test_get_recommendation_items_feed_row(
    provider: SoundcloudMusicProvider,
) -> None:
    """Items for the feed row trigger only the subscribed-feed fetch, not the payload."""
    background_tasks = _install_fake_cache(provider)
    api = _stub_api(provider)

    items = await provider.get_recommendation_items(f"{provider.instance_id}_sc_subscribed_feed")

    api.get_subscribe_feed.assert_awaited_once_with(40)
    api.get_mixed_selection.assert_not_awaited()
    assert isinstance(items, UniqueList)
    assert [item.item_id for item in items] == ["111"]
    assert items[0].name == "Feed Track"
    await asyncio.gather(*background_tasks)


@pytest.mark.asyncio
async def test_get_recommendation_items_unknown_id_returns_empty(
    provider: SoundcloudMusicProvider,
) -> None:
    """An unknown item_id yields an empty result."""
    background_tasks = _install_fake_cache(provider)
    _stub_api(provider)

    result = await provider.get_recommendation_items("bogus_row")

    assert isinstance(result, UniqueList)
    assert len(result) == 0
    await asyncio.gather(*background_tasks)
