"""Fixtures for Tidal provider tests."""

from collections.abc import Generator
from unittest.mock import AsyncMock, Mock, patch

import pytest

from music_assistant.providers.tidal.media import TidalMediaManager


@pytest.fixture
def media_manager(provider_mock: Mock) -> TidalMediaManager:
    """Return a TidalMediaManager instance."""
    # provider_mock is defined per test module and differs between them, so a module
    # requesting this fixture must bring its own
    return TidalMediaManager(provider_mock)


@pytest.fixture(autouse=True)
def no_throttling() -> Generator[None]:
    """
    Disable rate limiting and retry backoff during tests.

    The API client's throttler is class-level shared state: its real-time
    rate window would otherwise carry over between tests and make every
    test wait it out.

    Note: the sleep patch targets the attribute on the shared asyncio
    module, so asyncio.sleep is mocked process-wide while each test in
    this directory runs. Keep that in mind for timing-dependent tests.
    """
    with (
        patch(
            "music_assistant.helpers.throttle_retry.Throttler.acquire",
            new=AsyncMock(return_value=0.0),
        ),
        patch(
            "music_assistant.helpers.throttle_retry.asyncio.sleep",
            new_callable=AsyncMock,
        ),
    ):
        yield
