"""Fixtures for testing Music Assistant."""

import asyncio
import json
import logging
import pathlib
import socket
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, NonCallableMagicMock, patch

import pytest
from zeroconf.asyncio import AsyncZeroconf

from music_assistant.controllers.cache import CacheController
from music_assistant.controllers.config import ConfigController
from music_assistant.controllers.discovery import DiscoveryController
from music_assistant.mass import MusicAssistant


def _find_free_port() -> int:
    """Find a free TCP port by binding to port 0."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return int(s.getsockname()[1])


@pytest.fixture(name="caplog")
def caplog_fixture(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    """Set log level to debug for tests using the caplog fixture."""
    caplog.set_level(logging.DEBUG)
    return caplog


def _create_mock_zeroconf() -> MagicMock:
    """Create a mock AsyncZeroconf that prevents real network I/O.

    Uses spec=AsyncZeroconf to ensure the mock only has valid attributes,
    preventing it from being mistakenly registered as an API handler.
    """
    mock_zc = MagicMock(spec=AsyncZeroconf)
    # Set up nested zeroconf object with proper spec
    mock_inner_zc = NonCallableMagicMock()
    mock_inner_zc.cache = NonCallableMagicMock()
    mock_inner_zc.cache.cache = {}  # Empty cache - no discovered services
    mock_zc.zeroconf = mock_inner_zc
    # Set up async methods
    mock_zc.async_register_service = AsyncMock()
    mock_zc.async_update_service = AsyncMock()
    mock_zc.async_unregister_service = AsyncMock()
    mock_zc.async_close = AsyncMock()
    return mock_zc


@pytest.fixture
async def mass(tmp_path: pathlib.Path) -> AsyncGenerator[MusicAssistant, None]:
    """Start a Music Assistant in test mode.

    :param tmp_path: Temporary directory for test data.
    """
    storage_path = tmp_path / "data"
    cache_path = tmp_path / "cache"
    storage_path.mkdir(parents=True)
    cache_path.mkdir(parents=True)

    logging.getLogger("aiosqlite").level = logging.INFO

    # Pre-write settings with random ports to avoid conflicts with a running MA instance
    web_port = _find_free_port()
    streams_port = _find_free_port()
    settings = {
        "core": {
            "webserver": {"domain": "webserver", "values": {"bind_port": web_port}},
            "streams": {"domain": "streams", "values": {"bind_port": streams_port}},
        }
    }
    (storage_path / "settings.json").write_text(json.dumps(settings))

    mass_instance = MusicAssistant(str(storage_path), str(cache_path))

    # Mock zeroconf and UPnP/SSDP to prevent real network I/O during tests
    mock_zc = _create_mock_zeroconf()
    mock_browser = NonCallableMagicMock()  # Use NonCallable to avoid api_cmd issues

    with (
        patch(
            "music_assistant.controllers.discovery.controller.AsyncZeroconf",
            return_value=mock_zc,
        ),
        patch(
            "music_assistant.controllers.discovery.controller.AsyncServiceBrowser",
            return_value=mock_browser,
        ),
        patch(
            "music_assistant.controllers.discovery.controller.async_upnp_search",
            new_callable=AsyncMock,
        ),
    ):
        await mass_instance.start()

        try:
            yield mass_instance
        finally:
            await mass_instance.stop()


@pytest.fixture
async def mass_minimal(tmp_path: pathlib.Path) -> AsyncGenerator[MusicAssistant, None]:
    """Create a minimal Music Assistant instance without starting the full server.

    Only initializes the event loop and config controller.
    Useful for testing individual controllers without the overhead of the webserver.

    :param tmp_path: Temporary directory for test data.
    """
    storage_path = tmp_path / "data"
    cache_path = tmp_path / "cache"
    storage_path.mkdir(parents=True)
    cache_path.mkdir(parents=True)

    logging.getLogger("aiosqlite").level = logging.INFO

    mass_instance = MusicAssistant(str(storage_path), str(cache_path))

    mass_instance.loop = asyncio.get_running_loop()
    mass_instance.loop_thread_id = (
        getattr(mass_instance.loop, "_thread_id", None)
        if hasattr(mass_instance.loop, "_thread_id")
        else id(mass_instance.loop)
    )

    mass_instance.config = ConfigController(mass_instance)
    await mass_instance.config.setup()
    mass_instance.discovery = DiscoveryController(mass_instance)

    mass_instance.cache = CacheController(mass_instance)

    try:
        yield mass_instance
    finally:
        if mass_instance.cache.database:
            await mass_instance.cache.database.close()
        await mass_instance.config.close()
