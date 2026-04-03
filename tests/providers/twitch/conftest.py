"""Shared fixtures for Twitch provider tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from music_assistant.providers.twitch import SUPPORTED_FEATURES, TwitchProvider

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict[str, Any]:
    """Load a JSON fixture file by name."""
    with (FIXTURES / name).open() as f:
        return json.load(f)  # type: ignore[no-any-return]


class MockResponse:
    """Mock aiohttp response that works as an async context manager.

    Behavioral contract:
    - .json() raises ValueError on non-2xx when json_data was not explicitly provided,
      matching real aiohttp behavior where error responses often aren't valid JSON.
    - .json() returns json_data when explicitly provided, even on error status codes,
      since some error responses have JSON bodies.
    - Accepts optional headers dict for testing header-dependent code paths.
    """

    _NO_JSON = object()  # sentinel to distinguish "not provided" from None

    def __init__(
        self,
        status: int = 200,
        json_data: dict[str, Any] | list[Any] | None = _NO_JSON,  # type: ignore[assignment]
        text_data: str = "",
        headers: dict[str, str] | None = None,
    ) -> None:
        """Initialize mock response."""
        self.status = status
        self._json_explicit = json_data is not MockResponse._NO_JSON
        self._json_data = json_data if self._json_explicit else None
        self._text_data = text_data
        self.headers = headers or {}

    async def json(self) -> dict[str, Any] | list[Any] | None:
        """Return JSON body. Raises ValueError on error status when json_data not provided."""
        if not self._json_explicit and self.status >= 400:
            msg = f"Cannot parse JSON from error response (status={self.status})"
            raise ValueError(msg)
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
        expected_count = len(responses)
        call_count = 0

        def side_effect(*args: Any, **kwargs: Any) -> MockResponse:  # noqa: ARG001
            nonlocal call_count
            call_count += 1
            try:
                return next(iterator)
            except StopIteration:
                msg = (
                    f"MockResponse list exhausted: expected {expected_count} calls, "
                    f"got call #{call_count}. Add more MockResponse entries or "
                    f"assert the correct call count."
                )
                raise RuntimeError(msg) from None

        mock = Mock(side_effect=side_effect)
    else:

        def single(*args: Any, **kwargs: Any) -> MockResponse:  # noqa: ARG001
            return responses

        mock = Mock(side_effect=single)
    return mock


_BASE_CONFIG: dict[str, Any] = {
    "client_id": "",
    "client_secret": "",
    "streamlink_token": "",
    "auto_raid": True,
    "log_level": "GLOBAL",
    "access_token": "",
    "refresh_token": "",
}


def config_side_effect(overrides: dict[str, Any] | None = None) -> Any:
    """Return a side_effect callable for config.get_value with optional overrides."""
    values = {**_BASE_CONFIG, **(overrides or {})}
    return lambda key, default=None: values.get(key, default)


@pytest.fixture
def mass_mock() -> Mock:
    """Return a mock MusicAssistant instance."""
    mass = Mock()
    mass.http_session = Mock()
    mass.http_session.ws_connect = AsyncMock()
    mass.player_queues = Mock()
    mass.player_queues.play_media = AsyncMock()
    mass.cache.get = AsyncMock(return_value=None)
    mass.cache.set = AsyncMock()
    mass.config.set_raw_provider_config_value = Mock()
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
        "auto_raid": True,
        "log_level": "GLOBAL",
    }.get(key, default)
    # Mock config.values as a defaultdict-like dict for _update_config_value
    config.values = {}

    class _ValueHolder:
        """Hold a config value for mock purposes."""

        def __init__(self) -> None:
            self.value: Any = None

    class _AutoValues(dict):  # type: ignore[type-arg]
        """Auto-create value holders on access."""

        def __missing__(self, key: str) -> _ValueHolder:
            holder = _ValueHolder()
            self[key] = holder
            return holder

    config.values = _AutoValues()
    return config


@pytest.fixture
def provider(mass_mock: Mock, manifest_mock: Mock, config_mock: Mock) -> TwitchProvider:
    """Return a TwitchProvider instance."""
    p = TwitchProvider(mass_mock, manifest_mock, config_mock, SUPPORTED_FEATURES)
    # Initialize raid state that would normally be set by handle_async_init
    p._active_streams = {}
    p._unsubscribe_timers = {}
    return p
