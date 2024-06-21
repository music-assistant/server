"""Fixtures for testing Music Assistant."""

import logging
import pathlib
from collections.abc import AsyncGenerator

import pytest

from music_assistant.server.server import MusicAssistant
from tests.common import wait_for_sync_completion


@pytest.fixture(name="caplog")
def caplog_fixture(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    """Set log level to debug for tests using the caplog fixture."""
    caplog.set_level(logging.DEBUG)
    return caplog


@pytest.fixture
async def mass(tmp_path: pathlib.Path) -> AsyncGenerator[MusicAssistant, None]:
    """Start a Music Assistant in test mode."""
    storage_path = tmp_path / "root"
    storage_path.mkdir(parents=True)

    logging.getLogger("aiosqlite").level = logging.INFO

    mass = MusicAssistant(str(storage_path))

    async with wait_for_sync_completion(mass):
        await mass.start()

    try:
        yield mass
    finally:
        await mass.stop()
