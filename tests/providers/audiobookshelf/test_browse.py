"""Test that Audiobookshelf serves series as collections and authors/narrators as artists."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aioaudiobookshelf.schema.author import AuthorExpanded
from aioaudiobookshelf.schema.shelf import SeriesShelf, ShelfAuthors, ShelfSeries
from aioaudiobookshelf.schema.shelf import ShelfId as AbsShelfId
from aioaudiobookshelf.schema.shelf import ShelfType as AbsShelfType
from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import (
    Artist,
    MediaCollection,
    MediaItemType,
    ProviderMapping,
    UniqueList,
)

from music_assistant.helpers.collections import get_collection_item_id
from music_assistant.providers.audiobookshelf import Audiobookshelf

INSTANCE_ID = "audiobookshelf--test123"


def _artist(item_id: str, name: str) -> Artist:
    mapping = ProviderMapping(
        item_id=item_id, provider_domain="audiobookshelf", provider_instance=INSTANCE_ID
    )
    return Artist(item_id=item_id, provider="library", name=name, provider_mappings={mapping})


ARTISTS = {
    "aut1": _artist("aut1", "Terry Pratchett"),
    "aut2": _artist("aut2", "Robert Jordan"),
    "aut3": _artist("aut3", "Brandon Sanderson"),
}


def _collection(name: str) -> MediaCollection[Mock]:
    return MediaCollection(
        item_id=get_collection_item_id(name, MediaType.AUDIOBOOK),
        name=name,
        provider="library",
        provider_mappings=set(),
        items=UniqueList(),
    )


def _stub_library(provider: Audiobookshelf) -> None:
    provider.mass.music.audiobooks.library_items = AsyncMock(  # type: ignore[method-assign]
        return_value=[_collection("Wheel of Time"), Mock(), _collection("Discworld")]
    )

    # the db returns matches in its own order, not in the requested one
    async def _get_library_items_by_prov_id(
        provider_item_ids: list[str], **_kwargs: str
    ) -> list[Artist]:
        return [ARTISTS[x] for x in reversed(provider_item_ids) if x in ARTISTS]

    provider.mass.music.artists.get_library_items_by_prov_id = AsyncMock(  # type: ignore[method-assign,misc]
        side_effect=_get_library_items_by_prov_id
    )


@pytest.mark.asyncio
async def test_recommendation_shelves(provider: Audiobookshelf) -> None:
    """Series shelves hold collections and author shelves hold artists, not folders."""
    _stub_library(provider)
    series_shelf = Mock(spec=ShelfSeries)
    series_shelf.id_ = AbsShelfId.RECENT_SERIES
    series_shelf.type_ = AbsShelfType.SERIES
    series_shelf.entities = []
    for name in ("Discworld", "Not synced", "Wheel of Time"):
        entity = Mock(spec=SeriesShelf)
        entity.name = name
        series_shelf.entities.append(entity)
    authors_shelf = Mock(spec=ShelfAuthors)
    authors_shelf.id_ = AbsShelfId.NEWEST_AUTHORS
    authors_shelf.type_ = AbsShelfType.AUTHORS
    authors_shelf.entities = []
    for author_id, num_books in (("aut2", 2), ("aut3", 0), ("aut1", 3)):
        entity = Mock(spec=AuthorExpanded)
        entity.id_ = author_id
        entity.num_books = num_books
        authors_shelf.entities.append(entity)
    items_by_shelf_id: dict[AbsShelfId, list[list[MediaItemType]]] = {}

    await provider._recommendations_iter_shelves(
        [series_shelf, authors_shelf], items_by_shelf_id, await provider._get_series_collections()
    )

    (series,) = items_by_shelf_id[AbsShelfId.RECENT_SERIES]
    assert [(type(x), x.name) for x in series] == [
        (MediaCollection, "Discworld"),
        (MediaCollection, "Wheel of Time"),
    ]
    assert items_by_shelf_id[AbsShelfId.NEWEST_AUTHORS] == [[ARTISTS["aut2"], ARTISTS["aut1"]]]


@pytest.mark.parametrize(
    ("path_key", "client_method"),
    [("a", "get_library_authors"), ("n", "get_library_narrators")],
)
@pytest.mark.asyncio
async def test_browse_authors_and_narrators(
    provider: Audiobookshelf, path_key: str, client_method: str
) -> None:
    """Authors and narrators browse to their library artists, sorted by name."""
    _stub_library(provider)
    setattr(
        provider._client,
        client_method,
        AsyncMock(return_value=[SimpleNamespace(id_=x) for x in ("aut1", "missing", "aut2")]),
    )

    items = await provider.browse(f"{provider.instance_id}://lb lib1/{path_key}")

    assert items == [ARTISTS["aut2"], ARTISTS["aut1"]]


@pytest.mark.asyncio
async def test_browse_series(provider: Audiobookshelf) -> None:
    """Series browse to the library collections of the library's series, sorted and unique."""
    _stub_library(provider)

    async def _get_library_series(**_kwargs: str) -> AsyncGenerator[SimpleNamespace]:
        names = ("Wheel of Time", "Not synced", "Discworld", "Discworld")
        yield SimpleNamespace(results=[SimpleNamespace(name=x) for x in names])
        yield SimpleNamespace(results=[])

    provider._client.get_library_series = _get_library_series  # type: ignore[method-assign,assignment]

    items = await provider.browse(f"{provider.instance_id}://lb lib1/s")

    assert [(type(x), x.name) for x in items] == [
        (MediaCollection, "Discworld"),
        (MediaCollection, "Wheel of Time"),
    ]
