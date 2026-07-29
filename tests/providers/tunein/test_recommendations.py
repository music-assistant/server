"""Test the TuneIn two-method recommendations contract."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from music_assistant_models.media_items import UniqueList

from music_assistant.providers.tunein import TuneInProvider

TRENDING_DATA = {
    "body": [
        {
            "type": "audio",
            "preset_id": "s100",
            "text": "Station One",
            "image": "http://cdn.example/1.png",
        },
        {"type": "audio", "preset_id": "s200", "text": "Station Two"},
        {"type": "link", "text": "Some Category", "URL": "http://cdn.example/category"},
    ]
}


def _stub_get_data(provider: TuneInProvider) -> AsyncMock:
    """Attach a __get_data stub that returns canned trending data."""
    mock = AsyncMock(return_value=TRENDING_DATA)
    provider._TuneInProvider__get_data = mock  # type: ignore[attr-defined]
    return mock


def _install_cache_mocks(provider: TuneInProvider) -> None:
    """Make the @use_cache decorator treat every call as a cache miss."""
    provider.mass.cache.get_with_freshness = AsyncMock(  # type: ignore[method-assign]
        return_value=(None, False, False)
    )
    provider.mass.cache.set = AsyncMock()  # type: ignore[method-assign]


def _install_fake_cache(provider: TuneInProvider) -> list[asyncio.Future[Any]]:
    """
    Back the real @use_cache decorator with a dict-backed fake cache.

    Mirrors production: set() stores the serialized (to_dict) folder and, since the
    caller passes base_class=RecommendationFolder, get_with_freshness reconstructs it
    via RecommendationFolder.from_dict on a hit.
    """
    store: dict[str, Any] = {}
    background_tasks: list[asyncio.Future[Any]] = []

    async def _cache_get(key: str, **kwargs: Any) -> tuple[Any, bool, bool]:
        if key not in store:
            return None, False, False
        data = store[key]
        base_class = kwargs.get("base_class")
        if base_class is not None and data is not None:
            return base_class.from_dict(data), True, True
        return data, True, True

    async def _cache_set(key: str, data: Any, **_kwargs: Any) -> None:
        store[key] = data.to_dict() if data is not None else None

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
async def test_get_recommendations_static_row_without_backend_calls(
    provider: TuneInProvider,
) -> None:
    """get_recommendations returns the static trending row without any backend call."""
    api_mock = _stub_get_data(provider)

    result = await provider.get_recommendations()

    api_mock.assert_not_awaited()
    assert len(result) == 1
    row = result[0]
    assert row.item_id == "trending"
    assert row.provider == provider.instance_id
    assert row.name == "Trending"
    assert row.translation_key == "trending_stations"
    assert len(row.items) == 0


@pytest.mark.asyncio
async def test_get_recommendation_items_trending_fetches_stations(
    provider: TuneInProvider,
) -> None:
    """get_recommendation_items("trending") fetches the trending feed and parses its stations."""
    _install_cache_mocks(provider)
    api_mock = _stub_get_data(provider)

    result = await provider.get_recommendation_items("trending")

    api_mock.assert_awaited_once()
    assert api_mock.call_args.args == ("Browse.ashx",)
    assert api_mock.call_args.kwargs == {"c": "trending"}
    # only the audio entries with a preset_id become stations (order is randomized)
    assert {item.item_id for item in result} == {"s100", "s200"}


@pytest.mark.asyncio
async def test_get_recommendation_items_served_from_warm_cache(
    provider: TuneInProvider,
) -> None:
    """A second items call is served from the cache and still returns the items."""
    background_tasks = _install_fake_cache(provider)
    api_mock = _stub_get_data(provider)

    first = await provider.get_recommendation_items("trending")
    # let the background cache-store task complete before the second call
    await asyncio.gather(*background_tasks)
    second = await provider.get_recommendation_items("trending")

    # the backend was fetched exactly once: the second call was a cache hit
    api_mock.assert_awaited_once()
    assert {item.item_id for item in first} == {"s100", "s200"}
    assert {item.item_id for item in second} == {"s100", "s200"}


@pytest.mark.asyncio
async def test_get_recommendation_items_unknown_id_returns_empty(
    provider: TuneInProvider,
) -> None:
    """An unknown row item_id yields an empty list without any backend call."""
    _install_cache_mocks(provider)
    api_mock = _stub_get_data(provider)

    result = await provider.get_recommendation_items("bogus_row")

    api_mock.assert_not_awaited()
    assert isinstance(result, UniqueList)
    assert len(result) == 0
