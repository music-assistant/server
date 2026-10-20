"""Test Tidal recommendations: payload manager + two-method provider contract."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import MediaNotFoundError, ResourceTemporarilyUnavailable
from music_assistant_models.media_items import (
    ItemMapping,
    RecommendationFolder,
    UniqueList,
)

from music_assistant.providers.tidal.constants import MAX_PAGE_MODULE_ITEMS
from music_assistant.providers.tidal.provider import TidalProvider
from music_assistant.providers.tidal.recommendations import TidalRecommendationManager

INSTANCE_ID = "tidal_test"

DataHandler = Callable[..., Awaitable[dict[str, Any]]]


@pytest.fixture
def recommendation_manager(provider_mock: Mock) -> TidalRecommendationManager:
    """Return a TidalRecommendationManager instance."""
    return TidalRecommendationManager(provider_mock)


@pytest.fixture
def mass_mock() -> Mock:
    """Return a mock MusicAssistant with a dict-backed cache and real background tasks."""
    mass = Mock()
    mass.http_session = AsyncMock()
    mass.metadata.locale = "en_US"

    cache_store: dict[str, Any] = {}
    background_tasks: list[asyncio.Future[Any]] = []

    async def _cache_get(key: str, **_kwargs: Any) -> tuple[Any, bool, bool]:
        if key in cache_store:
            return cache_store[key], True, True
        return None, False, False

    async def _cache_set(key: str, data: Any, **_kwargs: Any) -> None:
        cache_store[key] = data

    def _create_task(target: Any, *_args: Any, **_kwargs: Any) -> asyncio.Future[Any]:
        task: asyncio.Future[Any] = asyncio.ensure_future(target)
        background_tasks.append(task)
        return task

    mass.cache.get = AsyncMock(return_value=None)
    mass.cache.get_with_freshness = AsyncMock(side_effect=_cache_get)
    mass.cache.set = AsyncMock(side_effect=_cache_set)
    mass.create_task = Mock(side_effect=_create_task)
    mass.background_tasks = background_tasks
    return mass


@pytest.fixture
def provider(mass_mock: Mock) -> TidalProvider:
    """Return a TidalProvider instance for provider-level tests."""
    manifest = Mock()
    manifest.domain = "tidal"

    config = Mock()
    config.name = "Tidal Test"
    config.instance_id = INSTANCE_ID
    config.enabled = True
    config.get_value.side_effect = lambda key: "INFO" if "log" in key else None

    return TidalProvider(mass_mock, manifest, config)


def _make_payload() -> list[RecommendationFolder]:
    """Build a two-folder payload, shaped like the manager's merged page modules."""
    return [
        RecommendationFolder(
            item_id="12345_for_you",
            provider=INSTANCE_ID,
            name="For You",
            translation_key="12345_for_you",
            icon="mdi-playlist-music",
            subtitle="From Home • 1 items",
            items=UniqueList(
                [
                    ItemMapping(
                        media_type=MediaType.PLAYLIST,
                        item_id="p1",
                        provider=INSTANCE_ID,
                        name="Daily Mix",
                    )
                ]
            ),
        ),
        RecommendationFolder(
            item_id="12345_new_albums",
            provider=INSTANCE_ID,
            name="New Albums",
            translation_key="12345_new_albums",
            icon="mdi-album",
            subtitle="From Explore New Music • 1 items",
            items=UniqueList(
                [
                    ItemMapping(
                        media_type=MediaType.ALBUM,
                        item_id="a1",
                        provider=INSTANCE_ID,
                        name="New Album",
                    )
                ]
            ),
        ),
    ]


async def test_get_recommendations_returns_rows_without_items(provider: TidalProvider) -> None:
    """get_recommendations returns the payload folders as rows, stripped of items."""
    payload = _make_payload()
    with patch.object(
        provider.recommendations_manager, "get_recommendations", new_callable=AsyncMock
    ) as mock_fetch:
        mock_fetch.return_value = payload

        rows = await provider.get_recommendations()

    mock_fetch.assert_awaited_once()
    assert [row.item_id for row in rows] == ["12345_for_you", "12345_new_albums"]
    for row, folder in zip(rows, payload, strict=True):
        assert row.name == folder.name
        assert row.translation_key == folder.translation_key
        assert row.icon == folder.icon
        assert len(row.items) == 0


async def test_rows_and_items_share_one_payload_fetch(
    provider: TidalProvider, mass_mock: Mock
) -> None:
    """Rows and both items calls are served from a single cached payload fetch."""
    payload = _make_payload()
    with patch.object(
        provider.recommendations_manager, "get_recommendations", new_callable=AsyncMock
    ) as mock_fetch:
        mock_fetch.return_value = payload

        await provider.get_recommendations()
        # let the background cache-store task complete before the next calls
        await asyncio.gather(*mass_mock.background_tasks)
        items_for_you = await provider.get_recommendation_items("12345_for_you")
        items_new_albums = await provider.get_recommendation_items("12345_new_albums")

    mock_fetch.assert_awaited_once()
    assert items_for_you == payload[0].items
    assert items_new_albums == payload[1].items


