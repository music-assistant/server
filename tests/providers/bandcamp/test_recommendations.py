"""Test Bandcamp recommendations() row filtering via the `wanted` parameter."""

from unittest.mock import AsyncMock, Mock, patch

import pytest
from bandcamp_async_api.models import CollectionType

from music_assistant.providers.bandcamp import BandcampProvider
from music_assistant.providers.bandcamp.constants import SUPPORTED_FEATURES


@pytest.fixture
def mass_mock() -> Mock:
    """Return a mock MusicAssistant instance."""
    mass = Mock()
    mass.http_session = AsyncMock()
    mass.metadata.locale = "en_US"
    mass.cache.get = AsyncMock(return_value=None)
    mass.cache.get_with_freshness = AsyncMock(return_value=(None, False, False))
    mass.cache.set = AsyncMock()
    mass.cache.delete = AsyncMock()
    return mass


@pytest.fixture
def manifest_mock() -> Mock:
    """Return a mock provider manifest."""
    manifest = Mock()
    manifest.domain = "bandcamp"
    return manifest


@pytest.fixture
def config_mock() -> Mock:
    """Return a mock provider config."""
    config = Mock()
    config.name = "Bandcamp Test"
    config.instance_id = "bandcamp_test"
    config.enabled = True
    config.get_value.side_effect = lambda key, default=None: {
        "identity": "mock_identity_token",
        "search_limit": 10,
        "top_tracks_limit": 50,
        "log_level": "INFO",
    }.get(key, default)
    return config


@pytest.fixture
async def provider(mass_mock: Mock, manifest_mock: Mock, config_mock: Mock) -> BandcampProvider:
    """Return a BandcampProvider instance."""
    provider = BandcampProvider(mass_mock, manifest_mock, config_mock, SUPPORTED_FEATURES)

    with patch("music_assistant.providers.bandcamp.BandcampAPIClient") as mock_client_class:
        mock_client_class.return_value = AsyncMock()
        await provider.handle_async_init()

    return provider


async def test_recommendations_wanted_none_fetches_all_rows(provider: BandcampProvider) -> None:
    """wanted=None (default) fetches and builds both rows — unchanged behavior."""
    feed_track = Mock()
    wishlist_album = Mock()

    with (
        patch.object(provider, "_get_feed_tracks", new_callable=AsyncMock) as mock_feed,
        patch.object(provider, "_browse_person_content", new_callable=AsyncMock) as mock_wishlist,
    ):
        mock_feed.return_value = [feed_track]
        mock_wishlist.return_value = [wishlist_album]

        result = await provider.recommendations()

        mock_feed.assert_awaited_once_with()
        mock_wishlist.assert_awaited_once_with(None, CollectionType.WISHLIST)
        assert [f.item_id for f in result] == ["feed", "wishlist"]


async def test_recommendations_wanted_feed_only_fetches_feed(provider: BandcampProvider) -> None:
    """wanted={"feed"} fetches only the feed and returns only the feed folder."""
    feed_track = Mock()

    with (
        patch.object(provider, "_get_feed_tracks", new_callable=AsyncMock) as mock_feed,
        patch.object(provider, "_browse_person_content", new_callable=AsyncMock) as mock_wishlist,
    ):
        mock_feed.return_value = [feed_track]

        result = await provider.recommendations(wanted={"feed"})

        mock_feed.assert_awaited_once_with()
        mock_wishlist.assert_not_called()
        assert [f.item_id for f in result] == ["feed"]


async def test_recommendations_wanted_wishlist_only_fetches_wishlist(
    provider: BandcampProvider,
) -> None:
    """wanted={"wishlist"} fetches only the wishlist and returns only the wishlist folder."""
    wishlist_album = Mock()

    with (
        patch.object(provider, "_get_feed_tracks", new_callable=AsyncMock) as mock_feed,
        patch.object(provider, "_browse_person_content", new_callable=AsyncMock) as mock_wishlist,
    ):
        mock_wishlist.return_value = [wishlist_album]

        result = await provider.recommendations(wanted={"wishlist"})

        mock_feed.assert_not_called()
        mock_wishlist.assert_awaited_once_with(None, CollectionType.WISHLIST)
        assert [f.item_id for f in result] == ["wishlist"]
