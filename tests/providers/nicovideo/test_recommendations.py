"""Test nicovideo's two-method recommendations contract."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, Mock, call

import pytest
from music_assistant_models.media_items import ProviderMapping, Track
from music_assistant_models.unique_list import UniqueList

from music_assistant.providers.nicovideo import SUPPORTED_FEATURES
from music_assistant.providers.nicovideo.provider import NicovideoMusicProvider

EXPECTED_ROW_IDS = [
    "nicovideo_recommendations",
    "nicovideo_history",
    "nicovideo_following_activities",
    "nicovideo_like_history",
]


@pytest.fixture
def provider() -> NicovideoMusicProvider:
    """Create a real NicovideoMusicProvider with mocked dependencies."""
    mass = Mock()
    manifest = Mock()
    manifest.domain = "nicovideo"
    config = Mock()
    config.instance_id = "nicovideo--test123"
    config.name = "Nicovideo Test"
    config.enabled = True
    config.get_value.side_effect = lambda key, default=None: {
        "log_level": "GLOBAL",
    }.get(key, default)
    return NicovideoMusicProvider(mass, manifest, config, SUPPORTED_FEATURES)


def _track(item_id: str) -> Track:
    """Create a minimal Track for stubbed user service responses."""
    return Track(
        item_id=item_id,
        provider="nicovideo--test123",
        name=f"Track {item_id}",
        provider_mappings={
            ProviderMapping(
                item_id=item_id,
                provider_domain="nicovideo",
                provider_instance="nicovideo--test123",
            )
        },
    )


def _stub_user_service(provider: NicovideoMusicProvider) -> dict[str, AsyncMock]:
    """Replace the user service fetch methods with AsyncMocks returning canned tracks."""
    user = provider.service_manager.user
    mocks = {
        "get_recommendations": AsyncMock(return_value=[_track("rec1")]),
        "get_user_history": AsyncMock(return_value=[_track("hist1")]),
        "get_following_activities": AsyncMock(return_value=[_track("follow1")]),
        "get_like_history": AsyncMock(return_value=[_track("like1")]),
    }
    user.get_recommendations = mocks["get_recommendations"]  # type: ignore[method-assign]
    user.get_user_history = mocks["get_user_history"]  # type: ignore[method-assign]
    user.get_following_activities = mocks["get_following_activities"]  # type: ignore[method-assign]
    user.get_like_history = mocks["get_like_history"]  # type: ignore[method-assign]
    return mocks


def _install_cache_mocks(provider: NicovideoMusicProvider) -> None:
    """Make the @use_cache decorator treat every call as a cache miss."""
    provider.mass.cache.get_with_freshness = AsyncMock(  # type: ignore[method-assign]
        return_value=(None, False, False)
    )
    provider.mass.cache.set = AsyncMock()  # type: ignore[method-assign]


def _install_dict_backed_cache(provider: NicovideoMusicProvider) -> list[asyncio.Future[Any]]:
    """
    Install a dict-backed fake cache that serves what set() stored.

    Mirrors production: entries are stored serialized (to_dict) and, when the caller
    passes base_class, reconstructed via base_class.from_dict on a hit. Returns the
    list of background store tasks so tests can await them between calls.
    """
    store: dict[str, Any] = {}
    tasks: list[asyncio.Future[Any]] = []

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
        tasks.append(task)
        return task

    provider.mass.cache.get_with_freshness = AsyncMock(  # type: ignore[method-assign]
        side_effect=_cache_get
    )
    provider.mass.cache.set = AsyncMock(side_effect=_cache_set)  # type: ignore[method-assign]
    provider.mass.create_task = Mock(side_effect=_create_task)  # type: ignore[method-assign]
    return tasks


@pytest.mark.asyncio
async def test_get_recommendations_returns_static_rows_without_backend_calls(
    provider: NicovideoMusicProvider,
) -> None:
    """get_recommendations() returns the four static rows, without items or backend calls."""
    mocks = _stub_user_service(provider)

    result = await provider.get_recommendations()

    assert [folder.item_id for folder in result] == EXPECTED_ROW_IDS
    for folder in result:
        assert folder.provider == provider.instance_id
        assert not folder.items
    for mock in mocks.values():
        mock.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("item_id", "fetch_name", "expected_call", "expected_track_id"),
    [
        (
            "nicovideo_recommendations",
            "get_recommendations",
            call("video_recommendation_recommend", limit=25),
            "rec1",
        ),
        ("nicovideo_history", "get_user_history", call(limit=50), "hist1"),
        (
            "nicovideo_following_activities",
            "get_following_activities",
            call(limit=30),
            "follow1",
        ),
        ("nicovideo_like_history", "get_like_history", call(limit=50), "like1"),
    ],
)
async def test_get_recommendation_items_fetches_only_that_row(
    provider: NicovideoMusicProvider,
    item_id: str,
    fetch_name: str,
    expected_call: object,
    expected_track_id: str,
) -> None:
    """get_recommendation_items(row) issues only that row's fetch and returns its items."""
    _install_cache_mocks(provider)
    mocks = _stub_user_service(provider)

    result = await provider.get_recommendation_items(item_id)

    mocks[fetch_name].assert_awaited_once()
    assert mocks[fetch_name].await_args == expected_call
    for name, mock in mocks.items():
        if name != fetch_name:
            mock.assert_not_awaited()
    assert [item.item_id for item in result] == [expected_track_id]


@pytest.mark.asyncio
async def test_get_recommendation_items_unknown_id_returns_empty(
    provider: NicovideoMusicProvider,
) -> None:
    """get_recommendation_items() with an unknown item_id returns empty without any fetch."""
    _install_cache_mocks(provider)
    mocks = _stub_user_service(provider)

    result = await provider.get_recommendation_items("nicovideo_does_not_exist")

    assert result == UniqueList()
    for mock in mocks.values():
        mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_recommendation_items_warm_cache_hit_serves_stored_items(
    provider: NicovideoMusicProvider,
) -> None:
    """A second call for the same row is served from the cache, without a new backend fetch."""
    tasks = _install_dict_backed_cache(provider)
    mocks = _stub_user_service(provider)

    first = await provider.get_recommendation_items("nicovideo_history")
    # let the background cache-store task complete before the warm call
    await asyncio.gather(*tasks)
    second = await provider.get_recommendation_items("nicovideo_history")

    mocks["get_user_history"].assert_awaited_once()
    assert [item.item_id for item in first] == ["hist1"]
    assert [item.item_id for item in second] == ["hist1"]