async def test_get_recommendation_items_unknown_id_returns_empty(
    provider: TidalProvider,
) -> None:
    """An unknown row item_id yields an empty UniqueList."""
    with patch.object(
        provider.recommendations_manager, "get_recommendations", new_callable=AsyncMock
    ) as mock_fetch:
        mock_fetch.return_value = _make_payload()

        result = await provider.get_recommendation_items("bogus_row")

    assert isinstance(result, UniqueList)
    assert len(result) == 0


@pytest.mark.usefixtures("provider_mock")
async def test_manager_builds_payload_folders(
    recommendation_manager: TidalRecommendationManager,
) -> None:
    """The manager merges page modules into folders with items and stable item_ids."""
    # Mock get_page_content to return a mock parser
    mock_parser = Mock()
    mock_parser.modules = [{"title": "Test Module"}]
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
        assert recommendations[0].item_id == "12345_test_module"
        assert len(recommendations[0].items) == 1

        # Should fetch pages
        assert mock_get_page.call_count >= 1


async def test_manager_filters_out_video_modules(
    recommendation_manager: TidalRecommendationManager,
) -> None:
    """Test video modules never surface, by VIDEO_LIST type or a video-mentioning title."""
    parser = Mock()
    parser.modules = [
        {"title": "Video Playlists", "type": "PLAYLIST_LIST"},  # title-based drop
        {"title": "New Videos", "type": "VIDEO_LIST"},  # both
        {"title": "Clips", "type": "VIDEO_LIST"},  # type-based drop
        {"title": "Playlists", "type": "PLAYLIST_LIST"},  # survives
    ]
    parser.get_module_items.return_value = (
        [Mock(item_id="p1", name="P1")],
        MediaType.PLAYLIST,
    )

    with patch.object(
        recommendation_manager, "get_page_content", new_callable=AsyncMock
    ) as mock_get_page:
        mock_get_page.return_value = parser

        recommendations = await recommendation_manager.get_recommendations()

    names = {r.name for r in recommendations}
    assert names == {"Playlists"}


async def test_manager_strips_at_symbol_when_multiple_instances(
    recommendation_manager: TidalRecommendationManager, provider_mock: Mock
) -> None:
    """Test that username is included and '@' is stripped when multiple instances exist."""
    provider_mock.auth.user = Mock(profile_name="john@domain.tld", user_name=None)

    provider_mock.mass.config.get_provider_configs = AsyncMock(
        return_value=[
            Mock(domain="tidal", instance_id="tidal_instance_1"),
            Mock(domain="tidal", instance_id="tidal_instance_2"),
            Mock(domain="other", instance_id="other_instance"),
        ]
    )

    parser_with_module = Mock()
    parser_with_module.modules = [{"title": "Test Module"}]
    parser_with_module.get_module_items.return_value = (
        [Mock(item_id="rec_1", name="Recommendation 1")],
        MediaType.PLAYLIST,
    )

    parser_empty = Mock()
    parser_empty.modules = []
    parser_empty.get_module_items = Mock()

    with patch.object(
        recommendation_manager, "get_page_content", new_callable=AsyncMock
    ) as mock_get_page:
        # Only first page returns the module, remaining pages return no modules
        mock_get_page.side_effect = [parser_with_module] + [parser_empty] * 4

        recommendations = await recommendation_manager.get_recommendations()

        assert len(recommendations) == 1
        assert recommendations[0].name == "Test Module (john)"
        assert "@" not in recommendations[0].name
        assert len(recommendations[0].items) == 1


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
        mock_parser_instance.parse_page_structure = Mock()  # Ensure it's a synchronous mock
        mock_parser_instance.to_cache = Mock(
            return_value={"module_map": [], "content_map": {}, "parsed_at": 1234567890}
        )

        # Mock API response
        provider_mock.api.get.return_value = {"rows": []}

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


async def test_get_page_content_propagates_api_errors(
    recommendation_manager: TidalRecommendationManager, provider_mock: Mock
) -> None:
    """Test API failures propagate so an empty result is never cached."""
    with patch(
        "music_assistant.providers.tidal.recommendations.TidalPageParser"
    ) as mock_parser_cls:
        mock_parser_cls.from_cache = AsyncMock(return_value=None)
        provider_mock.api.get.side_effect = ResourceTemporarilyUnavailable("API error")

        with pytest.raises(ResourceTemporarilyUnavailable):
            await recommendation_manager.get_page_content("pages/home")

        provider_mock.mass.cache.set.assert_not_called()


def _paged_module(embedded: int, total: int) -> dict[str, Any]:
    """Build a page module whose pagedList embeds only part of its items."""
    return {
        "title": "Custom mixes",
        "type": "MIX_LIST",
        "pagedList": {
            "dataApiPath": "pages/data/abc",
            "limit": embedded,
            "offset": 0,
            "totalNumberOfItems": total,
            "items": [{"id": f"m{i}"} for i in range(embedded)],
        },
    }


