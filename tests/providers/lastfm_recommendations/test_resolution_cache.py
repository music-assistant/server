"""Tests for how the Last.fm Recommendations provider caches item resolution outcomes."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from music_assistant_models.errors import RetriesExhausted
from music_assistant_models.media_items import ItemMapping, Track

from music_assistant.providers.lastfm_recommendations import parsers
from music_assistant.providers.lastfm_recommendations.recommendations import (
    LastFMRecommendationManager,
)

INSTANCE_ID = "lastfm_recommendations--test1"
LASTFM_TRACK = {"name": "Chasing Cars", "artist": {"name": "Snow Patrol"}}


class _FakeCache:
    """In-memory stand-in for the cache controller."""

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}

    async def get(self, key: str, **_kwargs: Any) -> Any:
        return self.data.get(key)

    async def set(self, key: str, data: Any, **_kwargs: Any) -> None:
        self.data[key] = data


@pytest.fixture
def cache() -> _FakeCache:
    """Return an empty in-memory cache."""
    return _FakeCache()


@pytest.fixture
def manager(cache: _FakeCache) -> LastFMRecommendationManager:
    """Return a recommendation manager backed by the in-memory cache."""
    provider = Mock()
    provider.instance_id = INSTANCE_ID
    provider.mass.cache = cache
    return LastFMRecommendationManager(provider)


def _track() -> Track:
    return Track(
        item_id="1",
        provider="apple_music",
        name="Chasing Cars",
        provider_mappings=set(),
    )


@pytest.mark.asyncio
async def test_miss_is_remembered(manager: LastFMRecommendationManager, cache: _FakeCache) -> None:
    """An item no provider has is not searched for again on the next refresh."""
    resolve = AsyncMock(return_value=None)
    with patch(
        "music_assistant.providers.lastfm_recommendations.recommendations.parse_track", resolve
    ):
        assert await manager.get_or_resolve_track(LASTFM_TRACK) is None
        assert await manager.get_or_resolve_track(LASTFM_TRACK) is None
    assert resolve.await_count == 1
    assert cache.data["miss_track_Snow Patrol_Chasing Cars"] is True


@pytest.mark.asyncio
async def test_persisted_miss_survives_restart(
    manager: LastFMRecommendationManager, cache: _FakeCache
) -> None:
    """A miss remembered before a restart still skips the search afterwards."""
    cache.data["miss_track_Snow Patrol_Chasing Cars"] = True
    resolve = AsyncMock()
    with patch(
        "music_assistant.providers.lastfm_recommendations.recommendations.parse_track", resolve
    ):
        assert await manager.get_or_resolve_track(LASTFM_TRACK) is None
    resolve.assert_not_awaited()


@pytest.mark.asyncio
async def test_incomplete_search_is_retried(
    manager: LastFMRecommendationManager, cache: _FakeCache
) -> None:
    """A search that could not complete is not remembered as a miss."""
    resolve = AsyncMock(side_effect=[parsers.SearchIncomplete("x"), _track()])
    with patch(
        "music_assistant.providers.lastfm_recommendations.recommendations.parse_track", resolve
    ):
        assert await manager.get_or_resolve_track(LASTFM_TRACK) is None
        resolved = await manager.get_or_resolve_track(LASTFM_TRACK)
    assert resolved is not None
    assert resolved.name == "Chasing Cars"
    assert not any(key.startswith("miss_") for key in cache.data)


@pytest.mark.asyncio
async def test_failed_provider_search_makes_resolution_incomplete() -> None:
    """A rate limited provider search does not count as the provider not having the item."""
    mapping = ItemMapping(media_type=Track.media_type, item_id="temp", provider="x", name="a")
    ctrl = Mock()
    ctrl.search = AsyncMock(side_effect=RetriesExhausted("rate limited"))
    with pytest.raises(parsers.SearchIncomplete):
        await parsers._search_providers_concurrent(ctrl, mapping, [Mock(name="p")], None)


@pytest.mark.asyncio
async def test_empty_provider_searches_are_a_miss() -> None:
    """Providers that answered without a match make a definite miss."""
    mapping = ItemMapping(media_type=Track.media_type, item_id="temp", provider="x", name="a")
    ctrl = Mock()
    ctrl.search = AsyncMock(return_value=[])
    assert await parsers._search_providers_concurrent(ctrl, mapping, [Mock(name="p")], None) is None
