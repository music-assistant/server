"""Test Tidal Recommendation Manager."""

from unittest.mock import AsyncMock, Mock, patch

import pytest
from music_assistant_models.enums import MediaType

from music_assistant.providers.tidal.recommendations import TidalRecommendationManager


@pytest.fixture
def provider_mock() -> Mock:
    """Return a mock provider."""
    provider = Mock()
    provider.domain = "tidal"
    provider.instance_id = "tidal_instance"
    provider.auth.user_id = "12345"
    provider.auth.country_code = "US"
    provider.api = AsyncMock()
    provider.logger = Mock()

    # Mock mass
    provider.mass = Mock()
    provider.mass.config.get_provider_configs = AsyncMock(return_value=[])
    provider.mass.metadata.locale = "en_US"
    provider.mass.cache.set = AsyncMock()

    return provider


@pytest.fixture
def recommendation_manager(provider_mock: Mock) -> TidalRecommendationManager:
    """Return a TidalRecommendationManager instance."""
    return TidalRecommendationManager(provider_mock)


@pytest.mark.usefixtures("provider_mock")
async def test_get_recommendations(
    recommendation_manager: TidalRecommendationManager,
) -> None:
    """Test get_recommendations."""
    # Mock get_page_content to return a mock parser
    mock_parser = Mock()
    mock_parser._module_map = [{"title": "Test Module"}]
    mock_parser.get_module_items.return_value = (
        [Mock(item_id="rec_1", name="Recommendation 1")],
        MediaType.PLAYLIST,
    )

    with patch.object(
        recommendation_manager, "get_page_content", new_callable=AsyncMock
    ) as mock_get_page:
        mock_get_page.return_value = mock_parser

        recommendations = await recommendation_manager.get_recommendations()

        assert len(recommendations) == 1
        assert recommendations[0].name == "Test Module"
        assert len(recommendations[0].items) == 1

        # Should fetch pages
        assert mock_get_page.call_count >= 1


async def test_get_page_content(
    recommendation_manager: TidalRecommendationManager, provider_mock: Mock
) -> None:
    """Test get_page_content."""
    with patch(
        "music_assistant.providers.tidal.recommendations.TidalPageParser"
    ) as mock_parser_cls:
        # Configure from_cache to be async and return None
        mock_parser_cls.from_cache = AsyncMock(return_value=None)

        # Configure parser instance
        mock_parser_instance = mock_parser_cls.return_value
        mock_parser_instance._module_map = []
        mock_parser_instance._content_map = {}
        mock_parser_instance._parsed_at = 1234567890
        mock_parser_instance.parse_page_structure = Mock()  # Ensure it's a synchronous mock

        # Mock API response
        provider_mock.api.get.return_value = ({"rows": []}, "etag")

        parser = await recommendation_manager.get_page_content("pages/home")

        assert parser == mock_parser_instance

        # Should check cache
        mock_parser_cls.from_cache.assert_called_with(provider_mock, "pages/home")

        # Should fetch from API
        provider_mock.api.get.assert_called()

        # Should parse structure
        mock_parser_instance.parse_page_structure.assert_called()

        # Should cache result
        provider_mock.mass.cache.set.assert_called()