async def _fetch_module(
    manager: TidalRecommendationManager,
    provider_mock: Mock,
    module: dict[str, Any],
    data_handler: DataHandler,
) -> list[dict[str, Any]]:
    """Fetch a one-module page, serving pages/data calls from data_handler."""
    page = {"rows": [{"modules": [module]}]}

    async def _get(endpoint: str, **kwargs: Any) -> dict[str, Any]:
        if endpoint.startswith("pages/data"):
            result: dict[str, Any] = await data_handler(endpoint, **kwargs)
            return result
        return page

    provider_mock.api.get.side_effect = _get
    with patch(
        "music_assistant.providers.tidal.recommendations.TidalPageParser.from_cache",
        new=AsyncMock(return_value=None),
    ):
        parser = await manager.get_page_content("pages/for_you")
    items: list[dict[str, Any]] = parser.modules[0]["raw_data"]["pagedList"]["items"]
    return items


def _slicing_handler(calls: list[dict[str, Any]], max_per_call: int = 1000) -> DataHandler:
    """Return a pages/data handler serving offset/limit slices of numbered items."""

    async def _handler(endpoint: str, **kwargs: Any) -> dict[str, Any]:
        params = kwargs["params"]
        calls.append({"endpoint": endpoint, **params})
        offset, limit = params["offset"], min(params["limit"], max_per_call)
        return {"items": [{"id": f"m{i}"} for i in range(offset, offset + limit)]}

    return _handler


async def test_get_page_content_extends_truncated_module(
    recommendation_manager: TidalRecommendationManager, provider_mock: Mock
) -> None:
    """A module embedding 6 of 8 items gets the missing 2 from its dataApiPath."""
    calls: list[dict[str, Any]] = []
    items = await _fetch_module(
        recommendation_manager, provider_mock, _paged_module(6, 8), _slicing_handler(calls)
    )

    assert [i["id"] for i in items] == [f"m{i}" for i in range(8)]
    assert len(calls) == 1
    assert calls[0]["endpoint"] == "pages/data/abc"
    assert (calls[0]["offset"], calls[0]["limit"]) == (6, 2)


async def test_get_page_content_respects_module_item_cap(
    recommendation_manager: TidalRecommendationManager, provider_mock: Mock
) -> None:
    """Extension stops at MAX_PAGE_MODULE_ITEMS and loops over short pages."""
    calls: list[dict[str, Any]] = []
    items = await _fetch_module(
        recommendation_manager,
        provider_mock,
        _paged_module(20, 209),
        _slicing_handler(calls, max_per_call=20),
    )

    assert len(items) == MAX_PAGE_MODULE_ITEMS
    assert len(calls) == 2
    assert all(call["offset"] + call["limit"] <= MAX_PAGE_MODULE_ITEMS for call in calls)


async def test_get_page_content_keeps_embedded_items_on_extend_failure(
    recommendation_manager: TidalRecommendationManager, provider_mock: Mock
) -> None:
    """A 404 on pages/data keeps the embedded items and still caches the page."""

    async def _not_found(_endpoint: str, **_kwargs: Any) -> dict[str, Any]:
        raise MediaNotFoundError("Not found")

    items = await _fetch_module(
        recommendation_manager, provider_mock, _paged_module(6, 8), _not_found
    )

    assert len(items) == 6
    provider_mock.mass.cache.set.assert_called()


async def test_get_page_content_propagates_extend_errors(
    recommendation_manager: TidalRecommendationManager, provider_mock: Mock
) -> None:
    """Non-404 pages/data failures propagate so a degraded page is never cached."""

    async def _fail(_endpoint: str, **_kwargs: Any) -> dict[str, Any]:
        raise ResourceTemporarilyUnavailable("API error")

    with pytest.raises(ResourceTemporarilyUnavailable):
        await _fetch_module(recommendation_manager, provider_mock, _paged_module(6, 8), _fail)

    provider_mock.mass.cache.set.assert_not_called()


@pytest.mark.parametrize(
    ("title", "module_type"),
    [("Music videos", "VIDEO_LIST"), ("Video Playlists", "PLAYLIST_LIST"), ("", "MIX_LIST")],
)
async def test_get_page_content_skips_extending_discarded_modules(
    recommendation_manager: TidalRecommendationManager,
    provider_mock: Mock,
    title: str,
    module_type: str,
) -> None:
    """Video and untitled modules are dropped later, so no pages/data call is made."""
    calls: list[dict[str, Any]] = []
    module = {**_paged_module(6, 8), "title": title, "type": module_type}
    items = await _fetch_module(
        recommendation_manager, provider_mock, module, _slicing_handler(calls)
    )

    assert len(items) == 6
    assert calls == []
