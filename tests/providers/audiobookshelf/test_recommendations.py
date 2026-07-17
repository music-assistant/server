"""Test Audiobookshelf recommendations() row filtering via the `wanted` parameter."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest
from aioaudiobookshelf.schema.shelf import ShelfBook, ShelfLibraryItemMinified
from aioaudiobookshelf.schema.shelf import ShelfId as AbsShelfId
from aioaudiobookshelf.schema.shelf import ShelfType as AbsShelfType

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


def _install_cache_mocks(provider: Audiobookshelf) -> None:
    """Make the @use_cache decorator treat every call as a cache miss."""
    provider.mass.cache.get_with_freshness = AsyncMock(  # type: ignore[method-assign]
        return_value=(None, False, False)
    )
    provider.mass.cache.set = AsyncMock()  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_recommendations_wanted_none_fetches_all_rows(provider: Audiobookshelf) -> None:
    """wanted=None (default) fetches the shelves and builds all rows — unchanged behavior."""
    _install_cache_mocks(provider)
    view_mock = _stub_backend(provider)

    result = await provider.recommendations()

    view_mock.assert_awaited_once_with(library_id="lib1", limit=20)
    assert {f.item_id for f in result} == {"recently-added", "browse"}


@pytest.mark.asyncio
async def test_recommendations_wanted_shelf_row_skips_browse(provider: Audiobookshelf) -> None:
    """wanted={recently-added} still issues the per-library fetch, but skips the browse row."""
    _install_cache_mocks(provider)
    view_mock = _stub_backend(provider)

    result = await provider.recommendations(wanted={"recently-added"})

    view_mock.assert_awaited_once_with(library_id="lib1", limit=20)
    assert [f.item_id for f in result] == ["recently-added"]


@pytest.mark.asyncio
async def test_recommendations_wanted_browse_only_skips_shelf_fetch(
    provider: Audiobookshelf,
) -> None:
    """wanted={browse} skips the personalized view fetch and returns only the browse row."""
    _install_cache_mocks(provider)
    view_mock = _stub_backend(provider)

    result = await provider.recommendations(wanted={"browse"})

    view_mock.assert_not_awaited()
    assert [f.item_id for f in result] == ["browse"]
