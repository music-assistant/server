"""Shared fixtures for the YouTube Music provider tests."""

from collections.abc import Generator
from unittest.mock import patch

import pytest

from music_assistant.helpers.throttle_retry import ThrottlerManager
from music_assistant.providers.ytmusic import helpers


@pytest.fixture(autouse=True)
def isolated_ytmusic_clients() -> Generator[None]:
    """Give each test its own search client and a throttler that neither waits nor retries."""
    helpers._search_client.cache_clear()
    throttler = ThrottlerManager(rate_limit=1000, retry_attempts=1)
    with patch.object(helpers._YTMusicGate, "throttler", throttler):
        yield
    helpers._search_client.cache_clear()
