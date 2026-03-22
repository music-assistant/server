"""Shared fixtures for Twitch provider tests."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from music_assistant.providers.twitch import SUPPORTED_FEATURES, TwitchProvider


class MockResponse:
    """Mock aiohttp response that works as an async context manager."""

    def __init__(
        self,
        status: int = 200,
        json_data: dict[str, Any] | list[Any] | None = None,
        text_data: str = "",
    ) -> None:
        """Initialize mock response."""
        self.status = status
        self._json_data = json_data
        self._text_data = text_data

    async def json(self) -> dict[str, Any] | list[Any] | None:
        """Return JSON body."""
        return self._json_data

    async def text(self) -> str:
        """Return text body."""
        return self._text_data

    async def __aenter__(self) -> MockResponse:
        """Enter async context."""
        return self

    async def __aexit__(self, *args: object) -> None:
        """Exit async context."""


def make_mock_session_method(
    responses: list[MockResponse] | MockResponse,
) -> Mock:
    """Create a mock HTTP method that returns async context manager responses.

    Accepts a single MockResponse or a list for sequential calls.
    """
    if isinstance(responses, list):
        iterator = iter(responses)

        def side_effect(*args: Any, **kwargs: Any) -> MockResponse:  # noqa: ARG001
            return next(iterator)

        mock = Mock(side_effect=side_effect)
    else:

        def single(*args: Any, **kwargs: Any) -> MockResponse:  # noqa: ARG001
            return responses

        mock = Mock(side_effect=single)
    return mock


@pytest.fixture
def mass_mock() -> Mock:
    """Return a mock MusicAssistant instance."""
    mass = Mock()
    mass.http_session = Mock()
    mass.http_session.ws_connect = AsyncMock()
    mass.subscribe = Mock(return_value=Mock())  # returns unsubscribe callable
    mass.player_queues = Mock()
    mass.player_queues.play_media = AsyncMock()
    mass.cache.get = AsyncMock(return_value=None)
    mass.cache.set = AsyncMock()
    # webserver for AuthenticationHelper
    mass.webserver = Mock()
    mass.webserver.base_url = "http://localhost:8095"
    mass.webserver.register_dynamic_route = Mock()
    mass.webserver.unregister_dynamic_route = Mock()
    mass.signal_event = Mock()
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
