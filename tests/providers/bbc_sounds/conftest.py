"""Shared fixtures for BBC Sounds provider tests."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from auntie_sounds.client import SoundsClient
from auntie_sounds.content import ContentService
from auntie_sounds.models import Menu, MenuItem
from auntie_sounds.playback import PlaybackService
from auntie_sounds.schedule import ScheduleService
from auntie_sounds.stations import StationService

from music_assistant.providers.bbc_sounds import SUPPORTED_FEATURES, BBCSoundsProvider
from music_assistant.providers.bbc_sounds.adaptor import Adaptor


@pytest.fixture
def client() -> SoundsClient:
    """Create a mock SoundsClient with mocked dependencies."""
    client = AsyncMock(spec=SoundsClient)
    # the services are only created by SoundsClient.start(), so the spec doesn't have them
    client.content = AsyncMock(spec=ContentService)
    client.playback = AsyncMock(spec=PlaybackService)
    client.stations = AsyncMock(spec=StationService)
    client.schedules = AsyncMock(spec=ScheduleService)
    client.is_signed_in = True
    return client


@pytest.fixture
def provider(client: SoundsClient) -> BBCSoundsProvider:
    """Create a real BBCSoundsProvider with mocked dependencies."""
    mass = Mock()
    manifest = Mock()
    manifest.domain = "bbc_sounds"
    config = Mock()
    config.instance_id = "bbc_sounds--test123"
    config.name = "BBC Sounds Test"
    config.enabled = True
    config.get_value.side_effect = lambda key, default=None: {
        "log_level": "GLOBAL",
    }.get(key, default)
    instance = BBCSoundsProvider(mass, manifest, config, SUPPORTED_FEATURES)
    instance.adaptor = Adaptor(instance)
    instance.client = client
    return instance


@pytest.fixture
def cache_tasks(provider: BBCSoundsProvider) -> list[asyncio.Future[Any]]:
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


@pytest.fixture
def blank_menu() -> Menu:
    """Create a blank menu."""
    return Menu(sub_items=[])


@pytest.fixture
def uk_menu() -> Menu:
    """Create a realistic UK menu."""
    return Menu(
        sub_items=[
            MenuItem(id="live", title="Live Radio"),
            MenuItem(id="catch_up", title="Catch-up Radio"),
            MenuItem(id="schedules", title="Station Schedules"),
            MenuItem(id="my_sounds", title="My Sounds"),
            MenuItem(id="latest_playables_for_curation_lllll", title="Latest News Playlist"),
            MenuItem(id="editorial_collection", title="Editor's Picks"),
            MenuItem(id="local_rail", title="From Your Area"),
            MenuItem(id="collections", title="Collections"),
            MenuItem(id="categories", title="Categories"),
            MenuItem(id="explore", title="Explore All"),
        ]
    )


@pytest.fixture
def international_menu() -> Menu:
    """Create a realistic international menu."""
    return Menu(
        sub_items=[
            MenuItem(id="live", title="Live Radio"),
            MenuItem(id="catch_up", title="Catch-up Radio"),
            MenuItem(id="schedules", title="Station Schedules"),
            MenuItem(id="collections", title="Collections"),
            MenuItem(id="categories", title="Categories"),
            MenuItem(id="explore", title="Explore All"),
        ]
    )
