"""Shared fixtures for BBC Sounds provider tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest
from auntie_sounds.auth import AuthService
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
    client.auth = Mock(spec=AuthService)
    client.auth.is_signed_in = True
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
    instance.show_local_stations = False
    return instance


@pytest.fixture
def blank_menu() -> Menu:
    """Create a blank menu."""
    return Menu(sub_items=[])


@pytest.fixture
def uk_menu() -> Menu:
    """Create a realistic UK menu."""
    return Menu(
        sub_items=[
            MenuItem(id="radio", title="Live Radio"),
            MenuItem(id="catch_up", title="Catch-up Radio"),
            MenuItem(id="schedule", title="Station Schedules"),
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
            MenuItem(id="radio", title="Live Radio"),
            MenuItem(id="catch_up", title="Catch-up Radio"),
            MenuItem(id="schedule", title="Station Schedules"),
            MenuItem(id="collections", title="Collections"),
            MenuItem(id="categories", title="Categories"),
            MenuItem(id="explore", title="Explore All"),
        ]
    )
