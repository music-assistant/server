"""Fixtures for Tidal provider tests."""

from __future__ import annotations

from collections.abc import Generator
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock, patch

import pytest
from music_assistant_models.media_items import ItemMapping

from music_assistant.providers.tidal.media import TidalMediaManager

if TYPE_CHECKING:
    from music_assistant_models.enums import MediaType


@pytest.fixture
def provider_mock() -> Mock:
    """Return a mock Tidal provider with an authenticated session and wired collaborators."""
    provider = Mock()
    provider.domain = "tidal"
    provider.instance_id = "tidal_instance"

    provider.auth.user_id = "12345"
    provider.auth.country_code = "US"
    provider.auth.access_token = "token"
    provider.auth.session_id = "session"
    provider.auth.user.profile_name = "Test User"
    provider.auth.user.user_name = "Test User"
    provider.auth.ensure_valid_token = AsyncMock(return_value=True)
    provider.auth.refresh_token = AsyncMock()

    provider.api = AsyncMock()
    provider.api.get.return_value = {}

    provider.get_track = AsyncMock()
    # Churn-healing collaborators: the cache-only redirect defaults to identity
    # (id is not known-stale) and the reactive resolver to unresolvable.
    provider.redirect_cached_id = AsyncMock(side_effect=lambda item_id: item_id)
    provider.resolve_live_track_id = AsyncMock(return_value=None)

    def get_item_mapping(media_type: MediaType, key: str, name: str) -> ItemMapping:
        return ItemMapping(
            media_type=media_type,
            item_id=key,
            provider=provider.instance_id,
            name=name,
        )

    provider.get_item_mapping.side_effect = get_item_mapping

    provider.mass.http_session = AsyncMock()
    provider.mass.metadata.locale = "en_US"
    provider.mass.config.get_provider_configs = AsyncMock(return_value=[])
    provider.mass.cache.get = AsyncMock(return_value=None)
    provider.mass.cache.set = AsyncMock()
    provider.mass.cache.delete = AsyncMock()
    provider.mass.music.tracks.get_library_item_by_prov_id = AsyncMock(return_value=None)

    return provider


@pytest.fixture
def media_manager(provider_mock: Mock) -> TidalMediaManager:
    """Return a TidalMediaManager instance."""
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
