"""Test Audible Provider."""

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import audible
import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import PodcastEpisode

from music_assistant.providers.audible import Audibleprovider
from music_assistant.providers.audible.audible_helper import (
    AudibleHelper,
    cached_authenticator_from_file,
    evict_cached_authenticator,
)


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
        provider=MagicMock(),
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


def _mock_auth(locale: str) -> MagicMock:
    """Return a mock Authenticator with signing auth and the given locale."""
    auth = MagicMock()
    auth.adp_token = "adp_token"
    auth.device_private_key = "private_key"
    auth.locale = audible.localization.Locale(locale)
    return auth


def _write_auth_file(path: Path, locale_code: str) -> None:
    """Write a syntactically valid auth file with dummy tokens and the given locale."""
    # assembled at runtime so the detect-private-key hook does not flag the dummy value
    pem = "RSA PRIVATE " + "KEY-----"
    path.write_text(
        json.dumps(
            {
                "website_cookies": {"session-id": "dummy"},
                "adp_token": "{enc:x}{key:x}{iv:x}{name:x}{serial:Mg==}",
                "access_token": "Atna|dummy",
                "refresh_token": "Atnr|dummy",
                "device_private_key": f"-----BEGIN {pem}\ndummy\n-----END {pem}\n",
                "expires": 9999999999.0,
                "locale_code": locale_code,
                "device_info": {"device_serial_number": "dummy"},
                "customer_info": {"name": "dummy"},
                "with_username": False,
            }
        )
    )


async def test_cached_authenticator_corrects_locale_mismatch(tmp_path: Path) -> None:
    """An auth file holding a stale marketplace is corrected to the configured locale."""
    path = tmp_path / "auth.json"
    _write_auth_file(path, "us")

    auth = await cached_authenticator_from_file(str(path), "de")

    assert auth.locale is not None
    assert auth.locale.country_code == "de"
    assert auth.locale.domain == "de"
    assert json.loads(path.read_text())["locale_code"] == "de"
    evict_cached_authenticator(str(path))


async def test_cached_authenticator_keeps_matching_locale(tmp_path: Path) -> None:
    """An auth file matching the configured locale is left untouched."""
    path = str(tmp_path / "auth.json")
    auth = _mock_auth("de")
    with patch("audible.Authenticator.from_file", return_value=auth):
        result = await cached_authenticator_from_file(path, "de")

    assert result is auth
    auth.to_file.assert_not_called()
    evict_cached_authenticator(path)


async def test_cached_authenticator_loads_new_file_after_reauth(tmp_path: Path) -> None:
    """A new auth file written by a reconfigure is loaded instead of a stale authenticator."""
    old_path = str(tmp_path / "old.json")
    new_path = str(tmp_path / "new.json")
    old_auth = _mock_auth("us")
    new_auth = _mock_auth("de")
    with patch("audible.Authenticator.from_file", side_effect=[old_auth, new_auth]):
        assert await cached_authenticator_from_file(old_path, "us") is old_auth
        assert await cached_authenticator_from_file(new_path, "de") is new_auth

    evict_cached_authenticator(old_path)
    evict_cached_authenticator(new_path)


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


async def test_get_library_podcasts_includes_legacy_periodicals(helper: AudibleHelper) -> None:
    """Test podcast sync also picks up series with the legacy Periodical delivery type."""
    library_items = [
        {
            "asin": "P1",
            "title": "Modern Podcast",
            "content_delivery_type": "PodcastParent",
        },
        {
            "asin": "P2",
            "title": "Audible Original Show",
            "content_delivery_type": "Periodical",
        },
        {
            "asin": "B1",
            "title": "Some Book",
            "content_delivery_type": "SinglePartBook",
        },
    ]

    async def side_effect(_: str, **kwargs: Any) -> dict[str, Any]:
        if kwargs.get("page") == 1:
            return {"items": library_items}
        return {"items": []}

    with patch.object(helper, "_call_api", side_effect=side_effect):
        podcasts = [podcast async for podcast in helper.get_library_podcasts()]

    assert [podcast.item_id for podcast in podcasts] == ["P1", "P2"]


