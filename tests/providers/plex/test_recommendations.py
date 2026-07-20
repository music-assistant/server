"""Tests for the Plex provider's two-method recommendations contract."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import ItemMapping, UniqueList

from music_assistant.providers.plex import PlexProvider
from music_assistant.providers.plex.helpers import SUPPORTED_FEATURES

INSTANCE_ID = "plex_instance_1"
HUB_RECENT = "hub.music.recentlyplayed"
HUB_NEW = "hub.music.newreleases"


class _FakeHubItem:
    """Minimal Plex hub item stub."""

    def __init__(self, type_: str, key: str, title: str) -> None:
        self.type = type_
        self.key = key
        self.title = title


class _FakeHub:
    """Minimal Plex hub stub with preloaded partial items."""

    def __init__(self, identifier: str, title: str, items: list[Any]) -> None:
        self.hubIdentifier = identifier
        self.title = title
        self._partialItems = items


def _make_hubs() -> list[_FakeHub]:
    """Build two hubs with one parseable item each."""
    return [
        _FakeHub(
            HUB_RECENT,
            "Recently Played",
            [_FakeHubItem("track", "/library/metadata/1", "Recent Track")],
        ),
        _FakeHub(
            HUB_NEW,
            "New Releases",
            [_FakeHubItem("track", "/library/metadata/2", "New Track")],
        ),
    ]


def _make_provider(hubs: list[_FakeHub]) -> tuple[Any, list[asyncio.Future[Any]]]:
    """Create a PlexProvider with stubbed backend, dict-backed cache and canned hubs."""
    mock_mass = MagicMock()
    mock_config = MagicMock()
    mock_config.instance_id = INSTANCE_ID
    config_values: dict[str, Any] = {
        "library_type": "music",
        "log_level": "INFO",
        "token": "local_auth",
        "hub_items_limit": 10,
    }
    mock_config.get_value = lambda key: config_values.get(key)
    mock_manifest = MagicMock()
    mock_manifest.type = "music"
    mock_manifest.domain = "plex"

    provider = PlexProvider(mock_mass, mock_manifest, mock_config, SUPPORTED_FEATURES)
    provider._plex_server = MagicMock()
    provider._plex_library = MagicMock()
    provider._plex_library.key = "10"
    provider._plex_library.fetchItems = Mock(return_value=hubs)
    provider._myplex_account = MagicMock()

    async def _run_async(call: Any, *args: Any, **kwargs: Any) -> Any:
        return call(*args, **kwargs)

    provider._run_async = _run_async  # type: ignore[method-assign]

    async def _parse(item: Any) -> ItemMapping:
        return ItemMapping(
            media_type=MediaType.TRACK,
            item_id=item.key,
            provider=INSTANCE_ID,
            name=item.title,
        )

    provider._parse = AsyncMock(side_effect=_parse)  # type: ignore[method-assign]

    # dict-backed fake cache so the mixin's @use_cache sees real hits/misses
    cache_store: dict[str, Any] = {}
    background_tasks: list[asyncio.Future[Any]] = []

    async def _cache_get(key: str, **_kwargs: Any) -> tuple[Any, bool, bool]:
        if key in cache_store:
            return cache_store[key], True, True
        return None, False, False

    async def _cache_set(key: str, data: Any, **_kwargs: Any) -> None:
        cache_store[key] = data

    def _create_task(target: Any, *_args: Any, **_kwargs: Any) -> asyncio.Future[Any]:
        task: asyncio.Future[Any] = asyncio.ensure_future(target)
        background_tasks.append(task)
        return task

    provider.mass.cache.get_with_freshness = AsyncMock(  # type: ignore[method-assign]
        side_effect=_cache_get
    )
    provider.mass.cache.set = AsyncMock(side_effect=_cache_set)  # type: ignore[method-assign]
    provider.mass.create_task = Mock(side_effect=_create_task)  # type: ignore[method-assign]

    return provider, background_tasks


@pytest.mark.asyncio
async def test_get_recommendations_returns_hub_rows_without_items() -> None:
    """Rows mirror the Plex hubs with stable item_ids and empty items."""
    provider, background_tasks = _make_provider(_make_hubs())

    rows = await provider.get_recommendations()
    await asyncio.gather(*background_tasks)

    assert [row.item_id for row in rows] == [
        f"{INSTANCE_ID}_{HUB_RECENT}",
        f"{INSTANCE_ID}_{HUB_NEW}",
    ]
    assert [row.name for row in rows] == ["Recently Played", "New Releases"]
    assert all(row.provider == INSTANCE_ID for row in rows)
    assert all(row.icon == "mdi-music" for row in rows)
    assert all(len(row.items) == 0 for row in rows)
    provider._plex_library.fetchItems.assert_called_once_with(
        "/hubs/sections/10?includeStations=1&count=10"
    )


@pytest.mark.asyncio
async def test_rows_and_items_served_from_single_payload_fetch() -> None:
    """The rows call and both items calls share one cached hubs fetch."""
    provider, background_tasks = _make_provider(_make_hubs())

    await provider.get_recommendations()
    # let the background cache-store task complete before the items calls
    await asyncio.gather(*background_tasks)
    items_recent = await provider.get_recommendation_items(f"{INSTANCE_ID}_{HUB_RECENT}")
    items_new = await provider.get_recommendation_items(f"{INSTANCE_ID}_{HUB_NEW}")

    assert provider._plex_library.fetchItems.call_count == 1
    assert [item.name for item in items_recent] == ["Recent Track"]
    assert [item.name for item in items_new] == ["New Track"]


@pytest.mark.asyncio
async def test_get_recommendation_items_unknown_id_returns_empty() -> None:
    """An unknown item_id yields an empty UniqueList."""
    provider, background_tasks = _make_provider(_make_hubs())

    result = await provider.get_recommendation_items(f"{INSTANCE_ID}_bogus_hub")
    await asyncio.gather(*background_tasks)

    assert isinstance(result, UniqueList)
    assert len(result) == 0


@pytest.mark.asyncio
async def test_hub_without_parseable_items_is_skipped() -> None:
    """A hub whose items all fail to parse does not produce a row."""
    hubs = _make_hubs()
    hubs.append(_FakeHub("hub.music.stations", "Stations", [_FakeHubItem("station", "/s1", "S1")]))
    provider, background_tasks = _make_provider(hubs)

    async def _parse_tracks_only(item: Any) -> ItemMapping | None:
        if item.type != "track":
            return None
        return ItemMapping(
            media_type=MediaType.TRACK,
            item_id=item.key,
            provider=INSTANCE_ID,
            name=item.title,
        )

    provider._parse = AsyncMock(side_effect=_parse_tracks_only)

    rows = await provider.get_recommendations()
    await asyncio.gather(*background_tasks)

    assert [row.item_id for row in rows] == [
        f"{INSTANCE_ID}_{HUB_RECENT}",
        f"{INSTANCE_ID}_{HUB_NEW}",
    ]


@pytest.mark.asyncio
async def test_backend_error_yields_no_rows() -> None:
    """A failing hubs fetch is isolated and results in zero rows."""
    provider, background_tasks = _make_provider([])
    provider._plex_library.fetchItems = Mock(side_effect=RuntimeError("plex down"))

    rows = await provider.get_recommendations()
    await asyncio.gather(*background_tasks)

    assert rows == []
