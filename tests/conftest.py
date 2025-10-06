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
    """Start a Music Assistant in test mode."""
    storage_path = tmp_path / "data"
    cache_path = tmp_path / "cache"
    storage_path.mkdir(parents=True)
    cache_path.mkdir(parents=True)

    logging.getLogger("aiosqlite").level = logging.INFO

    mass = MusicAssistant(str(storage_path), str(cache_path))

    await mass.start()

    try:
        yield mass
    finally:
        await mass.stop()
