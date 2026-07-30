"""Test Audible Provider."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import PodcastEpisode

from music_assistant.providers.audible import Audibleprovider
from music_assistant.providers.audible.audible_helper import AudibleHelper


@pytest.fixture
def mass_mock() -> AsyncMock:
    """Return a mock MusicAssistant instance."""
    mass = AsyncMock()
    mass.http_session = AsyncMock()
    mass.cache.get = AsyncMock(return_value=None)
    mass.cache.set = AsyncMock()
    return mass


@pytest.fixture
def audible_client_mock() -> AsyncMock:
    """Return a mock Audible AsyncClient."""
    client = AsyncMock()
    client.post = AsyncMock()
    client.put = AsyncMock()
    return client


@pytest.fixture
def helper(mass_mock: AsyncMock, audible_client_mock: AsyncMock) -> AudibleHelper:
    """Return an AudibleHelper instance."""
    return AudibleHelper(
        mass=mass_mock,
        client=audible_client_mock,
        provider_domain="audible",
        provider_instance="audible_test",
    )


@pytest.fixture
def provider(mass_mock: AsyncMock) -> Audibleprovider:
    """Return an Audibleprovider instance."""
    manifest = MagicMock()
    manifest.domain = "audible"
    config = MagicMock()

    def get_value(key: str) -> str | None:
        if key == "locale":
            return "us"
        if key == "auth_file":
            return "mock_auth_file"
        return None

    config.get_value.side_effect = get_value
    config.get_value.return_value = None  # Default

    # Patch logger setLevel to avoid ValueError with 'us'
    with patch("music_assistant.models.provider.logging.Logger.setLevel"):
        prov = Audibleprovider(mass_mock, manifest, config)

    prov.helper = MagicMock(spec=AudibleHelper)
    return prov


async def test_pagination_get_library(helper: AudibleHelper) -> None:
    """Test get_library uses pagination correctly."""
    # To trigger pagination, the first page must have 50 items (page_size)
    # We generate 50 dummy items for page 1
    page1_items = [
        {
            "asin": f"1_{i}",
            "title": f"Book 1_{i}",
            "content_delivery_type": "SinglePartBook",
            "authors": [],
        }
        for i in range(50)
    ]
    page2_items = [
        {
            "asin": "2_1",
            "title": "Book 2_1",
            "content_delivery_type": "SinglePartBook",
            "authors": [],
        },
    ]

    # Mock side_effect for _call_api
    async def side_effect(_: str, **kwargs: Any) -> dict[str, Any]:
        if kwargs.get("page") == 1:
            return {"items": page1_items, "total_results": 51}
        if kwargs.get("page") == 2:
            return {"items": page2_items, "total_results": 51}
        return {"items": [], "total_results": 51}

    with patch.object(helper, "_call_api", side_effect=side_effect) as mock_call:
        books = []
        async for book in helper.get_library():
            books.append(book)

        # 50 from page 1 + 1 from page 2 = 51
        assert len(books) == 51
        assert books[0].item_id == "1_0"
        assert books[50].item_id == "2_1"

        # Verify pagination calls
        assert mock_call.call_count >= 2
        calls = mock_call.call_args_list
        assert calls[0].kwargs["page"] == 1
        assert calls[1].kwargs["page"] == 2


async def test_pagination_browse_helpers(helper: AudibleHelper) -> None:
    """Test browse helpers (like get_authors) use pagination."""
    # Mock _call_api to return items across pages
    # Page 1 must be full (50 items) to trigger next page
    page1_items = [
        {
            "asin": f"1_{i}",
            "content_delivery_type": "SinglePartBook",
            "authors": [{"asin": f"A1_{i}", "name": f"Author 1_{i}"}],
        }
        for i in range(50)
    ]
    page2_items = [
        {
            "asin": "2_1",
            "content_delivery_type": "SinglePartBook",
            "authors": [{"asin": "A2_1", "name": "Author 2_1"}],
        },
    ]

    async def side_effect(_: str, **kwargs: Any) -> dict[str, Any]:
        if kwargs.get("page") == 1:
            return {"items": page1_items}
        if kwargs.get("page") == 2:
            return {"items": page2_items}
        return {"items": []}

    with patch.object(helper, "_call_api", side_effect=side_effect):
        authors = await helper.get_authors()

        # 50 authors from page 1 + 1 from page 2 = 51
        assert len(authors) == 51
        assert authors["A1_0"] == "Author 1_0"
        assert authors["A2_1"] == "Author 2_1"


async def test_acr_caching(helper: AudibleHelper, audible_client_mock: AsyncMock) -> None:
    """Test ACR is cached and used for set_last_position."""
    asin = "B001"

    # Mock get_stream response
    audible_client_mock.post.return_value = {
        "content_license": {
            "acr": "test_acr_value",
            "license_response": "http://stream.url",
            "content_metadata": {"content_reference": {"content_size_in_bytes": 1000}},
        }
    }

    # 1. Call get_stream to populate cache
    await helper.get_stream(asin, MediaType.AUDIOBOOK)
    assert (asin, MediaType.AUDIOBOOK) in helper._acr_cache
    assert helper._acr_cache[(asin, MediaType.AUDIOBOOK)] == "test_acr_value"

    # Reset mock to ensure it's not called again if we were to call get_stream
    # (but we check cache usage in set_last_position)
    audible_client_mock.post.reset_mock()

    # 2. Call set_last_position -> should use cache and NOT call get_stream
    # (which calls client.post)
    # We patch get_stream to verify it's NOT called
    with patch.object(helper, "get_stream") as mock_get_stream:
        await helper.set_last_position(asin, 10, MediaType.AUDIOBOOK)

        mock_get_stream.assert_not_called()
        audible_client_mock.put.assert_called_once()
        call_args = audible_client_mock.put.call_args[1]
        assert call_args["body"]["acr"] == "test_acr_value"


async def test_set_last_position_without_cache(
    helper: AudibleHelper, audible_client_mock: AsyncMock
) -> None:
    """Test set_last_position fetches ACR if not in cache."""
    asin = "B002"

    # Mock get_stream internal call
    with patch.object(helper, "get_stream") as mock_get_stream:
        mock_get_stream.return_value.data = {"acr": "fetched_acr"}

        await helper.set_last_position(asin, 10, MediaType.AUDIOBOOK)

        mock_get_stream.assert_called_once_with(asin=asin, media_type=MediaType.AUDIOBOOK)
        audible_client_mock.put.assert_called_once()
        call_args = audible_client_mock.put.call_args[1]
        assert call_args["body"]["acr"] == "fetched_acr"


async def test_podcast_parent_fallback(helper: AudibleHelper) -> None:
    """Test podcast episode parsing handles missing parent ASIN."""
    episode_data = {
        "asin": "ep1",
        "title": "Episode 1",
        "relationships": [],  # No parent relationship
    }

    # Should not raise error, but log warning and use empty/self ASIN for parent
    episode = helper._parse_podcast_episode(episode_data, None, 0)

    assert isinstance(episode, PodcastEpisode)
    assert episode.podcast.item_id == ""


async def test_browse_decoding(provider: Audibleprovider) -> None:
    """Test browse path decoding."""
    # We need to test the provider's browse method, not the helper's.
    # We mocked the helper in the provider fixture.

    # Mock helper methods to return empty lists/dicts so we just check calls
    provider.helper.get_audiobooks_by_author = AsyncMock(return_value=[])  # type: ignore[method-assign]
    provider.helper.get_audiobooks_by_genre = AsyncMock(return_value=[])  # type: ignore[method-assign]

    # Test Author with special chars
    await provider.browse("audible://authors/Author%20Name")
    provider.helper.get_audiobooks_by_author.assert_called_with("Author Name")

    # Test Genre with slash (encoded)
    await provider.browse("audible://genres/Sci-Fi%2FFantasy")
    provider.helper.get_audiobooks_by_genre.assert_called_with("Sci-Fi/Fantasy")
