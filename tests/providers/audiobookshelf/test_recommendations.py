"""Test Audiobookshelf's two-method recommendations contract."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from aioaudiobookshelf.schema.shelf import ShelfBook, ShelfLibraryItemMinified
from aioaudiobookshelf.schema.shelf import ShelfId as AbsShelfId
from aioaudiobookshelf.schema.shelf import ShelfType as AbsShelfType
from music_assistant_models.media_items import BrowseFolder, UniqueList

from music_assistant.providers.audiobookshelf import Audiobookshelf


def _make_shelf() -> Mock:
    """Create a minimal recently-added book shelf as returned by the personalized view."""
    entity = Mock(spec=ShelfLibraryItemMinified)
    entity.id_ = "book1"
    shelf = Mock(spec=ShelfBook)
    shelf.id_ = AbsShelfId.RECENTLY_ADDED
    shelf.type_ = AbsShelfType.BOOK
    shelf.entities = [entity]
    return shelf


def _stub_backend(provider: Audiobookshelf) -> AsyncMock:
    """Stub the personalized view call and the library item lookup."""
    view_mock = AsyncMock(return_value=[_make_shelf()])
    provider._client.get_library_personalized_view = view_mock  # type: ignore[method-assign]
    provider.mass.music.get_library_item_by_prov_id = AsyncMock(  # type: ignore[method-assign]
        return_value=Mock()
    )
    return view_mock


def _install_cache_mocks(provider: Audiobookshelf) -> list[asyncio.Future[Any]]:
    """Back the @use_cache decorator with a dict store; return the background store tasks."""
    store: dict[str, Any] = {}
    tasks: list[asyncio.Future[Any]] = []

    async def _cache_get(key: str, **_kwargs: Any) -> tuple[Any, bool, bool]:
        if key in store:
            return store[key], True, True
        return None, False, False

    async def _cache_set(key: str, data: Any, **_kwargs: Any) -> None:
        store[key] = data

    def _create_task(target: Any, *_args: Any, **_kwargs: Any) -> asyncio.Future[Any]:
        task: asyncio.Future[Any] = asyncio.ensure_future(target)
        tasks.append(task)
        return task

    provider.mass.cache.get_with_freshness = AsyncMock(  # type: ignore[method-assign]
        side_effect=_cache_get
    )
    provider.mass.cache.set = AsyncMock(side_effect=_cache_set)  # type: ignore[method-assign]
    provider.mass.create_task = Mock(side_effect=_create_task)  # type: ignore[method-assign]
    return tasks


@pytest.mark.asyncio
async def test_get_recommendations_returns_shelf_rows_plus_browse(
    provider: Audiobookshelf,
) -> None:
    """The rows are the payload's shelf rows plus the static browse row, all without items."""
    _install_cache_mocks(provider)
    view_mock = _stub_backend(provider)

    rows = await provider.get_recommendations()

    view_mock.assert_awaited_once_with(library_id="lib1", limit=20)
    assert [f.item_id for f in rows] == ["recently-added", "browse"]
    assert all(len(f.items) == 0 for f in rows)


@pytest.mark.asyncio
async def test_get_recommendations_row_identity(provider: Audiobookshelf) -> None:
    """Row identity fields match the previous bulk implementation exactly."""
    _install_cache_mocks(provider)
    _stub_backend(provider)

    shelf_row, browse_row = await provider.get_recommendations()

    assert shelf_row.name == "Recently added"
    assert shelf_row.icon == "mdi-plus-box-multiple-outline"
    assert shelf_row.translation_key == "recently_added"
    assert shelf_row.provider == provider.instance_id
    assert browse_row.name == "Libraries"
    assert browse_row.icon == "mdi-bookshelf"
    assert browse_row.translation_key == "library"
    assert browse_row.provider == provider.instance_id


@pytest.mark.asyncio
async def test_get_recommendations_no_libraries_returns_no_rows(
    provider: Audiobookshelf,
) -> None:
    """Without any libraries there are no rows at all, browse included."""
    _install_cache_mocks(provider)
    view_mock = _stub_backend(provider)
    provider.libraries.audiobooks.clear()

    assert await provider.get_recommendations() == []
    view_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_recommendation_items_served_from_cached_payload(
    provider: Audiobookshelf,
) -> None:
    """A shelf row's items call reuses the payload the rows call fetched."""
    tasks = _install_cache_mocks(provider)
    view_mock = _stub_backend(provider)

    await provider.get_recommendations()
    # let the background cache-store task complete before the items call
    await asyncio.gather(*tasks)
    items = await provider.get_recommendation_items("recently-added")

    view_mock.assert_awaited_once_with(library_id="lib1", limit=20)
    assert len(items) == 1


@pytest.mark.asyncio
async def test_get_recommendation_items_cold_triggers_payload_fetch(
    provider: Audiobookshelf,
) -> None:
    """An items call without a warm cache performs the payload fetch itself."""
    _install_cache_mocks(provider)
    view_mock = _stub_backend(provider)

    items = await provider.get_recommendation_items("recently-added")

    view_mock.assert_awaited_once_with(library_id="lib1", limit=20)
    assert len(items) == 1


@pytest.mark.asyncio
async def test_get_recommendation_items_browse_is_local(provider: Audiobookshelf) -> None:
    """The browse row's items are built from local library state, without any backend fetch."""
    _install_cache_mocks(provider)
    view_mock = _stub_backend(provider)

    items = await provider.get_recommendation_items("browse")

    view_mock.assert_not_awaited()
    assert len(items) > 0
    assert all(isinstance(item, BrowseFolder) for item in items)


@pytest.mark.asyncio
async def test_get_recommendation_items_unknown_id_returns_empty(
    provider: Audiobookshelf,
) -> None:
    """An unknown row id yields an empty result."""
    _install_cache_mocks(provider)
    _stub_backend(provider)

    items = await provider.get_recommendation_items("bogus")

    assert isinstance(items, UniqueList)
    assert len(items) == 0