async def test_podcast_episodes_ranked_by_publication_datetime(helper: AudibleHelper) -> None:
    """Episodes are positioned by publication time, not the order the API returns them."""
    # a serialised show: listed oldest-first, with two episodes sharing a release_date
    episodes = [
        {
            "asin": "trailer",
            "title": "Trailer",
            "relationships": [],
            "release_date": "2022-08-17",
            "publication_datetime": "2022-08-17T04:27:10Z",
        },
        {
            "asin": "ep2",
            "title": "Ep2",
            "relationships": [],
            "release_date": "2022-08-21",
            "publication_datetime": "2022-08-21T15:02:44Z",
        },
        {
            "asin": "ep3",
            "title": "Ep3",
            "relationships": [],
            "release_date": "2022-08-21",
            "publication_datetime": "2022-08-21T15:04:34Z",
        },
    ]

    async def side_effect(_: str, **kwargs: Any) -> dict[str, Any]:
        return {"items": episodes} if kwargs.get("page") == 1 else {"items": []}

    helper._call_api = AsyncMock(side_effect=side_effect)  # type: ignore[method-assign]
    helper.get_podcast = AsyncMock(return_value=None)  # type: ignore[method-assign]

    parsed = [ep async for ep in helper.get_podcast_episodes("parent")]

    assert {ep.item_id: ep.position for ep in parsed} == {"trailer": 1, "ep2": 2, "ep3": 3}


async def test_podcast_episodes_reverse_a_newest_first_listing(helper: AudibleHelper) -> None:
    """A show listed newest-first gets its newest episode the highest position."""
    episodes = [
        {
            "asin": "new",
            "title": "Newest",
            "relationships": [],
            "publication_datetime": "2026-08-24T04:01:00Z",
        },
        {
            "asin": "mid",
            "title": "Middle",
            "relationships": [],
            "publication_datetime": "2026-08-10T04:01:00Z",
        },
        {
            "asin": "old",
            "title": "Oldest",
            "relationships": [],
            "publication_datetime": "2026-07-27T04:01:00Z",
        },
    ]

    async def side_effect(_: str, **kwargs: Any) -> dict[str, Any]:
        return {"items": episodes} if kwargs.get("page") == 1 else {"items": []}

    helper._call_api = AsyncMock(side_effect=side_effect)  # type: ignore[method-assign]
    helper.get_podcast = AsyncMock(return_value=None)  # type: ignore[method-assign]

    parsed = [ep async for ep in helper.get_podcast_episodes("parent")]

    assert {ep.item_id: ep.position for ep in parsed} == {"new": 3, "mid": 2, "old": 1}


async def test_legacy_show_episodes_use_the_listing_order(helper: AudibleHelper) -> None:
    """A legacy series is released in one go, so its listing order decides the position."""
    # every episode shares one publication timestamp, so the dates rank nothing
    episodes = [
        {
            "asin": "ep5",
            "title": "Ep 5",
            "relationships": [],
            "content_type": "Show",
            "publication_datetime": "2021-02-09T00:00:00Z",
        },
        {
            "asin": "ep4",
            "title": "Ep 4",
            "relationships": [],
            "content_type": "Show",
            "publication_datetime": "2021-02-09T00:00:00Z",
        },
        {
            "asin": "ep3",
            "title": "Ep 3",
            "relationships": [],
            "content_type": "Show",
            "publication_datetime": "2021-02-09T00:00:00Z",
        },
    ]

    async def side_effect(_: str, **kwargs: Any) -> dict[str, Any]:
        return {"items": episodes} if kwargs.get("page") == 1 else {"items": []}

    helper._call_api = AsyncMock(side_effect=side_effect)  # type: ignore[method-assign]
    helper.get_podcast = AsyncMock(return_value=None)  # type: ignore[method-assign]

    parsed = [ep async for ep in helper.get_podcast_episodes("parent")]

    assert {ep.item_id: ep.position for ep in parsed} == {"ep5": 3, "ep4": 2, "ep3": 1}
