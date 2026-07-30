"""Test Nugs.net two-method recommendations: static rows + per-row item fetching."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from music_assistant.providers.nugs import NugsProvider

POPULAR_DATA = {"items": [{"id": "100"}]}
SHOW_DATA = {
    "Response": {"id": "100", "title": "Popular Show", "artist": {"id": "9", "name": "Artist"}}
}
RECOMMENDED_DATA = {
    "items": [{"id": "200", "title": "Recommended Show", "artist": {"id": "9", "name": "Artist"}}]
}
RECENT_DATA = {
    "items": [{"id": "300", "title": "Recent Show", "artist": {"id": "9", "name": "Artist"}}]
}


def _stub_get_data(provider: NugsProvider) -> AsyncMock:
    """Attach a _get_data stub that returns canned data keyed by endpoint."""

    async def _fake(_nugs_api: str, endpoint: str, **_kwargs: Any) -> Any:
        if endpoint == "releases/popular":
            return POPULAR_DATA
        if endpoint.startswith("shows/"):
            return SHOW_DATA
        if endpoint == "me/releases/recommendations":
            return RECOMMENDED_DATA
        if endpoint == "releases/recent":
            return RECENT_DATA
        return {"items": []}

    mock = AsyncMock(side_effect=_fake)
    provider._get_data = mock  # type: ignore[method-assign]
    return mock


def _install_cache_mocks(provider: NugsProvider) -> None:
    """Make the @use_cache decorator treat every call as a cache miss."""
    provider.mass.cache.get_with_freshness = AsyncMock(  # type: ignore[method-assign]
        return_value=(None, False, False)
    )
    provider.mass.cache.set = AsyncMock()  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_get_recommendations_static_rows_no_backend_calls(provider: NugsProvider) -> None:
    """get_recommendations returns the three static rows without any backend call."""
    api_mock = _stub_get_data(provider)

    result = await provider.get_recommendations()

    api_mock.assert_not_awaited()
    assert [folder.item_id for folder in result] == [
        "nugs_popular_shows",
        "nugs_recommended_shows",
        "nugs_recent_shows",
    ]
    assert [folder.name for folder in result] == [
        "Most Popular",
        "Recommended Shows",
        "Recent Shows",
    ]
    assert [folder.translation_key for folder in result] == [
        "nugs_popular_shows",
        "nugs_recommended_shows",
        "nugs_recent_shows",
    ]
    assert all(folder.provider == provider.instance_id for folder in result)
    assert all(len(folder.items) == 0 for folder in result)


@pytest.mark.asyncio
async def test_get_recommendation_items_popular(provider: NugsProvider) -> None:
    """The popular row fetches the popular listing plus per-show details, nothing else."""
    _install_cache_mocks(provider)
    api_mock = _stub_get_data(provider)

    result = await provider.get_recommendation_items("nugs_popular_shows")

    called_endpoints = {call.args[1] for call in api_mock.call_args_list}
    assert called_endpoints == {"releases/popular", "shows/100"}
    assert [item.item_id for item in result] == ["100"]


@pytest.mark.asyncio
async def test_get_recommendation_items_recommended(provider: NugsProvider) -> None:
    """The recommended row fetches only its own endpoint."""
    _install_cache_mocks(provider)
    api_mock = _stub_get_data(provider)

    result = await provider.get_recommendation_items("nugs_recommended_shows")

    api_mock.assert_awaited_once()
    assert api_mock.call_args.args == ("catalog", "me/releases/recommendations")
    assert [item.item_id for item in result] == ["200"]


@pytest.mark.asyncio
async def test_get_recommendation_items_recent(provider: NugsProvider) -> None:
    """The recent row fetches only releases/recent, skipping the popular N+1 loop."""
    _install_cache_mocks(provider)
    api_mock = _stub_get_data(provider)

    result = await provider.get_recommendation_items("nugs_recent_shows")

    api_mock.assert_awaited_once()
    assert api_mock.call_args.args == ("catalog", "releases/recent")
    assert [item.item_id for item in result] == ["300"]


@pytest.mark.asyncio
async def test_get_recommendation_items_warm_cache_hit(provider: NugsProvider) -> None:
    """A second items call is served from the cache: backend not fetched, items intact."""
    store: dict[str, Any] = {}
    tasks: list[asyncio.Future[Any]] = []

    async def _cache_get(key: str, **kwargs: Any) -> tuple[Any, bool, bool]:
        if key not in store:
            return None, False, False
        data = store[key]
        base_class = kwargs.get("base_class")
        if base_class is not None and isinstance(data, dict):
            # mimic production get_with_freshness reconstructing via base_class
            data = base_class.from_dict(data)
        return data, True, True

    async def _cache_set(key: str, data: Any, **_kwargs: Any) -> None:
        # mimic production cache storing serialized data
        store[key] = data.to_dict() if hasattr(data, "to_dict") else data

    def _create_task(coro: Any, **_kwargs: Any) -> None:
        tasks.append(asyncio.ensure_future(coro))

    provider.mass.cache.get_with_freshness = AsyncMock(  # type: ignore[method-assign]
        side_effect=_cache_get
    )
    provider.mass.cache.set = AsyncMock(side_effect=_cache_set)  # type: ignore[method-assign]
    provider.mass.create_task = Mock(side_effect=_create_task)  # type: ignore[method-assign]
    api_mock = _stub_get_data(provider)

    first = await provider.get_recommendation_items("nugs_recommended_shows")
    # let the background cache-store task complete before the second call
    await asyncio.gather(*tasks)
    api_mock.assert_awaited_once()
    api_mock.reset_mock()

    second = await provider.get_recommendation_items("nugs_recommended_shows")

    api_mock.assert_not_awaited()
    assert [item.item_id for item in first] == ["200"]
    assert [item.item_id for item in second] == ["200"]


@pytest.mark.asyncio
async def test_get_recommendation_items_unknown_id_returns_empty(provider: NugsProvider) -> None:
    """An unknown row item_id yields an empty result without any backend call."""
    _install_cache_mocks(provider)
    api_mock = _stub_get_data(provider)

    result = await provider.get_recommendation_items("bogus_row")

    api_mock.assert_not_awaited()
    assert len(result) == 0
