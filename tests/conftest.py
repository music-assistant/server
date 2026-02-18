"""Fixtures for testing Music Assistant."""

import asyncio
import logging
import pathlib
from collections.abc import AsyncGenerator

import pytest

from music_assistant.controllers.cache import CacheController
from music_assistant.controllers.config import ConfigController
from music_assistant.mass import MusicAssistant


@pytest.fixture(name="caplog")
def caplog_fixture(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    """Set log level to debug for tests using the caplog fixture."""
    caplog.set_level(logging.DEBUG)
    return caplog


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

    mass_instance = MusicAssistant(str(storage_path), str(cache_path))

    # TODO: Configure a random port to avoid conflicts when MA is already running
    # The conftest was modified in PR #2738 to add port configuration but it doesn't
    # work correctly - the settings.json file is created but the config isn't respected.
    # For now, tests that use the `mass` fixture will fail if MA is running on port 8095.

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

    mass_instance.cache = CacheController(mass_instance)

    try:
        yield mass_instance
    finally:
        if mass_instance.cache.database:
            await mass_instance.cache.database.close()
        await mass_instance.config.close()
