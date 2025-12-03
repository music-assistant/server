"""Fixtures for testing Music Assistant."""

import json
import logging
import pathlib
import socket
from collections.abc import AsyncGenerator

import aiofiles
import pytest

from music_assistant.mass import MusicAssistant


def get_free_port() -> int:
    """Get a free port number.

    :return: Available port number.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        s.listen(1)
        port: int = s.getsockname()[1]
        return port


@pytest.fixture(name="caplog")
def caplog_fixture(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    """Set log level to debug for tests using the caplog fixture."""
    caplog.set_level(logging.DEBUG)
    return caplog


@pytest.fixture
async def mass(tmp_path: pathlib.Path) -> AsyncGenerator[MusicAssistant, None]:
    """Start a Music Assistant in test mode with a random available port.

    :param tmp_path: Temporary directory for test data.
    """
    storage_path = tmp_path / "data"
    cache_path = tmp_path / "cache"
    storage_path.mkdir(parents=True)
    cache_path.mkdir(parents=True)

    logging.getLogger("aiosqlite").level = logging.INFO

    # Get a free port and pre-configure it before starting
    test_port = get_free_port()

    # Create a minimal config file with the test port
    config_file = storage_path / "settings.json"
    config_data = {
        "core": {
            "webserver": {
                "bind_port": test_port,
            }
        }
    }
    async with aiofiles.open(config_file, "w") as f:
        await f.write(json.dumps(config_data))

    mass_instance = MusicAssistant(str(storage_path), str(cache_path))

    await mass_instance.start()

    try:
        yield mass_instance
    finally:
        await mass_instance.stop()
