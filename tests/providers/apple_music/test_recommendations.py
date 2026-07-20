"""Test Apple Music two-method recommendations served from the cached bulk payload."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, Mock

import pytest
from music_assistant_models.media_items import Playlist, UniqueList

from music_assistant.providers.apple_music.provider import AppleMusicProvider

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Coroutine

INSTANCE_ID = "apple_music--test1"


def _station(station_id: str, name: str, is_live: bool = False) -> dict[str, Any]:
    """Build a station content object for a me/recommendations response."""
    return {"type": "stations", "id": station_id, "attributes": {"name": name, "isLive": is_live}}


def _recommendations_response(sections: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """Build a me/recommendations response from section title -> station objects."""
    return {
        "data": [
            {
                "id": f"rec{index}",
                "attributes": {"title": {"stringForDisplay": title}},
                "relationships": {"contents": {"data": stations}},
            }
            for index, (title, stations) in enumerate(sections.items())
        ]
    }


def _default_response() -> dict[str, Any]:
    """Build the default two-section payload response used by most tests."""
    return _recommendations_response(
        {
            "Made for You": [_station("ra.111", "My Station One")],
            "Stations for You": [
                _station("ra.222", "My Station Two"),
                _station("ra.333", "Live Station", is_live=True),
            ],
        }
    )


@pytest.fixture
async def provider() -> AsyncGenerator[AppleMusicProvider]:
    """Create a real AppleMusicProvider with mocked mass (dict-backed cache) and API client."""
    cache_store: dict[str, Any] = {}
    background_tasks: list[asyncio.Future[Any]] = []

    async def _cache_get(key: str, **_kwargs: Any) -> tuple[Any, bool, bool]:
        if key in cache_store:
            return cache_store[key], True, True
        return None, False, False

    async def _cache_set(key: str, data: Any, **_kwargs: Any) -> None:
        cache_store[key] = data

    def _create_task(target: Coroutine[Any, Any, Any], *_args: Any, **_kwargs: Any) -> Any:
        task: asyncio.Future[Any] = asyncio.ensure_future(target)
        background_tasks.append(task)
        return task

    mass = Mock()
    mass.cache.get_with_freshness = AsyncMock(side_effect=_cache_get)
    mass.cache.set = AsyncMock(side_effect=_cache_set)
    mass.create_task = Mock(side_effect=_create_task)
    manifest = Mock()
    manifest.domain = "apple_music"
    config = Mock()
    config.instance_id = INSTANCE_ID
    config.name = "Apple Music Test"
    config.get_value.side_effect = lambda key, default=None: {"log_level": "GLOBAL"}.get(
        key, default
    )
    provider = AppleMusicProvider(mass, manifest, config)
    provider.api_client.get_data = AsyncMock(return_value=_default_response())  # type: ignore[method-assign]
    yield provider
    await asyncio.gather(*background_tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_get_recommendations_returns_rows_without_items(
    provider: AppleMusicProvider,
) -> None:
    """get_recommendations() returns the payload's section rows, stripped of items."""
    rows = await provider.get_recommendations()

    assert [row.item_id for row in rows] == ["made_for_you", "stations_for_you"]
    assert [row.name for row in rows] == ["Made for You", "Stations for You"]
    assert all(row.provider == INSTANCE_ID for row in rows)
    assert all(not row.items for row in rows)


@pytest.mark.asyncio
async def test_rows_and_items_share_one_backend_fetch(provider: AppleMusicProvider) -> None:
    """The rows call and both per-row items calls are served from one me/recommendations fetch."""
    api_get_data = cast("AsyncMock", provider.api_client.get_data)

    await provider.get_recommendations()
    # let the background cache-store task complete before the next calls
    await asyncio.sleep(0)
    items_one = await provider.get_recommendation_items("made_for_you")
    items_two = await provider.get_recommendation_items("stations_for_you")

    api_get_data.assert_awaited_once()
    assert [item.item_id for item in items_one] == ["ra.111"]
    assert all(isinstance(item, Playlist) for item in items_one)
    # the live station in the payload is skipped
    assert [item.item_id for item in items_two] == ["ra.222"]


