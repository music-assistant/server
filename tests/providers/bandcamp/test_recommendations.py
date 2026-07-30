"""Test Bandcamp's two-method recommendations contract."""

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
    # setup_data is unset in these unit tests, so get_setup_value falls through to
    # the provider config's get_value (which the config mock stubs)
    mass.config.get = Mock(return_value=None)
    mass.config.get_raw_provider_config_value = Mock(return_value=None)
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
    config.values = {}
    config.get_value.side_effect = lambda key, default=None: {
        "identity": "mock_identity_token",
        "search_limit": 10,
        "top_tracks_limit": 50,
        "log_level": "INFO",
    }.get(key, default)
    return config


@pytest.fixture
async def provider(mass_mock: Mock, manifest_mock: Mock, config_mock: Mock) -> BandcampProvider:
    """Return a BandcampProvider instance with a reset (call-free) mock client."""
    provider = BandcampProvider(mass_mock, manifest_mock, config_mock, SUPPORTED_FEATURES)

    with patch("music_assistant.providers.bandcamp.BandcampAPIClient") as mock_client_class:
        mock_client_class.return_value = AsyncMock()
        await provider.handle_async_init()

    # Discard the login-validation call from init so tests can assert zero backend calls,
    # and use a real string for identity so the auth gate does not register a mock call.
    provider._client.reset_mock()
    provider._client.identity = "mock_identity_token"
    return provider


async def test_get_recommendations_static_rows_without_backend_calls(
    provider: BandcampProvider,
) -> None:
    """get_recommendations returns the feed and wishlist rows without items or backend I/O."""
    with (
        patch.object(provider, "_get_feed_tracks", new_callable=AsyncMock) as mock_feed,
        patch.object(provider, "_browse_person_content", new_callable=AsyncMock) as mock_wishlist,
    ):
        rows = await provider.get_recommendations()

        mock_feed.assert_not_called()
        mock_wishlist.assert_not_called()
    assert provider._client.mock_calls == []
    assert [f.item_id for f in rows] == ["feed", "wishlist"]
    feed, wishlist = rows
    assert feed.name == "Bandcamp Feed"
    assert feed.translation_key == "feed"
    assert feed.icon == "mdi-rss"
    assert wishlist.name == "Wishlist"
    assert wishlist.translation_key == "wishlist"
    assert wishlist.icon == "mdi-heart"
    assert all(f.provider == "bandcamp_test" for f in rows)
    assert all(f.is_playable for f in rows)
    assert all(not f.items for f in rows)


async def test_get_recommendations_no_identity(provider: BandcampProvider) -> None:
    """get_recommendations returns no rows without an identity token."""
    provider._client.identity = None
    assert await provider.get_recommendations() == []


async def test_get_recommendation_items_feed_fetches_only_feed(
    provider: BandcampProvider,
) -> None:
    """Items for the feed row trigger only the feed fetch."""
    feed_track = Mock()

    with (
        patch.object(provider, "_get_feed_tracks", new_callable=AsyncMock) as mock_feed,
        patch.object(provider, "_browse_person_content", new_callable=AsyncMock) as mock_wishlist,
    ):
        mock_feed.return_value = [feed_track]

        result = await provider.get_recommendation_items("feed")

        mock_feed.assert_awaited_once_with()
        mock_wishlist.assert_not_called()
        assert list(result) == [feed_track]


async def test_get_recommendation_items_wishlist_fetches_only_wishlist(
    provider: BandcampProvider,
) -> None:
    """Items for the wishlist row trigger only the wishlist fetch."""
    wishlist_album = Mock()

    with (
        patch.object(provider, "_get_feed_tracks", new_callable=AsyncMock) as mock_feed,
        patch.object(provider, "_browse_person_content", new_callable=AsyncMock) as mock_wishlist,
    ):
        mock_wishlist.return_value = [wishlist_album]

        result = await provider.get_recommendation_items("wishlist")

        mock_feed.assert_not_called()
        mock_wishlist.assert_awaited_once_with(None, CollectionType.WISHLIST)
        assert list(result) == [wishlist_album]


async def test_get_recommendation_items_unknown_id_returns_empty(
    provider: BandcampProvider,
) -> None:
    """An unknown row item_id returns an empty list without any fetch."""
    with (
        patch.object(provider, "_get_feed_tracks", new_callable=AsyncMock) as mock_feed,
        patch.object(provider, "_browse_person_content", new_callable=AsyncMock) as mock_wishlist,
    ):
        result = await provider.get_recommendation_items("unknown")

        mock_feed.assert_not_called()
        mock_wishlist.assert_not_called()
        assert list(result) == []


async def test_get_recommendation_items_no_identity(provider: BandcampProvider) -> None:
    """Items return empty without an identity token, without any fetch."""
    provider._client.identity = None

    with patch.object(provider, "_get_feed_tracks", new_callable=AsyncMock) as mock_feed:
        result = await provider.get_recommendation_items("feed")

        mock_feed.assert_not_called()
        assert list(result) == []
