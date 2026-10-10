"""Tests for how the Last.fm Recommendations provider caches item resolution outcomes."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from music_assistant_models.enums import ProviderFeature, ProviderStatus
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
async def test_expired_miss_is_searched_again(
    manager: LastFMRecommendationManager, cache: _FakeCache
) -> None:
    """Once the remembered miss expires, the item is searched for again."""
    resolve = AsyncMock(side_effect=[None, _track()])
    with patch(
        "music_assistant.providers.lastfm_recommendations.recommendations.parse_track", resolve
    ):
        assert await manager.get_or_resolve_track(LASTFM_TRACK) is None
        del cache.data["miss_track_Snow Patrol_Chasing Cars"]
        resolved = await manager.get_or_resolve_track(LASTFM_TRACK)
    assert resolved is not None
    assert resolve.await_count == 2


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


def _mass(provider_status: ProviderStatus) -> Mock:
    """Return a mass stand-in with one streaming provider that finds nothing."""
    streaming = Mock(
        instance_id="apple_music--1",
        is_streaming_provider=True,
        supported_features={ProviderFeature.LIBRARY_TRACKS},
    )
    mass = Mock()
    mass.music.providers = [streaming]
    mass.music.tracks.get_library_item_by_external_ids = AsyncMock(return_value=None)
    mass.music.tracks.search = AsyncMock(return_value=[])
    mass.config.get_provider_configs = AsyncMock(
        return_value=[Mock(status=ProviderStatus.LOADED), Mock(status=provider_status)]
    )
    return mass


@pytest.mark.asyncio
async def test_no_match_while_a_provider_loads_is_incomplete() -> None:
    """A provider that has not loaded yet may have the item, so it is not a miss yet."""
    with pytest.raises(parsers.SearchIncomplete):
        await parsers.parse_track(LASTFM_TRACK, _mass(ProviderStatus.LOADING), INSTANCE_ID)


@pytest.mark.asyncio
async def test_no_match_with_all_providers_settled_is_a_miss() -> None:
    """A provider that failed to load does not hold back a miss."""
    assert await parsers.parse_track(LASTFM_TRACK, _mass(ProviderStatus.ERROR), INSTANCE_ID) is None


@pytest.mark.asyncio
async def test_provider_finishing_loading_during_the_search_is_no_miss() -> None:
    """A provider that loads while the others are searched was not searched itself."""
    mass = _mass(ProviderStatus.LOADING)

    async def _search_while_provider_finishes(*_args: Any, **_kwargs: Any) -> list[Track]:
        mass.config.get_provider_configs.return_value = [Mock(status=ProviderStatus.LOADED)]
        return []

    mass.music.tracks.search = AsyncMock(side_effect=_search_while_provider_finishes)
    with pytest.raises(parsers.SearchIncomplete):
        await parsers.parse_track(LASTFM_TRACK, mass, INSTANCE_ID)


@pytest.mark.asyncio
async def test_a_match_in_second_place_is_found() -> None:
    """Every candidate a provider returns is checked, not only the first."""
    mapping = ItemMapping(
        media_type=Track.media_type, item_id="temp", provider="x", name="Chasing Cars"
    )
    other = Track(item_id="2", provider="p", name="Run", provider_mappings=set())
    ctrl = Mock()
    ctrl.search = AsyncMock(return_value=[other, _track()])
    result = await parsers._search_providers_concurrent(ctrl, mapping, [Mock(name="p")], None)
    assert result is not None
    assert result.name == "Chasing Cars"


@pytest.mark.asyncio
async def test_unavailable_provider_makes_resolution_incomplete() -> None:
    """A provider that is unavailable was not searched, so finding nothing is no miss."""
    mapping = ItemMapping(media_type=Track.media_type, item_id="temp", provider="x", name="a")
    ctrl = Mock()
    ctrl.search = AsyncMock(return_value=[])
    with pytest.raises(parsers.SearchIncomplete):
        await parsers._search_providers_concurrent(
            ctrl, mapping, [Mock(name="p", available=False)], None
        )
    ctrl.search.assert_not_awaited()


@pytest.mark.asyncio
async def test_provider_unloading_during_the_search_makes_resolution_incomplete() -> None:
    """A provider that went unavailable while searched may have answered for that reason."""
    mapping = ItemMapping(media_type=Track.media_type, item_id="temp", provider="x", name="a")
    provider = Mock(name="p", available=True)

    async def _search_while_unloading(*_args: Any, **_kwargs: Any) -> list[Track]:
        provider.available = False
        return []

    ctrl = Mock()
    ctrl.search = AsyncMock(side_effect=_search_while_unloading)
    with pytest.raises(parsers.SearchIncomplete):
        await parsers._search_providers_concurrent(ctrl, mapping, [provider], None)
