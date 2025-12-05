"""Fixtures for testing Music Assistant."""

import logging
import pathlib
from collections.abc import AsyncGenerator

import pytest

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