@pytest.mark.asyncio
async def test_warm_cache_hit_serves_items_without_backend_fetch(
    provider: AppleMusicProvider,
) -> None:
    """A second items call is served from the stored cache entry, not a new backend fetch."""
    api_get_data = cast("AsyncMock", provider.api_client.get_data)

    items_first = await provider.get_recommendation_items("made_for_you")
    # let the background cache-store task complete before the next call
    await asyncio.sleep(0)
    items_second = await provider.get_recommendation_items("made_for_you")

    api_get_data.assert_awaited_once()
    assert [item.item_id for item in items_first] == ["ra.111"]
    assert [item.item_id for item in items_second] == ["ra.111"]


@pytest.mark.asyncio
async def test_get_recommendation_items_unknown_id_returns_empty(
    provider: AppleMusicProvider,
) -> None:
    """An item_id not present in the payload yields an empty UniqueList."""
    result = await provider.get_recommendation_items("no_such_row")

    assert isinstance(result, UniqueList)
    assert list(result) == []


@pytest.mark.asyncio
async def test_browse_stations_returns_all_payload_stations(
    provider: AppleMusicProvider,
) -> None:
    """browse_stations flattens the stations of every payload folder."""
    stations = await provider.recommendation_manager.browse_stations()

    assert [station.item_id for station in stations] == ["ra.111", "ra.222"]


@pytest.mark.asyncio
async def test_resolve_station_id_returns_rotated_id(provider: AppleMusicProvider) -> None:
    """resolve_station_id maps a stale station id to the fresh id via the station name."""
    api_get_data = cast("AsyncMock", provider.api_client.get_data)
    rotated = _recommendations_response({"Made for You": [_station("ra.999", "My Station One")]})
    api_get_data.side_effect = [_default_response(), rotated]

    result = await provider.recommendation_manager.resolve_station_id("ra.111")

    assert result == "ra.999"
    # one payload fetch to populate the maps, one fresh fetch to learn the current id
    assert api_get_data.await_count == 2


@pytest.mark.asyncio
async def test_resolve_station_id_stores_fresh_payload_for_rows_and_items(
    provider: AppleMusicProvider,
) -> None:
    """The forced-fresh fetch of resolve_station_id is stored back to the payload cache."""
    api_get_data = cast("AsyncMock", provider.api_client.get_data)
    rotated = _recommendations_response({"Made for You": [_station("ra.999", "My Station One")]})
    api_get_data.side_effect = [_default_response(), rotated]

    result = await provider.recommendation_manager.resolve_station_id("ra.111")
    items = await provider.get_recommendation_items("made_for_you")

    assert result == "ra.999"
    # rows/items serve the refreshed (rotated) payload from the cache, without a new fetch
    assert [item.item_id for item in items] == ["ra.999"]
    assert api_get_data.await_count == 2


@pytest.mark.asyncio
async def test_resolve_station_id_after_restart_with_cached_payload(
    provider: AppleMusicProvider,
) -> None:
    """After a restart the station maps are rebuilt from the cache-served payload folders."""
    api_get_data = cast("AsyncMock", provider.api_client.get_data)
    rotated = _recommendations_response({"Made for You": [_station("ra.999", "My Station One")]})
    api_get_data.side_effect = [_default_response(), rotated]
    # warm the persistent cache, then simulate a process restart: the in-memory
    # station maps are gone while the cached payload entry survives
    await provider.get_recommendations()
    await asyncio.sleep(0)
    manager = provider.recommendation_manager
    manager._station_id_to_name.clear()
    manager._station_name_to_id.clear()

    result = await manager.resolve_station_id("ra.111")

    assert result == "ra.999"
    # the cache-served payload populated the maps without a backend fetch;
    # only the initial warm-up and the forced-fresh rotation fetch hit the API
    assert api_get_data.await_count == 2


@pytest.mark.asyncio
async def test_resolve_station_id_unknown_returns_none(provider: AppleMusicProvider) -> None:
    """resolve_station_id returns None for an id absent from the payload, without a fresh fetch."""
    api_get_data = cast("AsyncMock", provider.api_client.get_data)

    result = await provider.recommendation_manager.resolve_station_id("ra.unknown")

    assert result is None
    api_get_data.assert_awaited_once()
