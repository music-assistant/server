"""Shared fixtures for Twitch provider tests."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from music_assistant.providers.twitch import SUPPORTED_FEATURES, TwitchProvider


@pytest.fixture
def mass_mock() -> Mock:
    """Return a mock MusicAssistant instance."""
    mass = Mock()
    mass.http_session = AsyncMock()
    mass.http_session.ws_connect = AsyncMock()
    mass.subscribe = Mock(return_value=Mock())  # returns unsubscribe callable
    mass.player_queues = Mock()
    mass.player_queues.play_media = AsyncMock()
    mass.cache.get = AsyncMock(return_value=None)
    mass.cache.set = AsyncMock()
    return mass


@pytest.fixture
def manifest_mock() -> Mock:
    """Return a mock provider manifest."""
    manifest = Mock()
    manifest.domain = "twitch"
    return manifest


@pytest.fixture
def config_mock() -> Mock:
    """Return a mock provider config."""
    config = Mock()
    config.name = "Twitch Test"
    config.instance_id = "twitch_test"
    config.enabled = True
    config.get_value.side_effect = lambda key, default=None: {
        "client_id": "",
        "client_secret": "",
        "streamlink_token": "",
        "ad_handling": "silence",
        "auto_raid": True,
        "log_level": "GLOBAL",
    }.get(key, default)
    return config


@pytest.fixture
def provider(mass_mock: Mock, manifest_mock: Mock, config_mock: Mock) -> TwitchProvider:
    """Return a TwitchProvider instance."""
    return TwitchProvider(mass_mock, manifest_mock, config_mock, SUPPORTED_FEATURES)


@pytest.fixture
def mock_http_response() -> Callable[..., AsyncMock]:
    """Return a factory for mock aiohttp responses."""

    def _make_response(
        status: int = 200,
        json_data: dict[str, Any] | list[Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> AsyncMock:
        response = AsyncMock()
        response.status = status
        response.headers = headers or {}
        if json_data is not None:
            response.json = AsyncMock(return_value=json_data)
        return response

    return _make_response
