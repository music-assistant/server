"""Test the iTunes Podcasts two-method recommendations contract."""

from unittest.mock import AsyncMock, Mock, patch

import pytest

from music_assistant.providers.itunes_podcasts import (
    RECOMMENDATION_ROW_TOP_PODCASTS,
    SUPPORTED_FEATURES,
    ITunesPodcastsProvider,
)
from music_assistant.providers.itunes_podcasts.schema import PodcastSearchResult, TopPodcastsHelper


@pytest.fixture
def mass_mock() -> Mock:
    """Return a mock MusicAssistant instance."""
    mass = Mock()
    mass.http_session = AsyncMock()
    mass.cache.get = AsyncMock(return_value=None)
    mass.cache.set = AsyncMock()
    return mass


@pytest.fixture
def manifest_mock() -> Mock:
    """Return a mock provider manifest."""
    manifest = Mock()
    manifest.domain = "itunes_podcasts"
    return manifest


@pytest.fixture
def config_mock() -> Mock:
    """Return a mock provider config."""
    config = Mock()
    config.name = "iTunes Podcasts Test"
    config.instance_id = "itunes_podcasts_test"
    config.enabled = True
    config.get_value.side_effect = lambda key, default=None: {
        "locale": "us",
        "explicit": True,
        "num_episodes": 0,
        "log_level": "INFO",
    }.get(key, default)
    return config


@pytest.fixture
async def provider(
    mass_mock: Mock, manifest_mock: Mock, config_mock: Mock
) -> ITunesPodcastsProvider:
    """Return an ITunesPodcastsProvider instance."""
    provider = ITunesPodcastsProvider(mass_mock, manifest_mock, config_mock, SUPPORTED_FEATURES)
    await provider.handle_async_init()
    return provider


def _search_result() -> PodcastSearchResult:
    """Return a minimal top-podcast search result."""
    return PodcastSearchResult(
        track_name="Test Podcast",
        artist_name="Test Publisher",
        feed_url="https://example.com/feed.xml",
        artwork_url_600="https://example.com/artwork600.jpg",
    )


async def test_get_recommendations_static_row_without_backend_calls(
    provider: ITunesPodcastsProvider, mass_mock: Mock
) -> None:
    """get_recommendations() returns the single static row without any backend or cache I/O."""
    result = await provider.get_recommendations()

    assert [folder.item_id for folder in result] == [RECOMMENDATION_ROW_TOP_PODCASTS]
    folder = result[0]
    assert folder.name == "Trending Podcasts"
    assert folder.translation_key == "trending_podcasts"
    assert folder.icon == "mdi-trending-up"
    assert folder.provider == "itunes_podcasts_test"
    assert len(folder.items) == 0
    mass_mock.http_session.get.assert_not_called()
    mass_mock.cache.get.assert_not_called()


async def test_get_recommendation_items_fetches_top_podcasts(
    provider: ITunesPodcastsProvider,
) -> None:
    """get_recommendation_items(<row>) triggers the top-podcasts fetch and returns its podcasts."""
    with patch.object(
        provider, "_cache_get_top_podcasts", new_callable=AsyncMock
    ) as mock_top_podcasts:
        mock_top_podcasts.return_value = [_search_result()]

        items = await provider.get_recommendation_items(RECOMMENDATION_ROW_TOP_PODCASTS)

    mock_top_podcasts.assert_awaited_once_with()
    assert [item.item_id for item in items] == ["https://example.com/feed.xml"]
    assert items[0].name == "Test Podcast"


async def test_get_recommendation_items_served_from_cache(
    provider: ITunesPodcastsProvider, mass_mock: Mock
) -> None:
    """A cached top-podcasts payload serves the row's items without any http calls."""
    helper = TopPodcastsHelper(top_podcasts=[_search_result()])
    mass_mock.cache.get = AsyncMock(return_value=helper.to_dict())

    items = await provider.get_recommendation_items(RECOMMENDATION_ROW_TOP_PODCASTS)

    assert [item.item_id for item in items] == ["https://example.com/feed.xml"]
    mass_mock.http_session.get.assert_not_called()


async def test_get_recommendation_items_unknown_id_returns_empty(
    provider: ITunesPodcastsProvider, mass_mock: Mock
) -> None:
    """An unknown row item_id returns an empty result without triggering any fetch."""
    with patch.object(
        provider, "_cache_get_top_podcasts", new_callable=AsyncMock
    ) as mock_top_podcasts:
        items = await provider.get_recommendation_items("unknown-row")

    assert len(items) == 0
    mock_top_podcasts.assert_not_called()
    mass_mock.http_session.get.assert_not_called()
