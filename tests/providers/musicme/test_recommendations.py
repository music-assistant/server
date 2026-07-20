"""Test the MusicMe two-method recommendations contract."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from music_assistant_models.media_items import Radio, RecommendationFolder, UniqueList

from music_assistant.providers.musicme.provider import MusicMeProvider

HOME_DATA = {"results": {"items": [{"id": 1, "name": "Home Radio"}]}}
NEWS_DATA = {"results": {"albums": [{"barcode": "b1", "name": "Album", "streamable": 2}]}}
TOPS_DATA = {"results": {"artists": [{"id": 2, "name": "Artist"}]}}
RADIOS_DATA = {"results": {"theme-airplays": [{"id": 3, "name": "Radio"}]}}


def _stub_api_get(provider: MusicMeProvider) -> AsyncMock:
    """Attach an _api_get stub that returns canned data keyed by endpoint prefix."""

    async def _fake(endpoint: str) -> dict[str, Any] | None:
        if endpoint.startswith("/home"):
            return HOME_DATA
        if endpoint.startswith("/news"):
            return NEWS_DATA
        if endpoint.startswith("/tops"):
            return TOPS_DATA
        if endpoint.startswith("/radios"):
            return RADIOS_DATA
        return None

    mock = AsyncMock(side_effect=_fake)
    provider._api_get = mock  # type: ignore[method-assign]
    return mock


def _install_cache_mocks(provider: MusicMeProvider) -> None:
    """Make the @use_cache decorator treat every call as a cache miss."""
    provider.mass.cache.get_with_freshness = AsyncMock(  # type: ignore[method-assign]
        return_value=(None, False, False)
    )
    provider.mass.cache.set = AsyncMock()  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_get_recommendations_static_rows_zero_backend_calls(
    provider: MusicMeProvider,
) -> None:
    """get_recommendations returns the four static rows, without items and without I/O."""
    api_mock = _stub_api_get(provider)

    rows = await provider.get_recommendations()

    api_mock.assert_not_awaited()
    assert [row.item_id for row in rows] == [
        f"{provider.instance_id}_home",
        f"{provider.instance_id}_news",
        f"{provider.instance_id}_tops",
        f"{provider.instance_id}_radios",
    ]
    assert [row.name for row in rows] == ["Featured", "New releases", "Top artists", "Radios"]
    assert [row.translation_key for row in rows] == [
        "featured",
        "new_releases",
        "top_artists",
        "radios",
    ]
    assert [row.icon for row in rows] == ["mdi-star", "mdi-new-box", "mdi-trending-up", "mdi-radio"]
    assert all(not row.items for row in rows)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("suffix", "endpoint", "expected_item_ids"),
    [
        ("home", "/home", ["1"]),
        ("news", "/news/0", ["b1"]),
        ("tops", "/tops", ["2"]),
        ("radios", "/radios", ["3"]),
    ],
)
async def test_get_recommendation_items_fetches_only_that_row(
    provider: MusicMeProvider,
    suffix: str,
    endpoint: str,
    expected_item_ids: list[str],
) -> None:
    """get_recommendation_items triggers exactly the requested row's backend fetch."""
    _install_cache_mocks(provider)
    api_mock = _stub_api_get(provider)

    items = await provider.get_recommendation_items(f"{provider.instance_id}_{suffix}")

    api_mock.assert_awaited_once()
    assert api_mock.call_args.args[0].split("?", 1)[0] == endpoint
    assert isinstance(items, UniqueList)
    assert [item.item_id for item in items] == expected_item_ids


@pytest.mark.asyncio
async def test_get_recommendation_items_unknown_id_returns_empty(
    provider: MusicMeProvider,
) -> None:
    """An unknown row item_id returns an empty result without backend calls."""
    _install_cache_mocks(provider)
    api_mock = _stub_api_get(provider)

    items = await provider.get_recommendation_items("bogus")

    api_mock.assert_not_awaited()
    assert items == []


@pytest.mark.asyncio
async def test_get_recommendation_items_cache_hit_skips_backend(
    provider: MusicMeProvider,
) -> None:
    """A fresh cache hit is served as media items without backend calls."""
    # production get_with_freshness reconstructs a base_class entry via from_dict
    # before returning it, so the decorator receives a RecommendationFolder
    cached_folder = RecommendationFolder(
        name="Featured",
        translation_key="featured",
        item_id=f"{provider.instance_id}_home",
        provider=provider.instance_id,
        icon="mdi-star",
        items=UniqueList([provider._parse_radio({"id": 1, "name": "Home Radio"})]),
    )
    provider.mass.cache.get_with_freshness = AsyncMock(  # type: ignore[method-assign]
        return_value=(cached_folder, True, True)
    )
    api_mock = _stub_api_get(provider)

    items = await provider.get_recommendation_items(f"{provider.instance_id}_home")

    api_mock.assert_not_awaited()
    assert len(items) == 1
    assert isinstance(items[0], Radio)
    assert items[0].item_id == "1"


@pytest.mark.asyncio
async def test_get_recommendation_items_warm_cache_hit_skips_second_fetch(
    provider: MusicMeProvider,
) -> None:
    """The second items call for a row is served from the cache, not the backend."""
    cache_store: dict[str, Any] = {}
    background_tasks: list[asyncio.Future[Any]] = []

    async def _cache_get(key: str, **_kwargs: Any) -> tuple[Any, bool, bool]:
        if key in cache_store:
            return cache_store[key], True, True
        return None, False, False

    async def _cache_set(key: str, data: Any, **_kwargs: Any) -> None:
        cache_store[key] = data

    def _create_task(target: Any, **_kwargs: Any) -> asyncio.Future[Any]:
        task: asyncio.Future[Any] = asyncio.ensure_future(target)
        background_tasks.append(task)
        return task

    provider.mass.cache.get_with_freshness = AsyncMock(  # type: ignore[method-assign]
        side_effect=_cache_get
    )
    provider.mass.cache.set = AsyncMock(side_effect=_cache_set)  # type: ignore[method-assign]
    provider.mass.create_task = Mock(side_effect=_create_task)  # type: ignore[method-assign]
    api_mock = _stub_api_get(provider)

    first = await provider.get_recommendation_items(f"{provider.instance_id}_home")
    # let the background cache-store task complete before the second call
    await asyncio.gather(*background_tasks)
    second = await provider.get_recommendation_items(f"{provider.instance_id}_home")

    api_mock.assert_awaited_once()
    assert [item.item_id for item in first] == ["1"]
    assert [item.item_id for item in second] == ["1"]
    assert isinstance(second[0], Radio)
