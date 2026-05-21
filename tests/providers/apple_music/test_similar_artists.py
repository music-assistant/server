"""Unit tests for Apple Music get_similar_artists."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.media_items import Artist

from music_assistant.providers.apple_music.recommendations import AppleMusicRecommendationManager


def _make_artist_obj(artist_id: str, name: str) -> dict[str, object]:
    return {
        "id": artist_id,
        "type": "artists",
        "attributes": {
            "name": name,
            "url": f"https://music.apple.com/artist/{artist_id}",
        },
        "relationships": {},
    }


def _make_api_response(artist_objects: list[dict[str, object]]) -> dict[str, object]:
    return {
        "data": [
            {
                "id": "123",
                "views": {
                    "similar-artists": {
                        "data": artist_objects,
                    }
                },
            }
        ]
    }


@pytest.fixture
def mock_api() -> MagicMock:
    """Return a MagicMock representing the Apple Music API client."""
    api_client = MagicMock()
    api_client.get_data = AsyncMock()
    return api_client


@pytest.fixture
def manager(mock_api: MagicMock) -> AppleMusicRecommendationManager:
    """Return an AppleMusicRecommendationManager wired to a mock API client."""
    provider = MagicMock()
    provider.instance_id = "apple_music_test"
    provider.domain = "apple_music"
    provider._storefront = "us"
    provider.logger = MagicMock()
    provider.mass.cache.get = AsyncMock(return_value=None)
    provider.mass.cache.set = AsyncMock()
    provider.api_client = mock_api

    return AppleMusicRecommendationManager(provider)


@pytest.mark.asyncio
async def test_get_similar_artists_returns_artists(
    manager: AppleMusicRecommendationManager,
    mock_api: MagicMock,
) -> None:
    """get_similar_artists parses artists from the views.similar-artists response."""
    mock_api.get_data.return_value = _make_api_response(
        [
            _make_artist_obj("456", "Radiohead"),
            _make_artist_obj("789", "Portishead"),
        ]
    )

    result = await manager.get_similar_artists("123", limit=25)

    mock_api.get_data.assert_called_once_with(
        "catalog/us/artists/123",
        views="similar-artists",
    )
    assert len(result) == 2
    assert all(isinstance(a, Artist) for a in result)
    names = {a.name for a in result}
    assert "Radiohead" in names
    assert "Portishead" in names


@pytest.mark.asyncio
async def test_get_similar_artists_respects_limit(
    manager: AppleMusicRecommendationManager,
    mock_api: MagicMock,
) -> None:
    """get_similar_artists truncates results to the requested limit."""
    many_artists = [_make_artist_obj(str(i), f"Artist {i}") for i in range(10)]
    mock_api.get_data.return_value = _make_api_response(many_artists)

    result = await manager.get_similar_artists("123", limit=3)

    assert len(result) == 3


@pytest.mark.asyncio
async def test_get_similar_artists_empty_data(
    manager: AppleMusicRecommendationManager,
    mock_api: MagicMock,
) -> None:
    """get_similar_artists returns empty list when API returns no data."""
    mock_api.get_data.return_value = {"data": []}

    result = await manager.get_similar_artists("123")

    assert result == []


@pytest.mark.asyncio
async def test_get_similar_artists_missing_view(
    manager: AppleMusicRecommendationManager,
    mock_api: MagicMock,
) -> None:
    """get_similar_artists returns empty list when similar-artists view is absent."""
    mock_api.get_data.return_value = {"data": [{"id": "123", "views": {}}]}

    result = await manager.get_similar_artists("123")

    assert result == []


@pytest.mark.asyncio
async def test_get_similar_artists_api_error(
    manager: AppleMusicRecommendationManager,
    mock_api: MagicMock,
) -> None:
    """get_similar_artists propagates exceptions so they are not cached."""
    mock_api.get_data.side_effect = Exception("API error")

    with pytest.raises(Exception, match="API error"):
        await manager.get_similar_artists("123")
