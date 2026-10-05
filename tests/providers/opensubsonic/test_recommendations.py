"""Test Open Subsonic get_recommendations() rows and get_recommendation_items() fetching."""

from __future__ import annotations

import asyncio
import pathlib
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from libopensonic.media import AlbumID3
from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import ItemMapping, RecommendationFolder, UniqueList

from music_assistant.providers.opensubsonic.sonic_provider import OpenSonicProvider

FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures"

ALL_ROW_IDS = [
    "subsonic_newest_podcasts",
    "subsonic_starred_albums",
    "subsonic_new_albums",
    "subsonic_most_played",
]


def _make_folder(provider: OpenSonicProvider, item_id: str, name: str) -> RecommendationFolder:
    return RecommendationFolder(
        item_id=item_id,
        provider=provider.domain,
        name=name,
        items=UniqueList(
            [
                ItemMapping(
                    media_type=MediaType.ALBUM,
                    item_id=f"{item_id}_album",
                    provider=provider.instance_id,
                    name=f"{name} Album",
                )
            ]
        ),
    )


def _enable_all_rows(provider: OpenSonicProvider) -> None:
    provider._enable_podcasts = True
    provider._show_faves = True
    provider._show_new = True
    provider._show_played = True


def _stub_shelf_builders(provider: OpenSonicProvider) -> dict[str, AsyncMock]:
    """Replace all four shelf-builder methods with AsyncMocks and enable all config gates."""
    _enable_all_rows(provider)
    mocks = {
        "_podcast_recommendations": AsyncMock(
            return_value=_make_folder(provider, "subsonic_newest_podcasts", "Newest Podcasts")
        ),
        "_favorites_recommendation": AsyncMock(
            return_value=_make_folder(provider, "subsonic_starred_albums", "Starred Albums")
        ),
        "_new_recommendations": AsyncMock(
            return_value=_make_folder(provider, "subsonic_new_albums", "New Albums")
        ),
        "_played_recommendations": AsyncMock(
            return_value=_make_folder(provider, "subsonic_most_played", "Most Played Albums")
        ),
    }
    for name, mock in mocks.items():
        setattr(provider, name, mock)
    return mocks


def _install_cache_mocks(provider: OpenSonicProvider) -> None:
    """Make the @use_cache decorator treat every call as a cache miss."""
    provider.mass.cache.get_with_freshness = AsyncMock(  # type: ignore[method-assign]
        return_value=(None, False, False)
    )
    provider.mass.cache.set = AsyncMock()  # type: ignore[method-assign]


def _install_dict_backed_cache(provider: OpenSonicProvider) -> list[asyncio.Future[Any]]:
    """
    Back the @use_cache decorator with a real dict store.

    Mirrors production: set() stores the serialized (to_dict) folder and, since the
    caller passes base_class=RecommendationFolder, get_with_freshness reconstructs it
    via RecommendationFolder.from_dict on a hit. Returns the list of background
    cache-store tasks so tests can await them before the warm call.
    """
    store: dict[str, Any] = {}
    tasks: list[asyncio.Future[Any]] = []

    async def _cache_get(key: str, **kwargs: Any) -> tuple[Any, bool, bool]:
        if key not in store:
            return None, False, False
        data = store[key]
        base_class = kwargs.get("base_class")
        if base_class is not None and data is not None:
            return base_class.from_dict(data), True, True
        return data, True, True

    async def _cache_set(key: str, data: Any, **_kwargs: Any) -> None:
        store[key] = data.to_dict() if data is not None else None

    def _create_task(target: Any, *_args: Any, **_kwargs: Any) -> asyncio.Future[Any]:
        task: asyncio.Future[Any] = asyncio.ensure_future(target)
        tasks.append(task)
        return task

    provider.mass.cache.get_with_freshness = AsyncMock(side_effect=_cache_get)  # type: ignore[method-assign]
    provider.mass.cache.set = AsyncMock(side_effect=_cache_set)  # type: ignore[method-assign]
    provider.mass.create_task = Mock(side_effect=_create_task)  # type: ignore[method-assign]
    return tasks


