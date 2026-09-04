"""Shared fixtures for BBC Sounds provider tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest
from sounds.client import SoundsClient
from sounds.models import Menu, MenuItem
from sounds.schedule import ScheduleService
from sounds.stations import StationService

from music_assistant.providers.bbc_sounds import SUPPORTED_FEATURES, BBCSoundsProvider
from music_assistant.providers.bbc_sounds.adaptor import Adaptor


@pytest.fixture
def client() -> SoundsClient:
    """Create a mock SoundsClient with mocked dependencies."""
    client = AsyncMock(spec=SoundsClient)
    client.stations = AsyncMock(spec=StationService)
    client.schedules = AsyncMock(spec=ScheduleService)
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
    instance.logged_in = True
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
            MenuItem(id="listen_live", title="Listen Live"),
            MenuItem(id="stations", title="Station & Schedules"),
            MenuItem(id="continue_listening", title="Continue Listening"),
            MenuItem(id="latest_news", title="Latest News Playlist"),
            MenuItem(id="editors_picks", title="Editor's Picks"),
            MenuItem(id="from_your_area", title="From Your Area"),
            MenuItem(id="collections", title="Collections"),
            MenuItem(id="categories", title="Categories"),
            MenuItem(id="explore_all", title="Explore All"),
        ]
    )


@pytest.fixture
def international_menu() -> Menu:
    """Create a realistic international menu."""
    return Menu(
        sub_items=[
            MenuItem(id="listen_live", title="Listen Live"),
            MenuItem(id="stations", title="Station & Schedules"),
            MenuItem(id="collections", title="Collections"),
            MenuItem(id="categories", title="Categories"),
            MenuItem(id="explore_all", title="Explore All"),
        ]
    )