@pytest.mark.asyncio
async def test_get_recommendations_returns_all_rows_without_backend_calls(
    provider: OpenSonicProvider,
) -> None:
    """With all config flags on, all four rows are returned in order with zero backend I/O."""
    _enable_all_rows(provider)
    provider.conn = Mock()

    result = await provider.get_recommendations()

    assert provider.conn.mock_calls == []
    assert [folder.item_id for folder in result] == ALL_ROW_IDS
    assert all(len(folder.items) == 0 for folder in result)
    assert [folder.translation_key for folder in result] == [
        "episodes_recently_added",
        "starred_items",
        "recently_added_albums",
        "most_played_albums",
    ]


@pytest.mark.asyncio
async def test_get_recommendations_omits_config_disabled_rows(
    provider: OpenSonicProvider,
) -> None:
    """Rows whose config flag is off are absent from the rows listing."""
    _enable_all_rows(provider)
    provider._enable_podcasts = False
    provider._show_new = False
    provider.conn = Mock()

    result = await provider.get_recommendations()

    assert [folder.item_id for folder in result] == [
        "subsonic_starred_albums",
        "subsonic_most_played",
    ]


@pytest.mark.asyncio
async def test_get_recommendation_items_builds_only_that_row(
    provider: OpenSonicProvider,
) -> None:
    """Requesting one row runs only that row's shelf builder and returns its items."""
    _install_cache_mocks(provider)
    mocks = _stub_shelf_builders(provider)

    result = await provider.get_recommendation_items("subsonic_new_albums")

    mocks["_new_recommendations"].assert_awaited_once()
    mocks["_podcast_recommendations"].assert_not_awaited()
    mocks["_favorites_recommendation"].assert_not_awaited()
    mocks["_played_recommendations"].assert_not_awaited()
    assert [item.item_id for item in result] == ["subsonic_new_albums_album"]


@pytest.mark.asyncio
async def test_get_recommendation_items_unknown_id_returns_empty(
    provider: OpenSonicProvider,
) -> None:
    """An unknown row item_id returns an empty UniqueList without any shelf builds."""
    _install_cache_mocks(provider)
    mocks = _stub_shelf_builders(provider)

    result = await provider.get_recommendation_items("bogus_row")

    for mock in mocks.values():
        mock.assert_not_awaited()
    assert isinstance(result, UniqueList)
    assert len(result) == 0


@pytest.mark.asyncio
async def test_get_recommendation_items_config_disabled_row_returns_empty(
    provider: OpenSonicProvider,
) -> None:
    """A row disabled in the provider config yields no items even when requested directly."""
    _install_cache_mocks(provider)
    mocks = _stub_shelf_builders(provider)
    provider._show_new = False

    result = await provider.get_recommendation_items("subsonic_new_albums")

    mocks["_new_recommendations"].assert_not_awaited()
    assert len(result) == 0


@pytest.mark.asyncio
async def test_get_recommendation_items_warm_cache_hit_skips_backend(
    provider: OpenSonicProvider,
) -> None:
    """A second items call is served from the cached folder without hitting the backend."""
    _enable_all_rows(provider)
    store_tasks = _install_dict_backed_cache(provider)
    album = AlbumID3.from_json(
        (FIXTURES_DIR / "albums" / "spec.album.json").read_text(encoding="utf-8")
    )
    provider.conn = Mock()
    provider.conn.get_album_list2 = AsyncMock(return_value=[album])

    first = await provider.get_recommendation_items("subsonic_new_albums")
    # let the decorator's background cache-store task complete
    await asyncio.gather(*store_tasks)
    second = await provider.get_recommendation_items("subsonic_new_albums")

    # backend was fetched exactly once: the warm call was served from the cache
    provider.conn.get_album_list2.assert_awaited_once()
    assert [item.item_id for item in first] == [album.id]
    assert [item.item_id for item in second] == [album.id]
